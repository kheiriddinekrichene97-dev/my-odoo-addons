"""Integration tests for the ``picking_scheduled_date_v1`` checked projection, on a real Odoo 19 DB.

Run by Odoo's own runner, with real record rules, real ACLs, real multi-company behaviour, the real
RPC dispatcher and -- the point of most of these -- **Odoo's own ``_compute_scheduled_date`` as the
golden reference**. Every "verified" case asserts the projection's answer equals the value Odoo
computed and stored, so a divergence between our recomputation and Odoo's is a failing test here
rather than a silent change in which transfers the shadow workflow considers overdue.

These complement the fake-ORM suite in ``unit_tests/``. Where the two disagree, the ORM is right.

Hidden moves are produced the way a deployment would produce them: an ``ir.rule`` on ``stock.move``
scoped to the service group. Adding a restrictive rule in a disposable test database narrows access;
it weakens nothing.
"""

import json
from datetime import datetime, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models import picking_scheduled_date_projection

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
PROJECTION_MODEL = "odoo.agent.picking.scheduled.date.projection"
METHOD = "agent_projected_picking_scheduled_date_v1"

T0 = datetime(2026, 10, 1, 8, 0, 0)


def at(hours):
    return T0 + timedelta(hours=hours)


def wire(value):
    """Odoo's naive-UTC datetime wire format, as the projection renders it."""

    return value.strftime("%Y-%m-%d %H:%M:%S")


class ScheduledDateCase(TransactionCase):
    """Two companies, an operation type in each, and three identities. Pickings are per test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent Sched Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent Sched Beta"})
        cls.type_a = cls._operation_type(cls.company_a, "Sched Alpha In")
        cls.type_b = cls._operation_type(cls.company_b, "Sched Beta In")
        cls.product = cls.env["product.product"].create({"name": "Agent Sched Widget"})
        cls.secret_product = cls.env["product.product"].create({"name": "Agent Sched Secret"})

        cls.service_user = cls._user("agent.sched.service", [cls.company_a], service=True)
        cls.ordinary_user = cls._user("agent.sched.ordinary", [cls.company_a], service=False)
        cls.both_companies_user = cls._user(
            "agent.sched.both", [cls.company_a, cls.company_b], service=True
        )

    # -- fixture helpers ----------------------------------------------------------------

    @classmethod
    def _operation_type(cls, company, name):
        warehouse = cls.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
        return cls.env["stock.picking.type"].create(
            {
                "name": name,
                "code": "incoming",
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

    def picking(self, picking_type, dates, *, move_type="direct", product=None):
        """A picking with one open move per date. Odoo computes its stored scheduled date."""

        company = picking_type.company_id
        stock = self.env["stock.location"].search(
            [("usage", "=", "internal"), ("company_id", "in", [company.id, False])], limit=1
        )
        customers = self.env.ref("stock.stock_location_customers")
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": stock.id,
                "location_dest_id": customers.id,
                "move_type": move_type,
            }
        )
        for date in dates:
            self.move(picking, date, product=product)
        return picking

    def move(self, picking, date, *, product=None):
        return self.env["stock.move"].create(
            {
                "picking_id": picking.id,
                "product_id": (product or self.product).id,
                "product_uom_qty": 1.0,
                "date": date,
                "company_id": picking.company_id.id,
                "location_id": picking.location_id.id,
                "location_dest_id": picking.location_dest_id.id,
            }
        )

    def hide_moves_of(self, product):
        """Hide every move of ``product`` from the service group, with a real record rule."""

        self.env["ir.rule"].create(
            {
                "name": "agent test: hide one product's moves from the service identity",
                "model_id": self.env["ir.model"]._get_id("stock.move"),
                "groups": [(6, 0, [self.env.ref(SERVICE_GROUP).id])],
                "domain_force": f"[('product_id', '!=', {product.id})]",
            }
        )

    def sql(self, statement, params):
        """Manufacture a state the ORM will not produce, then drop every cached value."""

        self.env.flush_all()
        self.env.cr.execute(statement, params)
        self.env.invalidate_all()

    # -- invocation ---------------------------------------------------------------------

    def project(self, user, picking_ids, companies=None):
        """Call the projection as ``user``, never as superuser."""

        env = self.env(user=user, su=False)
        if companies is not None:
            env = env(context=dict(env.context, allowed_company_ids=[c.id for c in companies]))
        return env[PROJECTION_MODEL].agent_projected_picking_scheduled_date_v1(picking_ids)

    def rows(self, response):
        return [
            (row["picking_id"], row["status"], row["scheduled_date"]) for row in response["results"]
        ]

    def assert_golden(self, picking, user=None):
        """The projection verified the picking, and its answer is exactly Odoo's own value."""

        self.env.flush_all()
        response = self.project(user or self.service_user, [picking.id])
        odoo_value = picking.scheduled_date
        self.assertEqual(self.rows(response), [(picking.id, "verified", wire(odoo_value))])
        return odoo_value


@tagged("post_install", "-at_install")
class TestScheduledDateAuthorization(ScheduledDateCase):
    """Same gate as ``picking_company_v1``: an unassigned group checked in the method body."""

    def test_an_ordinary_internal_user_is_refused(self):
        picking = self.picking(self.type_a, [at(1)])
        with self.assertRaises(AccessError) as refusal:
            self.project(self.ordinary_user, [picking.id])
        self.assertIn("requires-the-service-group", str(refusal.exception))

    def test_the_service_identity_is_allowed(self):
        picking = self.picking(self.type_a, [at(1)])
        self.assert_golden(picking)

    def test_superuser_mode_is_refused(self):
        picking = self.picking(self.type_a, [at(1)])
        with self.assertRaises(UserError) as refusal:
            self.env[PROJECTION_MODEL].sudo().agent_projected_picking_scheduled_date_v1(
                [picking.id]
            )
        self.assertIn("refuses-superuser-mode", str(refusal.exception))

    def test_superuser_refusal_precedes_the_group_check(self):
        picking = self.picking(self.type_a, [at(1)])
        with self.assertRaises(UserError) as refusal:
            self.env(user=self.ordinary_user)[
                PROJECTION_MODEL
            ].sudo().agent_projected_picking_scheduled_date_v1([picking.id])
        self.assertIn("refuses-superuser-mode", str(refusal.exception))


@tagged("post_install", "-at_install")
class TestScheduledDateGolden(ScheduledDateCase):
    """Recomputation against Odoo's own ``_compute_scheduled_date``, case by case."""

    def test_as_soon_as_possible_is_the_earliest_open_move(self):
        picking = self.picking(self.type_a, [at(5), at(2), at(9)], move_type="direct")
        self.assertEqual(self.assert_golden(picking), at(2))

    def test_when_all_ready_is_the_latest_open_move(self):
        picking = self.picking(self.type_a, [at(5), at(2), at(9)], move_type="one")
        self.assertEqual(self.assert_golden(picking), at(9))

    def test_done_and_cancelled_moves_do_not_contribute(self):
        picking = self.picking(self.type_a, [at(5)], move_type="direct")
        self.move(picking, at(1)).write({"state": "done"})
        self.move(picking, at(2)).write({"state": "cancel"})
        self.assertEqual(self.assert_golden(picking), at(5))

    def test_changing_the_shipping_policy_is_followed(self):
        picking = self.picking(self.type_a, [at(3), at(7)], move_type="direct")
        self.assertEqual(self.assert_golden(picking), at(3))
        picking.write({"move_type": "one"})
        self.assertEqual(self.assert_golden(picking), at(7))

    def test_setting_the_scheduled_date_through_odoos_inverse_is_followed(self):
        """``_set_scheduled_date`` writes the date onto every move, so it stays recomputable."""

        picking = self.picking(self.type_a, [at(3), at(7)])
        picking.write({"scheduled_date": at(12)})
        self.assertEqual(self.assert_golden(picking), at(12))

    def test_a_picking_without_moves_is_not_recomputable(self):
        """Odoo keeps its creation default; there is nothing visible to recompute it from."""

        picking = self.picking(self.type_a, [])
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "no-open-moves", False)])

    def test_a_picking_whose_moves_are_all_closed_is_not_recomputable(self):
        """Odoo's fallback is the stored value itself -- exactly what §6 forbids forwarding."""

        picking = self.picking(self.type_a, [at(4)])
        picking.move_ids.write({"state": "done"})
        self.env.flush_all()
        self.assertTrue(picking.scheduled_date, "Odoo still holds a stored value to fall back on")
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "no-open-moves", False)])


@tagged("post_install", "-at_install")
class TestScheduledDateVisibility(ScheduledDateCase):
    """Hidden, missing and unreadable dependencies, each through real access control."""

    def test_a_picking_in_another_company_is_not_visible(self):
        picking = self.picking(self.type_b, [at(1)])
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "picking-not-visible", False)])

    def test_a_missing_picking_is_indistinguishable_from_a_hidden_one(self):
        hidden = self.picking(self.type_b, [at(1)])
        missing = self.project(self.service_user, [2_000_000_000])["results"][0]
        refused = self.project(self.service_user, [hidden.id])["results"][0]
        self.assertEqual(missing["status"], refused["status"])
        self.assertEqual(missing["scheduled_date"], False)

    def test_a_visible_picking_whose_operation_type_is_hidden_fails_closed(self):
        """``move_type`` was computed through an operation type this caller cannot see."""

        picking = self.picking(self.type_b, [at(1)])
        self.sql(
            "UPDATE stock_picking SET company_id = %s WHERE id = %s",
            (self.company_a.id, picking.id),
        )
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "operation-type-not-visible", False)])

    def test_a_hidden_move_holding_the_extremum_fails_closed(self):
        """The case the projection exists for.

        Odoo's elevated compute took the earliest date from a move the service identity cannot read.
        Recomputed over the visible moves the date is later, the tripwire trips, and no date -- in
        particular not the elevated one -- is returned.
        """

        picking = self.picking(self.type_a, [at(6)])
        self.move(picking, at(1), product=self.secret_product)
        self.hide_moves_of(self.secret_product)
        self.env.flush_all()
        self.assertEqual(picking.scheduled_date, at(1), "Odoo aggregated the hidden move")

        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "scheduled-date-conflicting", False)])

    def test_a_hidden_move_that_is_not_the_extremum_does_not_change_the_answer(self):
        """The documented limit, pinned so it cannot be overstated in either direction.

        Without elevation a restricted identity cannot count rows its rules hide. A hidden move
        that does not hold the extremum is therefore undetectable -- and the answer is the date of a
        move the caller *can* read, which is the value Odoo computed.
        """

        picking = self.picking(self.type_a, [at(2)])
        self.move(picking, at(8), product=self.secret_product)
        self.hide_moves_of(self.secret_product)
        self.assertEqual(self.assert_golden(picking), at(2))

    def test_a_picking_whose_every_open_move_is_hidden_is_not_recomputable(self):
        picking = self.picking(self.type_a, [at(3)], product=self.secret_product)
        self.hide_moves_of(self.secret_product)
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "no-open-moves", False)])

    def test_an_unreadable_move_date_fails_closed(self):
        """Field-level access is not row-level access, and an ``AccessError`` is an answer."""

        picking = self.picking(self.type_a, [at(3)])
        self.patch(self.env["stock.move"]._fields["date"], "groups", "base.group_system")
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "moves-not-readable", False)])

    def test_an_unreadable_shipping_policy_fails_closed(self):
        picking = self.picking(self.type_a, [at(3)])
        self.patch(self.env["stock.picking"]._fields["move_type"], "groups", "base.group_system")
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "move-type-unresolved", False)])


@tagged("post_install", "-at_install")
class TestScheduledDateDivergence(ScheduledDateCase):
    """Stored values Odoo would not produce, and inputs outside the reviewed vocabulary."""

    def test_a_divergent_stored_date_is_refused_not_reconciled(self):
        picking = self.picking(self.type_a, [at(3)])
        self.sql(
            "UPDATE stock_picking SET scheduled_date = %s WHERE id = %s", (at(-48), picking.id)
        )
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "scheduled-date-conflicting", False)])
        self.assertNotEqual(response["results"][0]["scheduled_date"], wire(at(-48)))

    def test_an_unrecognised_move_state_is_refused(self):
        """Odoo's compute would treat it as open. A state outside the review is a divergence."""

        picking = self.picking(self.type_a, [at(3)])
        self.sql("UPDATE stock_move SET state = 'agent_bogus' WHERE picking_id = %s", (picking.id,))
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "move-state-unrecognised", False)])

    def test_an_unrecognised_shipping_policy_is_refused(self):
        picking = self.picking(self.type_a, [at(3)])
        self.sql("UPDATE stock_picking SET move_type = 'agent_bogus' WHERE id = %s", (picking.id,))
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "move-type-unresolved", False)])

    def test_a_move_set_over_the_bound_is_refused_and_one_at_the_bound_is_not(self):
        """A truncated set might be missing the extremum, so it has no honest min or max."""

        self.patch(picking_scheduled_date_projection, "MAX_MOVES_PER_PICKING", 2)
        at_bound = self.picking(self.type_a, [at(4), at(2)])
        over = self.picking(self.type_a, [at(4), at(2), at(1)])
        self.env.flush_all()
        response = self.project(self.service_user, [at_bound.id, over.id])
        self.assertEqual(
            self.rows(response),
            [
                (at_bound.id, "verified", wire(at(2))),
                (over.id, "moves-over-bound", False),
            ],
        )


@tagged("post_install", "-at_install")
class TestScheduledDateCompanies(ScheduledDateCase):
    """Multi-company: the active set decides, and each picking answers for itself."""

    def test_a_company_outside_the_active_set_is_refused(self):
        picking = self.picking(self.type_b, [at(1)])
        response = self.project(self.both_companies_user, [picking.id], companies=[self.company_a])
        self.assertEqual(self.rows(response), [(picking.id, "picking-not-visible", False)])

    def test_both_companies_active_resolves_each_picking(self):
        alpha = self.picking(self.type_a, [at(2), at(5)], move_type="direct")
        beta = self.picking(self.type_b, [at(2), at(5)], move_type="one")
        self.env.flush_all()
        response = self.project(
            self.both_companies_user,
            [alpha.id, beta.id],
            companies=[self.company_a, self.company_b],
        )
        self.assertEqual(
            self.rows(response),
            [(alpha.id, "verified", wire(at(2))), (beta.id, "verified", wire(at(5)))],
        )

    def test_a_move_in_an_inactive_company_is_hidden_by_odoos_own_rule(self):
        """No test rule here: ``stock.move``'s standard multi-company rule does the hiding.

        The move is forced into company B on a company-A picking -- a crossover ``check_company``
        refuses through the ORM, so it is manufactured in SQL. Odoo's elevated compute still counts
        it; the caller, with only company A active, cannot read it.
        """

        picking = self.picking(self.type_a, [at(6)])
        crossover = self.move(picking, at(1))
        self.sql(
            "UPDATE stock_move SET company_id = %s WHERE id = %s", (self.company_b.id, crossover.id)
        )
        self.assertEqual(picking.scheduled_date, at(1))
        response = self.project(self.both_companies_user, [picking.id], companies=[self.company_a])
        self.assertEqual(self.rows(response), [(picking.id, "scheduled-date-conflicting", False)])

    def test_a_mixed_request_answers_per_picking(self):
        alpha = self.picking(self.type_a, [at(2)])
        beta = self.picking(self.type_b, [at(2)])
        empty = self.picking(self.type_a, [])
        self.env.flush_all()
        response = self.project(self.service_user, [alpha.id, beta.id, empty.id, alpha.id])
        self.assertEqual(
            self.rows(response),
            [
                (alpha.id, "verified", wire(at(2))),
                (beta.id, "picking-not-visible", False),
                (empty.id, "no-open-moves", False),
            ],
        )


@tagged("post_install", "-at_install")
class TestScheduledDateBounds(ScheduledDateCase):
    """The argument grammar and the closed response shape."""

    def test_the_id_bound_is_enforced(self):
        with self.assertRaises(UserError) as refusal:
            self.project(self.service_user, list(range(1, 202)))
        self.assertIn("picking-ids-over-bound", str(refusal.exception))

    def test_a_non_integer_id_is_refused(self):
        for bad in ("1", 1.0, True, -1, 0, None):
            with self.assertRaises(UserError):
                self.project(self.service_user, [bad])

    def test_an_empty_request_is_refused(self):
        with self.assertRaises(UserError):
            self.project(self.service_user, [])

    def test_the_response_envelope_is_closed(self):
        alpha = self.picking(self.type_a, [at(2)])
        beta = self.picking(self.type_b, [at(2)])
        response = self.project(self.service_user, [alpha.id, beta.id])
        self.assertEqual(list(response), ["schema_version", "projection", "results"])
        self.assertEqual(response["schema_version"], 1)
        self.assertEqual(response["projection"], "picking_scheduled_date_v1")
        for row in response["results"]:
            self.assertEqual(list(row), ["picking_id", "status", "scheduled_date"])
            self.assertIsInstance(row["scheduled_date"], (str, bool))


@tagged("post_install", "-at_install")
class TestScheduledDateRpcDispatch(HttpCase, ScheduledDateCase):
    """The real dispatcher, over real HTTP, with real API-key authentication."""

    ENDPOINT = f"/json/2/{PROJECTION_MODEL}/{METHOD}"

    def api_key_for(self, user):
        return (
            self.env["res.users.apikeys"]
            .with_user(user)
            ._generate(None, f"itest-{user.login}", datetime.now() + timedelta(days=1))
        )

    def call(self, user, picking_ids):
        return self.url_open(
            self.ENDPOINT,
            data=json.dumps({"picking_ids": picking_ids}),
            headers={
                "Authorization": f"Bearer {self.api_key_for(user)}",
                "Content-Type": "application/json",
            },
        )

    def test_an_ordinary_internal_user_is_refused_over_rpc(self):
        picking = self.picking(self.type_a, [at(1)])
        response = self.call(self.ordinary_user, [picking.id])
        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["name"], "odoo.exceptions.AccessError")
        self.assertIn("requires-the-service-group", payload["message"])

    def test_the_service_identity_is_served_odoos_own_value_over_rpc(self):
        picking = self.picking(self.type_a, [at(5), at(2)], move_type="direct")
        self.env.flush_all()
        response = self.call(self.service_user, [picking.id])
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["projection"], "picking_scheduled_date_v1")
        self.assertEqual(
            payload["results"],
            [
                {
                    "picking_id": picking.id,
                    "status": "verified",
                    "scheduled_date": wire(picking.scheduled_date),
                }
            ],
        )

    def test_a_hidden_extremal_move_fails_closed_over_rpc(self):
        picking = self.picking(self.type_a, [at(6)])
        self.move(picking, at(1), product=self.secret_product)
        self.hide_moves_of(self.secret_product)
        self.env.flush_all()
        response = self.call(self.service_user, [picking.id])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"],
            [
                {
                    "picking_id": picking.id,
                    "status": "scheduled-date-conflicting",
                    "scheduled_date": False,
                }
            ],
        )

    def test_the_rpc_path_still_enforces_the_bounds(self):
        response = self.call(self.service_user, list(range(1, 202)))
        self.assertEqual(response.status_code, 422)
        self.assertIn("picking-ids-over-bound", response.json()["message"])

    def test_an_unauthenticated_request_is_refused(self):
        response = self.url_open(
            self.ENDPOINT,
            data=json.dumps({"picking_ids": [1]}),
            headers={"Content-Type": "application/json"},
        )
        self.assertIn(response.status_code, (401, 403))
