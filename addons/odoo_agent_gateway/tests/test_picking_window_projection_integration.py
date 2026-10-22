"""Integration tests for the ``picking_window_v1`` bounded search projection, on real Odoo 19.

The claims under test, against Odoo itself:

* **Coverage.** Every picking today's elevated search domain returns appears in the answer, either
  ``included`` with values proven under the caller or ``refused`` -- never silently absent. And
  pickings today's domain misses because a stored value is stale are found too.
* **No partial answers.** Over a work or response bound, the whole request is refused with no rows
  and no totals, and a refusal is never shaped like an empty answer.
* **No claim beyond the proof.** Every answer is ``covered`` under the named coverage, never
  ``complete``, and ``absence_proven`` is always false. The counterexample that makes a complete
  recomputed answer unprovable -- a stale stored date plus a hidden determining move -- is a
  regression test, together with Odoo's own recomputation showing the omitted picking does match.
* **One call, one snapshot.** There is no cursor. A mutation between two calls is reflected whole
  in the second call, never spliced into the first.
* **One snapshot inside the call, measured rather than inferred.** In
  :class:`TestWindowSnapshotIsolation` the method runs on a cursor of its own, opened the way JSON-2
  opens one, while a *second PostgreSQL connection commits* between search A, search B and the
  verification. At Odoo's ``REPEATABLE READ`` the answer is exactly the answer before the commit.
  At ``READ COMMITTED`` -- with the guard made to lie -- the same interleavings miss a covered
  picking and splice two database states into one answer, which is why the method reads the
  isolation level and the snapshot itself and refuses rather than answering.

Hidden rows are produced with real record rules scoped to the service group; stale values either
with SQL (the stored ``scheduled_date`` is kept fresh by the ORM, so only an out-of-ORM write can
stale it) or with an ordinary ORM write Odoo's own ``@api.depends`` does not track.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models import picking_window_projection

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
PROJECTION_MODEL = "odoo.agent.picking.window.projection"
METHOD = "agent_projected_picking_window_v1"
OPEN = ["waiting", "confirmed", "assigned"]
MTO = {"procure_method": "make_to_order"}

T0 = datetime(2026, 10, 1, 8, 0, 0)
FORMAT = "%Y-%m-%d %H:%M:%S"


def at(hours):
    return T0 + timedelta(hours=hours)


def wire(value):
    return value.strftime(FORMAT)


INSIDE = at(1)
OUTSIDE = at(24 * 10)
EARLY = at(-48)
WINDOW = (wire(at(-24)), wire(at(24)))


class WindowCase(TransactionCase):
    """Two companies with two operation types in A and one in B, and three identities."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent Window Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent Window Beta"})
        cls.type_a = cls._operation_type(cls.company_a, "Window Alpha Out")
        cls.type_a2 = cls._operation_type(cls.company_a, "Window Alpha Two")
        cls.type_b = cls._operation_type(cls.company_b, "Window Beta Out")
        cls.product = cls.env["product.product"].create({"name": "Agent Window Widget"})
        cls.secret_product = cls.env["product.product"].create({"name": "Agent Window Secret"})
        cls.customers = cls.env.ref("stock.stock_location_customers")
        cls.suppliers = cls.env.ref("stock.stock_location_suppliers")

        cls.service_user = cls._user("agent.window.service", [cls.company_a], service=True)
        cls.ordinary_user = cls._user("agent.window.ordinary", [cls.company_a], service=False)
        cls.both_companies_user = cls._user(
            "agent.window.both", [cls.company_a, cls.company_b], service=True
        )

    @classmethod
    def _operation_type(cls, company, name):
        warehouse = cls.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
        return cls.env["stock.picking.type"].create(
            {
                "name": name,
                "code": "outgoing",
                "sequence_code": name.replace(" ", "")[:5],
                "company_id": company.id,
                "warehouse_id": warehouse.id if warehouse else False,
            }
        )

    @classmethod
    def _user(cls, login, companies, *, service):
        groups = [cls.env.ref("base.group_user").id, cls.env.ref("stock.group_stock_user").id]
        if service:
            groups.append(cls.env.ref(SERVICE_GROUP).id)
        return cls.env["res.users"].create(
            {
                "name": login,
                "login": login,
                "group_ids": [(6, 0, groups)],
                "company_id": companies[0].id,
                "company_ids": [(6, 0, [company.id for company in companies])],
            }
        )

    # -- fixtures -------------------------------------------------------------------------

    def picking(self, picking_type, moves, *, source=None):
        """A picking with ``(state, date, extra)`` moves. ``assigned`` make-to-stock moves give a
        location-independent verified state, so most fixtures use them."""

        company = picking_type.company_id
        stock = self.env["stock.location"].search(
            [("usage", "=", "internal"), ("company_id", "=", company.id)], limit=1
        )
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": (source or stock).id,
                "location_dest_id": self.customers.id,
            }
        )
        for state, date, extra in moves:
            self.add_move(picking, state, date, **extra)
        return picking

    def add_move(self, picking, state, date, product=None, **values):
        move = self.env["stock.move"].create(
            {
                "picking_id": picking.id,
                "product_id": (product or self.product).id,
                "product_uom_qty": 1.0,
                "date": date,
                "company_id": picking.company_id.id,
                "location_id": picking.location_id.id,
                "location_dest_id": self.customers.id,
                **values,
            }
        )
        if state != "draft":
            move.write({"state": state})
        return move

    def hide_from_service(self, model, domain):
        self.env["ir.rule"].create(
            {
                "name": f"agent test: hide {model} rows from the service identity",
                "model_id": self.env["ir.model"]._get_id(model),
                "groups": [(6, 0, [self.env.ref(SERVICE_GROUP).id])],
                "domain_force": domain,
            }
        )

    def sql(self, statement, params):
        self.env.flush_all()
        self.env.cr.execute(statement, params)
        self.env.invalidate_all()

    # -- invocation -----------------------------------------------------------------------

    def window(
        self,
        user=None,
        types=None,
        *,
        company=None,
        states=None,
        window=WINDOW,
        max_rows=200,
        companies=None,
    ):
        self.env.flush_all()
        env = self.env(user=user or self.service_user, su=False)
        if companies is not None:
            env = env(context=dict(env.context, allowed_company_ids=[c.id for c in companies]))
        return env[PROJECTION_MODEL].agent_projected_picking_window_v1(
            (company or self.company_a).id,
            [t.id for t in (types or [self.type_a])],
            states or OPEN,
            window[0],
            window[1],
            max_rows,
        )

    def listed(self, response):
        return {row["picking_id"]: row["disposition"] for row in response["rows"]}

    def assert_covered(self, response):
        """The only success shape: covered, named coverage, absence never claimed, totals that
        agree with the rows listed."""

        self.assertEqual(response["status"], "covered")
        self.assertEqual(
            response["coverage"], "stored-scheduled-date-or-visible-open-move-in-window"
        )
        self.assertIs(response["absence_proven"], False)
        totals = response["totals"]
        self.assertEqual(totals["included"] + totals["refused"], len(response["rows"]))
        self.assertEqual(
            totals["candidates"], totals["included"] + totals["refused"] + totals["excluded"]
        )
        ids = [row["picking_id"] for row in response["rows"]]
        self.assertEqual(ids, sorted(ids))
        return response

    def assert_refused_request(self, response, status):
        self.assertEqual(
            (response["status"], response["coverage"], response["totals"], response["rows"]),
            (status, False, False, []),
        )
        self.assertIs(response["absence_proven"], False)

    def todays_domain(self, user=None, types=None, company=None, states=None):
        """The elevated domain ``PickingMoveReader.search_pickings`` sends today, as the caller."""

        env = self.env(user=user or self.service_user, su=False)
        return set(
            env["stock.picking"]
            .search(
                [
                    ("company_id", "=", (company or self.company_a).id),
                    ("picking_type_id", "in", [t.id for t in (types or [self.type_a])]),
                    ("state", "in", states or OPEN),
                    ("scheduled_date", ">=", WINDOW[0]),
                    ("scheduled_date", "<=", WINDOW[1]),
                ]
            )
            .ids
        )


@tagged("post_install", "-at_install")
class TestWindowAuthorization(WindowCase):
    def test_an_ordinary_internal_user_is_refused(self):
        with self.assertRaises(AccessError) as refusal:
            self.window(self.ordinary_user)
        self.assertIn("requires-the-service-group", str(refusal.exception))

    def test_the_service_identity_is_allowed(self):
        self.assert_covered(self.window())

    def test_superuser_mode_is_refused(self):
        with self.assertRaises(UserError) as refusal:
            self.env[PROJECTION_MODEL].sudo().agent_projected_picking_window_v1(
                self.company_a.id, [self.type_a.id], OPEN, *WINDOW, 10
            )
        self.assertIn("refuses-superuser-mode", str(refusal.exception))

    def test_superuser_refusal_precedes_the_group_check(self):
        with self.assertRaises(UserError) as refusal:
            self.env(user=self.ordinary_user)[
                PROJECTION_MODEL
            ].sudo().agent_projected_picking_window_v1(
                self.company_a.id, [self.type_a.id], OPEN, *WINDOW, 10
            )
        self.assertIn("refuses-superuser-mode", str(refusal.exception))


@tagged("post_install", "-at_install")
class TestWindowAnswers(WindowCase):
    """The three dispositions, and coverage of today's domain."""

    def test_a_verified_picking_is_included_with_odoos_own_values(self):
        picking = self.picking(self.type_a, [("assigned", INSIDE, {})])
        response = self.assert_covered(self.window())
        (row,) = response["rows"]
        self.assertEqual(
            row,
            {
                "picking_id": picking.id,
                "disposition": "included",
                "company_id": self.company_a.id,
                "state": picking.state,
                "scheduled_date": wire(picking.scheduled_date),
                "statuses": {
                    "company": "verified",
                    "state": "verified",
                    "scheduled_date": "verified",
                },
            },
        )
        self.assertEqual(picking.state, "assigned")

    def test_a_proven_mismatch_is_counted_not_listed(self):
        self.picking(self.type_a, [("assigned", INSIDE, {})])
        response = self.assert_covered(self.window(states=["waiting"]))
        self.assertEqual(response["rows"], [])
        self.assertEqual(
            response["totals"], {"candidates": 1, "included": 0, "refused": 0, "excluded": 1}
        )

    def test_an_unprovable_picking_is_listed_as_refused_with_its_reasons(self):
        """An unreserved make-to-stock move: its state depends on the elevated source location."""

        picking = self.picking(self.type_a, [("confirmed", INSIDE, {})])
        (row,) = self.window()["rows"]
        self.assertEqual(row["picking_id"], picking.id)
        self.assertEqual(row["disposition"], "refused")
        self.assertEqual(
            row["statuses"],
            {
                "company": "verified",
                "state": "source-location-dependent",
                "scheduled_date": "verified",
            },
        )
        self.assertEqual((row["company_id"], row["state"], row["scheduled_date"]), (False,) * 3)

    def test_an_empty_covered_answer_is_distinct_from_a_refusal_and_claims_no_absence(self):
        """An empty answer says only that nothing is covered -- never that nothing matches."""

        empty = self.assert_covered(self.window())
        self.assertEqual(empty["totals"]["candidates"], 0)
        self.assertEqual(empty["rows"], [])
        self.assertIs(empty["absence_proven"], False)
        self.assert_refused_request(self.window(company=self.company_b), "company-not-allowed")

    def test_every_picking_todays_domain_returns_is_listed(self):
        """Coverage against the domain this replaces, over a mixed set."""

        pickings = [
            self.picking(self.type_a, [("assigned", INSIDE, {})]),
            self.picking(self.type_a, [("confirmed", INSIDE, {})]),
            self.picking(self.type_a, [("waiting", INSIDE, MTO)]),
            self.picking(self.type_a, [("assigned", at(-3), {}), ("confirmed", at(5), MTO)]),
            self.picking(self.type_a, [("assigned", OUTSIDE, {})]),
            self.picking(self.type_a, [("draft", INSIDE, {})]),
            self.picking(self.type_a, [("done", INSIDE, {})]),
        ]
        today = self.todays_domain()
        listed = self.listed(self.assert_covered(self.window()))
        self.assertTrue(today, "the fixture must exercise today's domain")
        self.assertLessEqual(today, set(listed), "today's result must never vanish")
        # Nothing outside the requested window or states is included.
        self.assertNotIn(pickings[4].id, listed)
        self.assertNotIn(pickings[5].id, listed)
        self.assertNotIn(pickings[6].id, listed)
        self.assertEqual(listed[pickings[0].id], "included")
        self.assertEqual(listed[pickings[1].id], "refused")
        self.assertEqual(listed[pickings[2].id], "included")

    def test_only_the_requested_operation_types_are_candidates(self):
        in_type = self.picking(self.type_a, [("assigned", INSIDE, {})])
        other_type = self.picking(self.type_a2, [("assigned", INSIDE, {})])
        self.assertEqual(set(self.listed(self.window(types=[self.type_a]))), {in_type.id})
        self.assertEqual(
            set(self.listed(self.window(types=[self.type_a, self.type_a2]))),
            {in_type.id, other_type.id},
        )


@tagged("post_install", "-at_install")
class TestWindowStaleValues(WindowCase):
    """Stored values that disagree with what the moves say."""

    def test_a_stored_date_staled_out_of_the_window_is_found_through_its_moves(self):
        """Today's domain loses this picking. Set A finds it; the tripwire refuses it."""

        picking = self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.sql(
            "UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (OUTSIDE, picking.id)
        )
        self.assertNotIn(picking.id, self.todays_domain())
        (row,) = self.window()["rows"]
        self.assertEqual(
            (row["picking_id"], row["disposition"], row["statuses"]["scheduled_date"]),
            (picking.id, "refused", "scheduled-date-conflicting"),
        )

    def test_a_stored_date_staled_into_the_window_is_found_through_its_stored_value(self):
        """No move is in the window, so only set B can find it; it is refused, not included."""

        picking = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        self.sql("UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (INSIDE, picking.id))
        self.assertIn(picking.id, self.todays_domain())
        (row,) = self.window()["rows"]
        self.assertEqual(
            (row["picking_id"], row["disposition"], row["statuses"]["scheduled_date"]),
            (picking.id, "refused", "scheduled-date-conflicting"),
        )

    def test_a_state_left_stale_by_odoos_own_dependencies_is_found_and_refused(self):
        """An ordinary ORM write Odoo's ``@api.depends`` does not track leaves ``assigned`` stored
        while Odoo's fresh compute says ``confirmed``. Today's domain asking for ``confirmed``
        misses it; set B has no state clause, so the window finds it."""

        picking = self.picking(self.type_a, [("confirmed", INSIDE, {})], source=self.suppliers)
        self.env.flush_all()
        self.assertEqual(picking.state, "assigned")
        picking.move_ids.write({"procure_method": "make_to_order"})
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(picking.state, "assigned", "stale by Odoo's design")
        self.assertNotIn(picking.id, self.todays_domain(states=["confirmed"]))
        (row,) = self.window(states=["confirmed"])["rows"]
        self.assertEqual(
            (row["picking_id"], row["disposition"], row["statuses"]["state"]),
            (picking.id, "refused", "state-conflicting"),
        )

    def test_a_stale_state_only_the_stored_date_can_reveal_is_still_found(self):
        """Isolates why set B carries no state clause.

        The visible move is out of the window, so set A cannot see the picking. Its stored date
        is in the window only because of a hidden move, and its stored state is left stale by an
        ordinary ORM write Odoo's ``@api.depends`` ignores: ``assigned`` stored, ``confirmed``
        true. A state clause on B -- the one today's domain has -- would lose it silently.
        """

        picking = self.picking(self.type_a, [("confirmed", OUTSIDE, {})], source=self.suppliers)
        self.add_move(picking, "confirmed", INSIDE, product=self.secret_product)
        self.env.flush_all()
        self.assertEqual((picking.state, picking.scheduled_date), ("assigned", INSIDE))
        picking.move_ids.write({"procure_method": "make_to_order"})
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(picking.state, "assigned", "stale by Odoo's design")
        self.hide_from_service("stock.move", f"[('product_id', '!=', {self.secret_product.id})]")
        self.assertNotIn(picking.id, self.todays_domain(states=["confirmed"]))
        response = self.window(states=["confirmed"])
        self.assertEqual(self.listed(response), {picking.id: "refused"})

    def test_the_counterexample_a_matching_picking_outside_both_sets_is_not_claimed(self):
        """Regression test for the gap between "covered" and a complete business answer.

        The stored date is staled out of the window **outside the ORM**, and the only in-window
        move is **hidden** from the caller. Neither search can see the picking, and no
        caller-rights read could. The response must therefore (1) not list it, (2) say
        ``covered`` under the named coverage rather than anything that reads as complete, and
        (3) state ``absence_proven: false``. Then Odoo's own fresh recomputation is run, to prove
        the omitted picking really does match -- so this is a genuine counterexample and not a
        picking that was correctly left out.
        """

        picking = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        self.add_move(picking, "assigned", INSIDE, product=self.secret_product)
        self.hide_from_service("stock.move", f"[('product_id', '!=', {self.secret_product.id})]")
        self.sql(
            "UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (OUTSIDE, picking.id)
        )

        response = self.assert_covered(self.window())
        self.assertNotIn(picking.id, self.listed(response))
        self.assertEqual(response["totals"]["candidates"], 0)
        self.assertIs(response["absence_proven"], False)

        # Odoo's truth, recomputed as superuser: the picking is assigned and scheduled INSIDE.
        self.env.add_to_compute(picking._fields["scheduled_date"], picking)
        self.env.add_to_compute(picking._fields["state"], picking)
        self.env.flush_all()
        self.assertEqual((picking.scheduled_date, picking.state), (INSIDE, "assigned"))


@tagged("post_install", "-at_install")
class TestWindowHiddenDependencies(WindowCase):
    def test_a_hidden_move_holding_the_in_window_date_is_found_and_refused(self):
        """The visible move is out of the window; Odoo's fresh stored date comes from a hidden
        in-window move. Set B finds the picking; the date tripwire refuses it."""

        picking = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        self.add_move(picking, "assigned", INSIDE, product=self.secret_product)
        self.hide_from_service("stock.move", f"[('product_id', '!=', {self.secret_product.id})]")
        self.assertEqual(picking.scheduled_date, INSIDE)
        (row,) = self.window()["rows"]
        self.assertEqual(
            (row["picking_id"], row["disposition"], row["statuses"]["scheduled_date"]),
            (picking.id, "refused", "scheduled-date-conflicting"),
        )

    def test_a_hidden_move_that_changes_nothing_leaves_the_picking_included(self):
        picking = self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.add_move(picking, "assigned", at(3), product=self.secret_product)
        self.hide_from_service("stock.move", f"[('product_id', '!=', {self.secret_product.id})]")
        self.assertEqual(self.listed(self.window()), {picking.id: "included"})

    def test_a_hidden_picking_is_not_a_candidate_through_either_search(self):
        """Its moves are readable, but the path search applies ``stock.picking``'s record rules."""

        visible = self.picking(self.type_a, [("assigned", INSIDE, {})])
        hidden = self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.hide_from_service("stock.picking", f"[('id', '!=', {hidden.id})]")
        response = self.assert_covered(self.window())
        self.assertEqual(self.listed(response), {visible.id: "included"})
        self.assertEqual(response["totals"]["candidates"], 1)

    def test_a_hidden_operation_type_refuses_the_whole_request(self):
        self.hide_from_service("stock.picking.type", f"[('id', '!=', {self.type_a2.id})]")
        self.assert_refused_request(
            self.window(types=[self.type_a, self.type_a2]), "operation-type-scope-invalid"
        )


@tagged("post_install", "-at_install")
class TestWindowCompanies(WindowCase):
    def test_a_company_outside_the_active_set_refuses_the_whole_request(self):
        self.assert_refused_request(
            self.window(company=self.company_b, types=[self.type_b]), "company-not-allowed"
        )

    def test_an_operation_type_of_another_company_refuses_the_whole_request(self):
        response = self.window(
            self.both_companies_user,
            types=[self.type_a, self.type_b],
            companies=[self.company_a, self.company_b],
        )
        self.assert_refused_request(response, "operation-type-scope-invalid")

    def test_each_company_answers_for_its_own_types(self):
        alpha = self.picking(self.type_a, [("assigned", INSIDE, {})])
        beta = self.picking(self.type_b, [("assigned", INSIDE, {})])
        both = [self.company_a, self.company_b]
        a = self.window(self.both_companies_user, [self.type_a], companies=both)
        b = self.window(
            self.both_companies_user, [self.type_b], company=self.company_b, companies=both
        )
        self.assertEqual(self.listed(a), {alpha.id: "included"})
        self.assertEqual(self.listed(b), {beta.id: "included"})
        self.assertEqual(b["rows"][0]["company_id"], self.company_b.id)

    def test_a_cross_company_move_is_a_witness_when_visible(self):
        """Set A has no company clause. A crossover move written around ``check_company`` holds
        the only in-window date, and the stored date is staled out of the window, so only the
        crossover move can reveal the picking. With both companies active it does."""

        picking = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        crossover = self.add_move(picking, "assigned", INSIDE)
        self.sql(
            "UPDATE stock_move SET company_id = %s WHERE id = %s", (self.company_b.id, crossover.id)
        )
        self.sql(
            "UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (OUTSIDE, picking.id)
        )
        both = [self.company_a, self.company_b]
        response = self.window(self.both_companies_user, companies=both)
        self.assertIn(picking.id, self.listed(response))
        self.assertEqual(self.listed(response)[picking.id], "refused")

    def test_a_cross_company_move_hidden_by_the_standard_rule_is_the_counterexample_again(self):
        """The same picking with only company A active: the crossover move is hidden by
        ``stock.move``'s own multi-company rule, so this is the counterexample through Odoo's
        standard rule rather than a test rule -- and still nothing is claimed about it."""

        picking = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        crossover = self.add_move(picking, "assigned", INSIDE)
        self.sql(
            "UPDATE stock_move SET company_id = %s WHERE id = %s", (self.company_b.id, crossover.id)
        )
        self.sql(
            "UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (OUTSIDE, picking.id)
        )
        response = self.assert_covered(
            self.window(self.both_companies_user, companies=[self.company_a])
        )
        self.assertNotIn(picking.id, self.listed(response))

    def test_a_fresh_cross_company_move_is_found_through_the_stored_date(self):
        """Without the SQL stale, Odoo's stored date reflects the crossover move, so set B finds
        the picking even when the move is hidden, and the tripwire refuses it."""

        picking = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        crossover = self.add_move(picking, "assigned", INSIDE)
        self.sql(
            "UPDATE stock_move SET company_id = %s WHERE id = %s", (self.company_b.id, crossover.id)
        )
        response = self.window(self.both_companies_user, companies=[self.company_a])
        self.assertEqual(self.listed(response), {picking.id: "refused"})


@tagged("post_install", "-at_install")
class TestWindowBounds(WindowCase):
    """Boundary-sized sets: exactly at each bound answers, one over refuses the whole request."""

    def test_the_witness_move_bound(self):
        self.patch(picking_window_projection, "MAX_WITNESS_MOVES", 3)
        pickings = [self.picking(self.type_a, [("assigned", INSIDE, {})]) for _ in range(3)]
        at_bound = self.assert_covered(self.window())
        self.assertEqual(set(self.listed(at_bound)), {p.id for p in pickings})
        self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.assert_refused_request(self.window(), "witness-moves-over-bound")

    def test_the_stored_candidate_bound(self):
        self.patch(picking_window_projection, "MAX_STORED_CANDIDATES", 2)
        for _ in range(2):
            self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.assert_covered(self.window())
        self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.assert_refused_request(self.window(), "stored-candidates-over-bound")

    def test_the_union_bound(self):
        """Two pickings found only by A and two only by B: the union is what is bounded."""

        self.patch(picking_window_projection, "MAX_CANDIDATES", 3)
        only_a = [self.picking(self.type_a, [("assigned", INSIDE, {})]) for _ in range(2)]
        for picking in only_a:
            self.sql(
                "UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (OUTSIDE, picking.id)
            )
        only_b = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        self.sql("UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (INSIDE, only_b.id))
        self.assert_covered(self.window())
        second_b = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        self.sql(
            "UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (INSIDE, second_b.id)
        )
        self.assert_refused_request(self.window(), "candidates-over-bound")

    def test_the_whole_answer_comes_in_one_call_ordered_by_id(self):
        pickings = [self.picking(self.type_a, [("assigned", INSIDE, {})]) for _ in range(5)]
        pickings.append(self.picking(self.type_a, [("confirmed", INSIDE, {})]))
        response = self.assert_covered(self.window())
        self.assertEqual(
            [row["picking_id"] for row in response["rows"]], sorted(p.id for p in pickings)
        )
        self.assertEqual(response["totals"]["included"], 5)
        self.assertEqual(response["totals"]["refused"], 1)

    def test_the_response_bound_refuses_rather_than_truncating(self):
        """Exactly ``max_rows`` listed answers; one more refuses the whole request, no rows."""

        for _ in range(3):
            self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.picking(self.type_a, [("assigned", OUTSIDE, {})])  # not a candidate at all
        self.assertEqual(len(self.assert_covered(self.window(max_rows=3))["rows"]), 3)
        self.assert_refused_request(self.window(max_rows=2), "rows-over-limit")

    def test_excluded_candidates_do_not_count_against_the_response_bound(self):
        self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.picking(self.type_a, [("assigned", INSIDE, {})])
        response = self.assert_covered(self.window(states=["waiting"], max_rows=1))
        self.assertEqual((response["rows"], response["totals"]["excluded"]), ([], 2))

    def test_there_is_no_cursor_to_continue_from(self):
        """Independent calls are independent snapshots; the method accepts nothing that would
        suggest otherwise."""

        env = self.env(user=self.service_user, su=False)[PROJECTION_MODEL]
        with self.assertRaises(TypeError):
            env.agent_projected_picking_window_v1(
                self.company_a.id, [self.type_a.id], OPEN, *WINDOW, 10, 0
            )
        for key in ("page", "has_more", "next_after_picking_id", "after_picking_id"):
            self.assertNotIn(key, self.window())

    def test_a_mutation_between_calls_is_reflected_whole_in_the_next_snapshot(self):
        """Regression test for cross-request pagination.

        Under the withdrawn cursor contract, a picking with a *low* id that started matching after
        page one had been read -- or one that stopped matching -- was either missed or double
        counted, because each page was its own snapshot. Now each call is the whole answer from one
        snapshot: the first reflects the state before the mutations, the second the state after,
        and neither is a splice of both.
        """

        low = self.picking(self.type_a, [("assigned", OUTSIDE, {})])
        middle = self.picking(self.type_a, [("assigned", INSIDE, {})])
        high = self.picking(self.type_a, [("assigned", INSIDE, {})])
        before = self.assert_covered(self.window())
        self.assertEqual(self.listed(before), {middle.id: "included", high.id: "included"})

        # Between the calls: the lowest id starts matching, the highest stops.
        low.move_ids.write({"date": INSIDE})
        high.move_ids.write({"date": OUTSIDE})
        self.env.flush_all()
        self.assertEqual((low.scheduled_date, high.scheduled_date), (INSIDE, OUTSIDE))

        after = self.assert_covered(self.window())
        self.assertEqual(self.listed(after), {low.id: "included", middle.id: "included"})
        # The first answer is untouched by what happened after it: it was never a partial view.
        self.assertEqual(self.listed(before), {middle.id: "included", high.id: "included"})

    def test_the_argument_grammar(self):
        good = dict(
            company_id=self.company_a.id,
            picking_type_ids=[self.type_a.id],
            states=OPEN,
            scheduled_from=WINDOW[0],
            scheduled_to=WINDOW[1],
            max_rows=10,
        )
        env = self.env(user=self.service_user, su=False)[PROJECTION_MODEL]
        bad_values = {
            "company_id": [0, True, "1", None],
            "picking_type_ids": [[], [True], list(range(1, 52)), "1"],
            "states": [[], ["draft"], ["done"], "assigned"],
            "scheduled_from": ["2026-10-01", "2026-10-01T08:00:00", None, wire(at(48))],
            "scheduled_to": [wire(at(24 * 400)), "later"],
            "max_rows": [0, 201, True, None],
        }
        for name, values in bad_values.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(UserError):
                    env.agent_projected_picking_window_v1(**{**good, name: value})

    def test_the_response_envelope_is_closed(self):
        self.picking(self.type_a, [("assigned", INSIDE, {})])
        self.picking(self.type_a, [("confirmed", INSIDE, {})])
        for response in (self.window(), self.window(company=self.company_b)):
            self.assertEqual(
                list(response),
                [
                    "schema_version",
                    "projection",
                    "status",
                    "coverage",
                    "absence_proven",
                    "totals",
                    "rows",
                ],
            )
            self.assertEqual(response["projection"], "picking_window_v1")
            for row in response["rows"]:
                self.assertEqual(
                    list(row),
                    [
                        "picking_id",
                        "disposition",
                        "company_id",
                        "state",
                        "scheduled_date",
                        "statuses",
                    ],
                )


@tagged("post_install", "-at_install")
class TestWindowSnapshotIsolation(TransactionCase):
    """Real PostgreSQL concurrency: a second connection commits between the method's steps.

    Nothing here uses the test's own transaction, because a row it holds uncommitted is invisible
    to every other connection. The fixtures are committed in a transaction of their own and deleted
    in cleanup; each call runs on a fresh ``registry.cursor(readonly=True)`` -- the constructor
    ``Request._serve_db`` uses for a readonly JSON-2 method -- as the service identity; and each
    change is committed by the superuser on a third connection, from inside the call, through the
    step hooks ``_witness_moves`` (search A), ``_stored_candidates`` (search B) and ``_verify``.

    Every change is an ordinary ORM write; nothing is staled in SQL.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env.ref("base.main_company")
            warehouse = env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
            picking_type = env["stock.picking.type"].create(
                {
                    "name": "Window Snapshot Out",
                    "code": "outgoing",
                    "sequence_code": "WSNAP",
                    "company_id": company.id,
                    "warehouse_id": warehouse.id,
                }
            )
            products = env["product.product"].create(
                [{"name": "Agent Snapshot Widget"}, {"name": "Agent Snapshot Early Line"}]
            )
            groups = [
                env.ref("base.group_user").id,
                env.ref("stock.group_stock_user").id,
                env.ref(SERVICE_GROUP).id,
            ]
            user = env["res.users"].create(
                {
                    "name": "agent.window.snapshot",
                    "login": "agent.window.snapshot",
                    "group_ids": [(6, 0, groups)],
                    "company_id": company.id,
                    "company_ids": [(6, 0, [company.id])],
                }
            )
            stock = env["stock.location"].search(
                [("usage", "=", "internal"), ("company_id", "=", company.id)], limit=1
            )
            cls.company_id = company.id
            cls.type_id = picking_type.id
            cls.product_id, cls.early_product_id = products.ids
            cls.user_id = user.id
            cls.stock_id = stock.id
            cls.customers_id = env.ref("stock.stock_location_customers").id
        cls.addClassCleanup(cls._delete_fixtures)

    @classmethod
    def _delete_fixtures(cls):
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            cls._delete_pickings(env)
            env["res.users"].browse(cls.user_id).unlink()
            env["stock.picking.type"].browse(cls.type_id).unlink()
            env["product.product"].browse([cls.product_id, cls.early_product_id]).unlink()

    @classmethod
    def _delete_pickings(cls, env):
        env.cr.execute(
            "DELETE FROM stock_move WHERE picking_id IN "
            "(SELECT id FROM stock_picking WHERE picking_type_id = %s)",
            [cls.type_id],
        )
        env.cr.execute("DELETE FROM stock_picking WHERE picking_type_id = %s", [cls.type_id])

    def setUp(self):
        super().setUp()
        self.addCleanup(self.committed, self._delete_pickings)

    # -- the second connection ------------------------------------------------------------

    def committed(self, change):
        """Run ``change(env)`` as the superuser on a connection of its own, and commit it."""

        with self.registry.cursor() as cr:
            return change(api.Environment(cr, SUPERUSER_ID, {}))

    def picking(self, env, moves):
        picking = env["stock.picking"].create(
            {
                "picking_type_id": self.type_id,
                "location_id": self.stock_id,
                "location_dest_id": self.customers_id,
            }
        )
        for product_id, state, date in moves:
            self.add_move(env, picking, product_id, state, date)
        return picking

    def add_move(self, env, picking, product_id, state, date):
        move = env["stock.move"].create(
            {
                "picking_id": picking.id,
                "product_id": product_id,
                "product_uom_qty": 1.0,
                "date": date,
                "company_id": self.company_id,
                "location_id": self.stock_id,
                "location_dest_id": self.customers_id,
            }
        )
        if state != "draft":
            move.write({"state": state})
        return move

    def between(self, step, change):
        """Commit ``change`` from the second connection just before the method's ``step`` runs."""

        model = self.registry[PROJECTION_MODEL]
        original = getattr(model, step)

        def hooked(records, *args):
            self.committed(change)
            return original(records, *args)

        return patch.object(model, step, hooked)

    # -- the call under test --------------------------------------------------------------

    def window(self, isolation=None):
        """One call on a fresh readonly cursor, optionally forced to another isolation level
        before its first statement."""

        cr = self.registry.cursor(readonly=True)
        try:
            if isolation == "read committed":
                cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            env = api.Environment(cr, self.user_id, {})
            return env[PROJECTION_MODEL].agent_projected_picking_window_v1(
                self.company_id, [self.type_id], OPEN, WINDOW[0], WINDOW[1], 200
            )
        finally:
            cr.rollback()
            cr.close()

    def lying_guard(self):
        """The mutation the guard exists to kill: report a snapshot level and a fixed snapshot
        whatever the transaction really does."""

        return patch.object(
            self.registry[PROJECTION_MODEL],
            "_transaction_snapshot",
            lambda records: ("repeatable read", "pretend"),
        )

    def answer(self, response):
        self.assertEqual(response["status"], "covered")
        return (
            [(row["picking_id"], row["disposition"]) for row in response["rows"]],
            response["totals"],
        )

    def assert_refused(self, response, status):
        self.assertEqual(
            (response["status"], response["coverage"], response["totals"], response["rows"]),
            (status, False, False, []),
        )

    # -- the two interleavings ------------------------------------------------------------

    def a_to_b_fixture(self):
        """Between A and B, a draft picking is confirmed and gains an earlier line.

        Before: one draft line in the window, so its stored date is in the window (B) but no open
        move is (not A). After: a confirmed line in the window (A) but an earlier confirmed line
        sets the stored date before the window (not B). It is in the named coverage in *both*
        states. Beside it, an included anchor that never changes."""

        def create(env):
            anchor = self.picking(env, [(self.product_id, "assigned", INSIDE)])
            moving = self.picking(env, [(self.product_id, "draft", INSIDE)])
            return anchor.id, moving.id

        anchor, moving = self.committed(create)

        def change(env):
            picking = env["stock.picking"].browse(moving)
            picking.move_ids.write({"state": "confirmed"})
            self.add_move(env, picking, self.early_product_id, "confirmed", EARLY)

        return anchor, moving, change

    def b_to_verify_fixture(self):
        """Between B and the verification, one picking stops matching and another starts."""

        leaving = self.committed(
            lambda env: self.picking(env, [(self.product_id, "assigned", INSIDE)]).id
        )
        arriving = []

        def change(env):
            env["stock.picking"].browse(leaving).move_ids.write({"date": OUTSIDE})
            arriving.append(self.picking(env, [(self.product_id, "assigned", INSIDE)]).id)

        return leaving, arriving, change

    def test_every_cursor_odoo_opens_reads_one_snapshot_per_transaction(self):
        """Measured, not inferred: the readonly cursor JSON-2 uses, the read/write cursor it falls
        back to, and this runner's test cursor are all ``REPEATABLE READ``; and PostgreSQL keeps a
        ``REPEATABLE READ`` transaction's snapshot across another connection's commit, where
        ``READ COMMITTED`` does not."""

        for readonly in (True, False):
            cr = self.registry.cursor(readonly=readonly)
            try:
                cr.execute(
                    "SELECT current_setting('transaction_isolation'), "
                    "current_setting('transaction_read_only')"
                )
                self.assertEqual(cr.fetchone(), ("repeatable read", "on" if readonly else "off"))
            finally:
                cr.close()
        self.env.cr.execute("SELECT current_setting('transaction_isolation')")
        self.assertEqual(self.env.cr.fetchone()[0], "repeatable read")

        count = (
            "SELECT pg_current_snapshot()::text, count(*) FROM stock_picking "
            "WHERE picking_type_id = %s"
        )
        for isolation, stable in ((None, True), ("read committed", False)):
            cr = self.registry.cursor(readonly=True)
            try:
                if isolation:
                    cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                cr.execute(count, [self.type_id])
                before = cr.fetchone()
                self.committed(lambda env: self.picking(env, []))
                cr.execute(count, [self.type_id])
                after = cr.fetchone()
            finally:
                cr.close()
            self.assertEqual(before == after, stable, isolation)
            self.assertEqual(after[1], before[1] if stable else before[1] + 1)

    def test_a_commit_between_search_a_and_search_b_is_not_seen(self):
        anchor, _moving, change = self.a_to_b_fixture()
        before = self.answer(self.window())
        self.assertEqual(
            before,
            (
                [(anchor, "included")],
                {"candidates": 2, "included": 1, "refused": 0, "excluded": 1},
            ),
        )
        with self.between("_stored_candidates", change):
            during = self.answer(self.window())
        self.assertEqual(during, before)

    def test_a_commit_between_search_b_and_the_verification_is_not_seen(self):
        leaving, arriving, change = self.b_to_verify_fixture()
        before = self.answer(self.window())
        self.assertEqual(
            before,
            (
                [(leaving, "included")],
                {"candidates": 1, "included": 1, "refused": 0, "excluded": 0},
            ),
        )
        with self.between("_verify", change):
            during = self.answer(self.window())
        self.assertEqual(during, before)
        # The change really was committed: the next call is the other state, whole.
        self.assertEqual(
            self.answer(self.window()),
            (
                [(arriving[0], "included")],
                {"candidates": 1, "included": 1, "refused": 0, "excluded": 0},
            ),
        )

    def test_without_a_snapshot_a_commit_between_a_and_b_misses_a_covered_picking(self):
        """The hazard, shown. The moving picking is in the named coverage before and after the
        change, and is counted in both states' answers; per-statement snapshots lose it."""

        anchor, _moving, change = self.a_to_b_fixture()
        with self.lying_guard(), self.between("_stored_candidates", change):
            spliced = self.answer(self.window("read committed"))
        after = self.answer(self.window())
        self.assertEqual(
            after,
            (
                [(anchor, "included")],
                {"candidates": 2, "included": 1, "refused": 0, "excluded": 1},
            ),
        )
        self.assertEqual(
            spliced,
            ([(anchor, "included")], {"candidates": 1, "included": 1, "refused": 0, "excluded": 0}),
        )

    def test_without_a_snapshot_a_commit_before_the_verification_splices_two_states(self):
        """The hazard, shown. Both states are consistent -- in each, the stored date equals the date
        recomputed from the moves -- yet the leaving picking comes back ``refused`` with
        ``scheduled-date-conflicting``: a conflict that exists in neither state, so one row was
        assembled from both. The arriving picking, included in the state after, is absent."""

        leaving, arriving, change = self.b_to_verify_fixture()
        with self.lying_guard(), self.between("_verify", change):
            response = self.window("read committed")
        self.assertEqual(
            self.answer(response),
            ([(leaving, "refused")], {"candidates": 1, "included": 0, "refused": 1, "excluded": 0}),
        )
        self.assertEqual(
            response["rows"][0]["statuses"],
            {
                "company": "verified",
                "state": "verified",
                "scheduled_date": "scheduled-date-conflicting",
            },
        )
        # Neither state has that conflict: after the change, one fresh snapshot sees the leaving
        # picking out of the window and the arriving one included.
        self.assertEqual(
            self.answer(self.window()),
            (
                [(arriving[0], "included")],
                {"candidates": 1, "included": 1, "refused": 0, "excluded": 0},
            ),
        )

    def test_read_committed_is_refused_before_the_first_business_read(self):
        def unreachable(records, *args):
            raise AssertionError("a business read ran without a snapshot")

        model = self.registry[PROJECTION_MODEL]
        with (
            patch.object(model, "_types_belong_to_company", unreachable),
            patch.object(model, "_witness_moves", unreachable),
            patch.object(model, "_stored_candidates", unreachable),
            patch.object(model, "_verify", unreachable),
        ):
            self.assert_refused(self.window("read committed"), "snapshot-not-established")

    def test_a_snapshot_that_changes_inside_the_call_is_refused(self):
        """The recheck, on its own: a per-statement level let through, or a transaction boundary
        inside the call at the right level, each refuse the whole request."""

        _leaving, _arriving, change = self.b_to_verify_fixture()
        with (
            patch.object(picking_window_projection, "snapshot_established", lambda level: True),
            self.between("_verify", change),
        ):
            self.assert_refused(self.window("read committed"), "snapshot-changed")

        model = self.registry[PROJECTION_MODEL]
        original = model._verify

        def rolled_back(records, candidates):
            records.env.cr.rollback()
            self.committed(lambda env: self.picking(env, [(self.product_id, "assigned", INSIDE)]))
            return original(records, candidates)

        with patch.object(model, "_verify", rolled_back):
            self.assert_refused(self.window(), "snapshot-changed")


@tagged("post_install", "-at_install")
class TestWindowRpcDispatch(HttpCase, WindowCase):
    ENDPOINT = f"/json/2/{PROJECTION_MODEL}/{METHOD}"

    def api_key_for(self, user):
        return (
            self.env["res.users.apikeys"]
            .with_user(user)
            ._generate(None, f"itest-{user.login}", datetime.now() + timedelta(days=1))
        )

    def call(self, user, **overrides):
        body = {
            "company_id": self.company_a.id,
            "picking_type_ids": [self.type_a.id],
            "states": OPEN,
            "scheduled_from": WINDOW[0],
            "scheduled_to": WINDOW[1],
            "max_rows": 10,
            **overrides,
        }
        self.env.flush_all()
        return self.url_open(
            self.ENDPOINT,
            data=json.dumps(body),
            headers={
                "Authorization": f"Bearer {self.api_key_for(user)}",
                "Content-Type": "application/json",
            },
        )

    def test_an_ordinary_internal_user_is_refused_over_rpc(self):
        response = self.call(self.ordinary_user)
        self.assertEqual(response.status_code, 403)
        self.assertIn("requires-the-service-group", response.json()["message"])

    def test_the_service_identity_gets_a_complete_page_over_rpc(self):
        included = self.picking(self.type_a, [("assigned", INSIDE, {})])
        refused = self.picking(self.type_a, [("confirmed", INSIDE, {})])
        response = self.call(self.service_user)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "covered")
        self.assertIs(payload["absence_proven"], False)
        self.assertEqual(
            [(row["picking_id"], row["disposition"]) for row in payload["rows"]],
            sorted([(included.id, "included"), (refused.id, "refused")]),
        )

    def test_a_whole_request_refusal_over_rpc_is_a_200_with_no_rows(self):
        response = self.call(self.service_user, company_id=self.company_b.id)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            (payload["status"], payload["totals"], payload["rows"]),
            ("company-not-allowed", False, []),
        )

    def test_a_cursor_is_not_accepted_over_rpc(self):
        """Measured: JSON-2 answers an unknown argument with 422, and nothing runs."""

        response = self.call(self.service_user, after_picking_id=0)
        self.assertEqual(response.status_code, 422)

    def test_the_grammar_is_enforced_over_rpc(self):
        response = self.call(self.service_user, states=["draft"])
        self.assertEqual(response.status_code, 422)
        self.assertIn("state-not-an-open-picking-state", response.json()["message"])

    def test_the_rpc_request_reads_one_repeatable_read_snapshot_in_the_test_transaction(self):
        """Measured in this runner: the JSON-2 request reads ``REPEATABLE READ`` and the same
        snapshot at both checks -- on the *test's own* connection. ``HttpCase`` serves requests
        through a ``TestCursor`` over the test transaction, so these HTTP tests prove dispatch and
        authorization, not isolation from other connections; that is
        :class:`TestWindowSnapshotIsolation`'s job."""

        model = self.registry[PROJECTION_MODEL]
        original = model._transaction_snapshot
        seen = []

        def spy(records):
            result = original(records)
            records.env.cr.execute("SELECT pg_backend_pid()")
            seen.append((*result, records.env.cr.fetchone()[0]))
            return result

        self.picking(self.type_a, [("assigned", INSIDE, {})])
        with patch.object(model, "_transaction_snapshot", spy):
            response = self.call(self.service_user)
        self.assertEqual((response.status_code, response.json()["status"]), (200, "covered"))
        (level, first, backend), (_, second, again) = seen
        self.assertEqual((level, second, again), ("repeatable read", first, backend))
        self.env.cr.execute("SELECT pg_backend_pid()")
        self.assertEqual(backend, self.env.cr.fetchone()[0])

    def test_an_unauthenticated_request_is_refused(self):
        response = self.url_open(
            self.ENDPOINT, data=json.dumps({}), headers={"Content-Type": "application/json"}
        )
        self.assertIn(response.status_code, (401, 403))
