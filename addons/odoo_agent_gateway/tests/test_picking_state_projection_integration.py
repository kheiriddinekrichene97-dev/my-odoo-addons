"""Integration tests for the ``picking_state_v1`` checked projection, on a real Odoo 19 database.

Two claims are tested here against Odoo itself.

**The port agrees with Odoo wherever it answers.** Every ``verified`` answer is compared with the
state Odoo's own ``_compute_state`` produces, freshly forced.

**The answer never depends on an elevated location.** ``stock.picking.location_id`` and
``stock.move.location_dest_id`` are elevated stored computes, and the projection reads neither. The
provenance matrix builds each move configuration several times with *identical caller-visible move
inputs* but a different source (a reservation-bypassing supplier location, or an internal one) and,
when the configuration has closed moves, a different destination (scrapped or not). It then
requires the projection to give the **same** answer for every variant, and to give a state exactly
when Odoo's own answers agree across the variants -- a location-dependent refusal otherwise. The
named provenance classes do the same through the mechanisms that actually choose those ids: a
partner's company-dependent stock properties hidden from the caller, the global ``ir.default``
those properties are compared against, and a route's final location.

The remaining classes run the other refusals through real access control, as in the other two
suites.
"""

import json
from datetime import datetime, timedelta
from itertools import combinations_with_replacement

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models import picking_state_projection

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
PROJECTION_MODEL = "odoo.agent.picking.state.projection"
METHOD = "agent_projected_picking_state_v1"

MOVE_STATES = (
    "draft",
    "waiting",
    "confirmed",
    "partially_available",
    "assigned",
    "done",
    "cancel",
)
OPEN_MOVE_STATES = ("waiting", "confirmed", "partially_available", "assigned")
CLOSED_MOVE_STATES = ("done", "cancel")
POLICIES = ("direct", "one")
MTO = {"procure_method": "make_to_order"}
LOCATION_DEPENDENT = ("source-location-dependent", "destination-location-dependent")


def up_to_two(items):
    """Every one-element and every unordered two-element combination, repetition allowed."""

    return [(item,) for item in items] + list(combinations_with_replacement(items, 2))


class StateCase(TransactionCase):
    """Two companies, operation types in each, and three identities. Pickings are per test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent State Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent State Beta"})
        cls.type_a = cls._operation_type(cls.company_a, "State Alpha Out")
        cls.type_b = cls._operation_type(cls.company_b, "State Beta Out")
        cls.product = cls.env["product.product"].create({"name": "Agent State Widget"})
        cls.secret_product = cls.env["product.product"].create({"name": "Agent State Secret"})
        cls.customers = cls.env.ref("stock.stock_location_customers")
        cls.suppliers = cls.env.ref("stock.stock_location_suppliers")

        cls.service_user = cls._user("agent.state.service", [cls.company_a], service=True)
        cls.ordinary_user = cls._user("agent.state.ordinary", [cls.company_a], service=False)
        cls.both_companies_user = cls._user(
            "agent.state.both", [cls.company_a, cls.company_b], service=True
        )

    # -- fixture helpers ----------------------------------------------------------------

    @classmethod
    def _operation_type(cls, company, name, code="outgoing", **values):
        warehouse = cls.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
        return cls.env["stock.picking.type"].create(
            {
                "name": name,
                "code": code,
                "sequence_code": name.replace(" ", "")[:5],
                "company_id": company.id,
                "warehouse_id": warehouse.id if warehouse else False,
                **values,
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

    def internal(self, company):
        return self.env["stock.location"].search(
            [("usage", "=", "internal"), ("company_id", "=", company.id)], limit=1
        )

    def scrap(self, company):
        location = self.env["stock.location"].search(
            [("usage", "=", "inventory"), ("company_id", "in", [company.id, False])], limit=1
        )
        self.assertTrue(location, "Odoo creates an inventory-usage location per company")
        return location

    def add_move(self, picking, state, demand=1.0, product=None, **values):
        """One move on ``picking``, created in draft and then written to ``state``.

        The move's *source* is the picking's, passed explicitly because Odoo does not precompute it
        on this path; ``_compute_state`` never reads a move's own source. The move's
        *destination* -- which ``_compute_state`` does read -- is left to Odoo's own compute unless
        ``values`` names it, so it is chosen exactly as it would be for a user.
        """

        move = self.env["stock.move"].create(
            {
                "picking_id": picking.id,
                "product_id": (product or self.product).id,
                "product_uom_qty": demand,
                "company_id": picking.company_id.id,
                "location_id": picking.location_id.id,
                **values,
            }
        )
        if state != "draft":
            move.write({"state": state})
        return move

    def picking(self, picking_type, moves, *, move_type="direct", source=None, **values):
        """A picking whose moves are ``(state, demand, extra move values)`` triples."""

        company = picking_type.company_id
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": (source or self.internal(company)).id,
                "location_dest_id": self.customers.id,
                "move_type": move_type,
                **values,
            }
        )
        for state, demand, extra in moves:
            self.add_move(picking, state, demand, **extra)
        return picking

    def odoo_state(self, picking):
        """The state Odoo's own ``_compute_state`` produces **now**, forced fresh."""

        self.env.flush_all()
        picking.invalidate_recordset(["state"])
        self.env.add_to_compute(picking._fields["state"], picking)
        self.env.flush_all()
        return picking.state

    def hide_from_service(self, model, domain):
        """Hide rows of ``model`` from the service group with a real record rule."""

        self.env["ir.rule"].create(
            {
                "name": f"agent test: hide {model} rows from the service identity",
                "model_id": self.env["ir.model"]._get_id(model),
                "groups": [(6, 0, [self.env.ref(SERVICE_GROUP).id])],
                "domain_force": domain,
            }
        )

    def sql(self, statement, params):
        """Manufacture a state the ORM will not produce, then drop every cached value."""

        self.env.flush_all()
        self.env.cr.execute(statement, params)
        self.env.invalidate_all()

    # -- invocation ---------------------------------------------------------------------

    def project(self, user, picking_ids, companies=None):
        env = self.env(user=user, su=False)
        if companies is not None:
            env = env(context=dict(env.context, allowed_company_ids=[c.id for c in companies]))
        return env[PROJECTION_MODEL].agent_projected_picking_state_v1(picking_ids)

    def rows(self, response):
        return [(row["picking_id"], row["status"], row["state"]) for row in response["results"]]

    def answer(self, picking, user=None):
        self.env.flush_all()
        ((_, status, state),) = self.rows(self.project(user or self.service_user, [picking.id]))
        return status, state

    def assert_golden(self, picking, user=None):
        """Verified, and exactly the value Odoo's own compute produces for this picking."""

        expected = self.odoo_state(picking)
        self.assertEqual(self.answer(picking, user), ("verified", expected))
        return expected

    def assert_same_answer_whatever_odoo_says(self, pickings, expected_status):
        """Identical visible inputs, different location provenance: one answer, never Odoo's.

        Asserts Odoo itself disagrees across ``pickings`` -- otherwise the case would prove nothing
        -- and that the projection gives every one of them the same location-dependent refusal.
        """

        odoo = [self.odoo_state(picking) for picking in pickings]
        self.assertGreater(len(set(odoo)), 1, f"Odoo agreed ({odoo}); the case proves nothing")
        for picking in pickings:
            self.assertEqual(self.answer(picking), (expected_status, False))
        return odoo


@tagged("post_install", "-at_install")
class TestStateAuthorization(StateCase):
    """Same gate as the other projections: an unassigned group checked in the method body."""

    def test_an_ordinary_internal_user_is_refused(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        with self.assertRaises(AccessError) as refusal:
            self.project(self.ordinary_user, [picking.id])
        self.assertIn("requires-the-service-group", str(refusal.exception))

    def test_the_service_identity_is_allowed(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.assertEqual(self.assert_golden(picking), "assigned")

    def test_superuser_mode_is_refused(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        with self.assertRaises(UserError) as refusal:
            self.env[PROJECTION_MODEL].sudo().agent_projected_picking_state_v1([picking.id])
        self.assertIn("refuses-superuser-mode", str(refusal.exception))

    def test_superuser_refusal_precedes_the_group_check(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        with self.assertRaises(UserError) as refusal:
            self.env(user=self.ordinary_user)[
                PROJECTION_MODEL
            ].sudo().agent_projected_picking_state_v1([picking.id])
        self.assertIn("refuses-superuser-mode", str(refusal.exception))


@tagged("post_install", "-at_install")
class TestStateProvenanceMatrix(StateCase):
    """Golden against Odoo, and invariant under the provenance of both elevated locations."""

    def check_variants(self, move_type, moves):
        """Build ``moves`` under every relevant location variant; return the one answer.

        Sources: an internal location (no reservation bypass) and the supplier location (bypass).
        Destinations, only when a move is closed: every destination the customers location, or
        every ``done`` move's destination the scrap location. These are one representative per
        side of the only two predicates Odoo applies to the locations, so Odoo's answers differ
        across them exactly when some pair of usages would make them differ.
        """

        closed = any(state in CLOSED_MOVE_STATES for state, _, _ in moves)
        scrap = self.scrap(self.company_a)
        destinations = ("customer", "scrap-done") if closed else ("customer",)
        answers, odoo = set(), set()
        for source in (self.internal(self.company_a), self.suppliers):
            for destination in destinations:
                variant = tuple(
                    (
                        state,
                        demand,
                        {
                            **extra,
                            "location_dest_id": (
                                scrap
                                if destination == "scrap-done" and state == "done"
                                else self.customers
                            ).id,
                        },
                    )
                    for state, demand, extra in moves
                )
                picking = self.picking(self.type_a, variant, move_type=move_type, source=source)
                odoo.add(self.odoo_state(picking))
                answers.add(self.answer(picking))
        self.assertEqual(len(answers), 1, f"identical visible inputs, answers {answers}")
        (answer,) = answers
        if len(odoo) == 1:
            self.assertEqual(answer, ("verified", next(iter(odoo))))
        else:
            all_closed = all(state in CLOSED_MOVE_STATES for state, _, _ in moves)
            expected = LOCATION_DEPENDENT[1] if all_closed else LOCATION_DEPENDENT[0]
            self.assertEqual(answer, (expected, False), f"Odoo gave {odoo}")
        return answer[0] if answer[0] != "verified" else answer[1]

    def run_matrix(self, cases):
        seen = set()
        for move_type, moves in cases:
            with self.subTest(move_type=move_type, moves=moves):
                seen.add(self.check_variants(move_type, moves))
        return seen

    def test_every_one_and_two_move_state_combination_under_both_policies(self):
        """7 + 28 state combinations x 2 policies = 70 cases, each under 2 or 4 variants."""

        seen = self.run_matrix(
            [
                (move_type, tuple((state, 1.0, {}) for state in combination))
                for move_type in POLICIES
                for combination in up_to_two(MOVE_STATES)
            ]
        )
        self.assertTrue({"draft", "cancel", "done", "assigned"} <= seen, seen)
        self.assertTrue(set(LOCATION_DEPENDENT) <= seen, seen)

    def test_zero_and_non_zero_demand_on_open_moves_under_both_policies(self):
        """Open states x {0, 2} demand: 44 combinations x 2 policies = 88 cases, 2 variants each."""

        items = [(state, demand) for state in OPEN_MOVE_STATES for demand in (0.0, 2.0)]
        seen = self.run_matrix(
            [
                (move_type, tuple((state, demand, {}) for state, demand in combination))
                for move_type in POLICIES
                for combination in up_to_two(items)
            ]
        )
        self.assertIn("assigned", seen)
        self.assertIn("source-location-dependent", seen)

    def test_one_make_to_order_move_makes_every_open_picking_location_independent(self):
        """Open state combinations with the first move make-to-order: 14 x 2 policies = 28 cases.

        With a make-to-order move the bypass step can never fire, so the source is irrelevant and
        every case must be answered -- including ``waiting`` and ``confirmed``.
        """

        seen = self.run_matrix(
            [
                (
                    move_type,
                    tuple(
                        (state, 1.0, MTO if index == 0 else {})
                        for index, state in enumerate(combination)
                    ),
                )
                for move_type in POLICIES
                for combination in up_to_two(OPEN_MOVE_STATES)
            ]
        )
        self.assertFalse(set(LOCATION_DEPENDENT) & seen, seen)
        self.assertTrue({"waiting", "confirmed", "assigned"} <= seen, seen)

    def test_a_picking_without_moves_is_draft(self):
        picking = self.picking(self.type_a, [])
        self.assertEqual(self.assert_golden(picking), "draft")


@tagged("post_install", "-at_install")
class TestStateLocationProvenance(StateCase):
    """The mechanisms that choose the two elevated location ids, one at a time.

    Each case builds pickings whose moves are identical to the caller and whose location was chosen
    by something the caller cannot see. Odoo's state differs; the projection's answer must not.
    """

    def receipt_type(self, company):
        return self._operation_type(
            company,
            f"State In {company.name}",
            code="incoming",
            default_location_src_id=self.suppliers.id,
            default_location_dest_id=self.internal(company).id,
        )

    def receipt(self, picking_type, partner=None):
        """A receipt whose source Odoo's ``_compute_location_id`` chooses, not the test."""

        values = {"picking_type_id": picking_type.id}
        if partner is not None:
            values["partner_id"] = partner.id
        return self.env["stock.picking"].create(values)

    def custom_location(self, name, usage, parent=None):
        return self.env["stock.location"].create(
            {
                "name": name,
                "usage": usage,
                "company_id": self.company_a.id,
                **({"location_id": parent.id} if parent else {}),
            }
        )

    def test_a_hidden_partner_supplier_property_moves_odoos_state_but_not_the_projection(self):
        """``property_stock_supplier`` on a partner the service identity cannot read."""

        receipt_type = self.receipt_type(self.company_a)
        vendor_bay = self.custom_location("Agent vendor bay", "internal")
        vendor = self.env["res.partner"].create({"name": "Agent hidden vendor"})
        vendor.with_company(self.company_a).property_stock_supplier = vendor_bay
        self.hide_from_service("res.partner", f"[('id', '!=', {vendor.id})]")
        self.assertFalse(vendor.with_user(self.service_user).has_access("read"))

        with_vendor = self.receipt(receipt_type, vendor)
        without_vendor = self.receipt(receipt_type)
        self.assertEqual(with_vendor.location_id, vendor_bay, "chosen from the hidden property")
        self.assertEqual(without_vendor.location_id, self.suppliers)

        for picking in (with_vendor, without_vendor):
            self.add_move(picking, "confirmed")
        self.assertEqual(
            self.assert_same_answer_whatever_odoo_says(
                [with_vendor, without_vendor], "source-location-dependent"
            ),
            ["confirmed", "assigned"],
        )

        # The branch-independent outcome over the same provenance: a reserved move is `assigned`
        # whatever the source, so both are answered, and identically.
        for picking in (with_vendor, without_vendor):
            picking.move_ids.write({"state": "assigned"})
            self.assertEqual(self.assert_golden(picking), "assigned")

    def test_the_global_ir_default_moves_odoos_state_but_not_the_projection(self):
        """Same partner, same moves; only the ``ir.default`` the property is compared with differs.

        ``_compute_location_id`` keeps the operation type's source when the partner's supplier
        location *equals* ``ir.default.sudo()._get('res.partner', 'property_stock_supplier')``.
        """

        receipt_type = self.receipt_type(self.company_a)
        vendor_bay = self.custom_location("Agent default bay", "internal")
        vendor = self.env["res.partner"].create({"name": "Agent defaulted vendor"})
        vendor.with_company(self.company_a).property_stock_supplier = vendor_bay
        defaults = self.env["ir.default"]

        defaults.set("res.partner", "property_stock_supplier", self.suppliers.id)
        differs = self.receipt(receipt_type, vendor)
        defaults.set("res.partner", "property_stock_supplier", vendor_bay.id)
        matches = self.receipt(receipt_type, vendor)
        self.assertEqual(differs.location_id, vendor_bay)
        self.assertEqual(matches.location_id, self.suppliers, "the default suppressed the property")

        for picking in (differs, matches):
            self.add_move(picking, "waiting")
        self.assertEqual(
            self.assert_same_answer_whatever_odoo_says(
                [differs, matches], "source-location-dependent"
            ),
            ["waiting", "assigned"],
        )

    def test_a_hidden_partner_customer_property_moves_odoos_state_but_not_the_projection(self):
        """``property_stock_customer`` chooses the picking's destination, which the cancelled
        move's ``location_dest_id`` compute copies."""

        delivery_type = self._operation_type(
            self.company_a,
            "State Out Custom",
            default_location_src_id=self.internal(self.company_a).id,
            default_location_dest_id=self.customers.id,
        )
        scrap = self.scrap(self.company_a)
        write_off = self.custom_location("Agent customer write-off", "inventory")
        customer = self.env["res.partner"].create({"name": "Agent hidden customer"})
        customer.with_company(self.company_a).property_stock_customer = write_off
        self.hide_from_service("res.partner", f"[('id', '!=', {customer.id})]")

        with_customer = self.env["stock.picking"].create(
            {"picking_type_id": delivery_type.id, "partner_id": customer.id}
        )
        without_customer = self.env["stock.picking"].create({"picking_type_id": delivery_type.id})
        self.assertEqual(with_customer.location_dest_id, write_off)
        self.assertEqual(without_customer.location_dest_id, self.customers)

        for picking in (with_customer, without_customer):
            self.add_move(picking, "done", location_dest_id=scrap.id)
            cancelled = self.add_move(picking, "cancel")
            self.assertEqual(cancelled.location_dest_id, picking.location_dest_id)
        self.assertEqual(
            self.assert_same_answer_whatever_odoo_says(
                [with_customer, without_customer], "destination-location-dependent"
            ),
            ["done", "cancel"],
        )

    def test_a_routes_final_location_moves_odoos_state_but_not_the_projection(self):
        """``_compute_location_dest_id`` substitutes ``location_final_id`` when it lies under the
        picking's destination -- the route-driven path of the ``#6`` compute."""

        stock = self.internal(self.company_a)
        quarantine = self.custom_location("Agent quarantine", "inventory", parent=stock)
        pickings = []
        for final in (quarantine, None):
            picking = self.picking(self.type_a, [], location_dest_id=stock.id)
            done = self.add_move(
                picking, "done", **({"location_final_id": final.id} if final else {})
            )
            self.assertEqual(done.location_dest_id, final or stock)
            self.add_move(picking, "cancel")
            pickings.append(picking)
        self.assertEqual(
            self.assert_same_answer_whatever_odoo_says(pickings, "destination-location-dependent"),
            ["cancel", "done"],
        )

    def test_only_done_moves_are_done_whatever_their_destinations(self):
        """The branch-independent closed outcome, over the same two destination provenances."""

        stock = self.internal(self.company_a)
        quarantine = self.custom_location("Agent quarantine two", "inventory", parent=stock)
        for final in (quarantine, None):
            picking = self.picking(self.type_a, [], location_dest_id=stock.id)
            self.add_move(picking, "done", **({"location_final_id": final.id} if final else {}))
            self.assertEqual(self.assert_golden(picking), "done")

    def test_hiding_every_location_from_the_caller_changes_no_answer(self):
        """Nothing is read from a location, so hiding them all is invisible to the projection."""

        answered = self.picking(self.type_a, [("assigned", 1.0, {})])
        dependent = self.picking(self.type_a, [("confirmed", 1.0, {})])
        before = (self.answer(answered), self.answer(dependent))
        self.hide_from_service("stock.location", "[(0, '=', 1)]")
        self.assertEqual((self.answer(answered), self.answer(dependent)), before)
        self.assertEqual(before, (("verified", "assigned"), ("source-location-dependent", False)))

    def test_an_unrecognised_location_usage_is_never_read(self):
        source = self.custom_location("Agent odd source", "internal")
        picking = self.picking(self.type_a, [("assigned", 1.0, {})], source=source)
        self.sql("UPDATE stock_location SET usage = 'agent_bogus' WHERE id = %s", (source.id,))
        self.assertEqual(self.answer(picking), ("verified", "assigned"))


@tagged("post_install", "-at_install")
class TestStateVisibility(StateCase):
    """Hidden, missing and unreadable dependencies, each through real access control."""

    def test_a_picking_in_another_company_is_not_visible(self):
        picking = self.picking(self.type_b, [("assigned", 1.0, {})])
        response = self.project(self.service_user, [picking.id])
        self.assertEqual(self.rows(response), [(picking.id, "picking-not-visible", False)])

    def test_a_missing_picking_is_indistinguishable_from_a_hidden_one(self):
        hidden = self.picking(self.type_b, [("assigned", 1.0, {})])
        missing = self.project(self.service_user, [2_000_000_000])["results"][0]
        refused = self.project(self.service_user, [hidden.id])["results"][0]
        self.assertEqual(missing["status"], refused["status"])
        self.assertEqual(missing["state"], False)

    def test_a_visible_picking_whose_operation_type_is_hidden_fails_closed(self):
        picking = self.picking(self.type_b, [("assigned", 1.0, {})])
        self.sql(
            "UPDATE stock_picking SET company_id = %s WHERE id = %s",
            (self.company_a.id, picking.id),
        )
        self.assertEqual(self.answer(picking), ("operation-type-not-visible", False))

    def test_a_hidden_move_that_changes_the_state_fails_closed(self):
        """Odoo counted a draft move the service identity cannot read, so the picking is draft.

        Recomputed over the visible move the picking would be ``assigned``; the tripwire trips and
        no state -- in particular not the elevated ``draft`` -- is returned.
        """

        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.add_move(picking, "draft", product=self.secret_product)
        self.hide_from_service("stock.move", f"[('product_id', '!=', {self.secret_product.id})]")
        self.assertEqual(self.odoo_state(picking), "draft")
        self.assertEqual(self.answer(picking), ("state-conflicting", False))

    def test_a_hidden_move_that_does_not_change_the_state_is_not_detected(self):
        """The documented limit: an irrelevant hidden move is invisible and changes nothing."""

        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.add_move(picking, "assigned", product=self.secret_product)
        self.hide_from_service("stock.move", f"[('product_id', '!=', {self.secret_product.id})]")
        self.assertEqual(self.assert_golden(picking), "assigned")

    def test_an_unreadable_move_demand_fails_closed(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.patch(self.env["stock.move"]._fields["product_uom_qty"], "groups", "base.group_system")
        self.assertEqual(self.answer(picking), ("moves-not-readable", False))

    def test_an_unreadable_shipping_policy_fails_closed(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.patch(self.env["stock.picking"]._fields["move_type"], "groups", "base.group_system")
        self.assertEqual(self.answer(picking), ("move-type-unresolved", False))


@tagged("post_install", "-at_install")
class TestStateDivergence(StateCase):
    """Stale stored values -- manufactured, and by Odoo's own design -- and unreviewed inputs."""

    def test_a_divergent_stored_state_is_refused_not_reconciled(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.sql("UPDATE stock_picking SET state = 'confirmed' WHERE id = %s", (picking.id,))
        self.assertEqual(self.answer(picking), ("state-conflicting", False))

    def test_odoos_incomplete_dependencies_leave_a_stale_state_that_is_refused(self):
        """No SQL: an ordinary ORM write Odoo's ``@api.depends`` does not declare.

        ``_compute_state`` reads ``procure_method`` but depends only on ``move_type``,
        ``move_ids.state`` and ``move_ids.picking_id``. A ``confirmed`` make-to-stock move from the
        supplier location is ``assigned`` by the bypass step; switching it to make-to-order makes
        Odoo's own compute say ``confirmed``, but nothing triggers it, so ``assigned`` stays stored.
        The projection, answering location-independently from the make-to-order move, says
        ``confirmed`` -- and refuses rather than pick between that and the stale ``assigned``.

        (Zeroing a move's demand does *not* produce this: ``stock.move.write`` re-evaluates the
        move itself, its state changes, and the picking is recomputed. Measured.)
        """

        picking = self.picking(self.type_a, [("confirmed", 1.0, {})], source=self.suppliers)
        self.env.flush_all()
        self.assertEqual(picking.state, "assigned")
        picking.move_ids.write({"procure_method": "make_to_order"})
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(picking.move_ids.state, "confirmed")
        self.assertEqual(picking.state, "assigned", "stored state is stale by Odoo's design")
        self.assertEqual(self.answer(picking), ("state-conflicting", False))
        self.assertEqual(self.odoo_state(picking), "confirmed", "Odoo's fresh compute agrees")

    def test_an_unrecognised_move_state_is_refused(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.sql("UPDATE stock_move SET state = 'agent_bogus' WHERE picking_id = %s", (picking.id,))
        self.assertEqual(self.answer(picking), ("move-state-unrecognised", False))

    def test_an_unrecognised_procurement_method_is_refused(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.sql(
            "UPDATE stock_move SET procure_method = 'agent_bogus' WHERE picking_id = %s",
            (picking.id,),
        )
        self.assertEqual(self.answer(picking), ("move-input-unrecognised", False))

    def test_an_unrecognised_shipping_policy_is_refused(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.sql("UPDATE stock_picking SET move_type = 'agent_bogus' WHERE id = %s", (picking.id,))
        self.assertEqual(self.answer(picking), ("move-type-unresolved", False))

    def test_a_move_set_over_the_bound_is_refused_and_one_at_the_bound_is_not(self):
        self.patch(picking_state_projection, "MAX_MOVES_PER_PICKING", 2)
        at_bound = self.picking(self.type_a, [("confirmed", 1.0, {}), ("assigned", 1.0, {})])
        over = self.picking(self.type_a, [("assigned", 1.0, {})] * 3)
        expected = self.odoo_state(at_bound)
        response = self.project(self.service_user, [at_bound.id, over.id])
        self.assertEqual(
            self.rows(response),
            [(at_bound.id, "verified", expected), (over.id, "moves-over-bound", False)],
        )


@tagged("post_install", "-at_install")
class TestStateCompanies(StateCase):
    """Multi-company: the active set decides, and each picking answers for itself."""

    def test_a_company_outside_the_active_set_is_refused(self):
        picking = self.picking(self.type_b, [("assigned", 1.0, {})])
        response = self.project(self.both_companies_user, [picking.id], companies=[self.company_a])
        self.assertEqual(self.rows(response), [(picking.id, "picking-not-visible", False)])

    def test_both_companies_active_resolves_each_picking_against_odoo(self):
        alpha = self.picking(self.type_a, [("assigned", 1.0, {}), ("confirmed", 1.0, {})])
        beta = self.picking(
            self.type_b, [("assigned", 1.0, {}), ("confirmed", 1.0, MTO)], move_type="one"
        )
        expected = (self.odoo_state(alpha), self.odoo_state(beta))
        response = self.project(
            self.both_companies_user,
            [alpha.id, beta.id],
            companies=[self.company_a, self.company_b],
        )
        self.assertEqual(
            self.rows(response),
            [(alpha.id, "verified", expected[0]), (beta.id, "verified", expected[1])],
        )
        self.assertEqual(expected, ("assigned", "confirmed"))

    def test_a_move_in_an_inactive_company_is_hidden_by_odoos_own_rule(self):
        """No test rule: ``stock.move``'s standard multi-company rule hides the crossover move."""

        picking = self.picking(self.type_a, [("assigned", 1.0, {}), ("draft", 1.0, {})])
        draft = picking.move_ids.filtered(lambda move: move.state == "draft")
        self.sql(
            "UPDATE stock_move SET company_id = %s WHERE id = %s", (self.company_b.id, draft.id)
        )
        self.assertEqual(self.odoo_state(picking), "draft")
        response = self.project(self.both_companies_user, [picking.id], companies=[self.company_a])
        self.assertEqual(self.rows(response), [(picking.id, "state-conflicting", False)])

    def test_a_mixed_request_answers_per_picking(self):
        alpha = self.picking(self.type_a, [("assigned", 1.0, {})])
        beta = self.picking(self.type_b, [("assigned", 1.0, {})])
        empty = self.picking(self.type_a, [])
        dependent = self.picking(self.type_a, [("confirmed", 1.0, {})])
        expected = (self.odoo_state(alpha), self.odoo_state(empty))
        response = self.project(
            self.service_user, [alpha.id, beta.id, empty.id, dependent.id, alpha.id]
        )
        self.assertEqual(
            self.rows(response),
            [
                (alpha.id, "verified", expected[0]),
                (beta.id, "picking-not-visible", False),
                (empty.id, "verified", expected[1]),
                (dependent.id, "source-location-dependent", False),
            ],
        )


@tagged("post_install", "-at_install")
class TestStateBounds(StateCase):
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
        alpha = self.picking(self.type_a, [("assigned", 1.0, {})])
        beta = self.picking(self.type_b, [("assigned", 1.0, {})])
        response = self.project(self.service_user, [alpha.id, beta.id])
        self.assertEqual(list(response), ["schema_version", "projection", "results"])
        self.assertEqual(response["schema_version"], 1)
        self.assertEqual(response["projection"], "picking_state_v1")
        for row in response["results"]:
            self.assertEqual(list(row), ["picking_id", "status", "state"])
            self.assertIsInstance(row["state"], (str, bool))


@tagged("post_install", "-at_install")
class TestStateRpcDispatch(HttpCase, StateCase):
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
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        response = self.call(self.ordinary_user, [picking.id])
        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["name"], "odoo.exceptions.AccessError")
        self.assertIn("requires-the-service-group", payload["message"])

    def test_the_service_identity_is_served_odoos_own_value_and_the_refusals_over_rpc(self):
        answered = self.picking(self.type_a, [("assigned", 1.0, {}), ("confirmed", 1.0, {})])
        dependent = self.picking(self.type_a, [("confirmed", 1.0, {})])
        expected = self.odoo_state(answered)
        self.env.flush_all()
        response = self.call(self.service_user, [answered.id, dependent.id])
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["projection"], "picking_state_v1")
        self.assertEqual(
            payload["results"],
            [
                {"picking_id": answered.id, "status": "verified", "state": expected},
                {"picking_id": dependent.id, "status": "source-location-dependent", "state": False},
            ],
        )

    def test_a_hidden_move_that_changes_the_state_fails_closed_over_rpc(self):
        picking = self.picking(self.type_a, [("assigned", 1.0, {})])
        self.add_move(picking, "draft", product=self.secret_product)
        self.hide_from_service("stock.move", f"[('product_id', '!=', {self.secret_product.id})]")
        self.env.flush_all()
        response = self.call(self.service_user, [picking.id])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"],
            [{"picking_id": picking.id, "status": "state-conflicting", "state": False}],
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
