"""Integration tests for the ``quant_unit_v1`` checked projection, on real Odoo 19.

The claims under test, against Odoo itself:

* **The dependency path is what the core assumes.** ``stock.quant.product_uom_id`` is measured to
  be a non-stored, elevated related field on ``product_id.uom_id``, and every hop's effective
  attributes are read from the registry.
* **The elevated value is never the answer.** The unit is re-derived hop by hop as the caller --
  quant, product, template, unit -- and any hop the caller cannot pass refuses that quant. Odoo's
  own elevated read, measured alongside, hands out the unit through a product the caller cannot
  see.
* **What cannot be derived is pinned.** Odoo lets a product's unit change after stock exists, and
  the quant's quantity is not converted; the projection reports the current unit and cannot know.

Hidden rows come from real record rules scoped to the service group, missing rights from revoking
read ACLs inside the test transaction, field restrictions from the ``groups`` attribute, and
inconsistent rows from SQL. Nothing here grants a right.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..projection.quant_unit import GUARDED_FIELDS

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
PROJECTION_MODEL = "odoo.agent.quant.unit.projection"
METHOD = "agent_projected_quant_unit_v1"


class UnitCase(TransactionCase):
    """Two companies and a child of the first, a location each, and four identities."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent Unit Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent Unit Beta"})
        cls.child_a = cls.env["res.company"].create(
            {"name": "Agent Unit Alpha Child", "parent_id": cls.company_a.id}
        )
        cls.location_a = cls._location("Agent Unit Alpha Shelf", cls.company_a)
        cls.location_b = cls._location("Agent Unit Beta Shelf", cls.company_b)
        cls.location_child = cls._location("Agent Unit Child Shelf", cls.child_a)
        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.dozen = cls.env.ref("uom.product_uom_dozen")
        cls.service_user = cls._user("agent.unit.service", [cls.company_a], service=True)
        cls.ordinary_user = cls._user("agent.unit.ordinary", [cls.company_a], service=False)
        cls.both_companies_user = cls._user(
            "agent.unit.both", [cls.company_a, cls.company_b], service=True
        )
        cls.child_user = cls._user("agent.unit.child", [cls.child_a], service=True)

    @classmethod
    def _location(cls, name, company):
        return cls.env["stock.location"].create(
            {"name": name, "usage": "internal", "company_id": company.id}
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

    def product(self, name="Agent Unit Widget", *, unit=None, company=False):
        return self.env["product.product"].create(
            {
                "name": name,
                "type": "consu",
                "is_storable": True,
                "uom_id": (unit or self.unit).id,
                "company_id": company.id if company else False,
            }
        )

    def quant(self, location=None, product=None, quantity=5.0):
        product = product or self.product()
        location = location or self.location_a
        self.env["stock.quant"]._update_available_quantity(product, location, quantity)
        quant = self.env["stock.quant"]._gather(product, location, strict=True)
        self.assertEqual(len(quant), 1)
        return quant

    def hide_from_service(self, model, domain):
        self.env["ir.rule"].create(
            {
                "name": f"agent test: hide {model} rows from the service identity",
                "model_id": self.env["ir.model"]._get_id(model),
                "groups": [(6, 0, [self.env.ref(SERVICE_GROUP).id])],
                "domain_force": domain,
            }
        )

    def revoke_read(self, model, user):
        """Revoke every read ACL on ``model`` that ``user`` holds, in this transaction only."""

        access = self.env["ir.model.access"].search(
            [("model_id.model", "=", model), ("perm_read", "=", True)]
        )
        access.filtered(lambda a: not a.group_id or a.group_id in user.all_group_ids).write(
            {"perm_read": False}
        )
        self.assertFalse(self.env[model].with_user(user).has_access("read"))

    def sql(self, statement, params):
        self.env.flush_all()
        self.env.cr.execute(statement, params)
        self.env.invalidate_all()

    def caller(self, user=None, companies=None):
        self.env.flush_all()
        env = self.env(user=user or self.service_user, su=False)
        if companies is not None:
            env = env(context=dict(env.context, allowed_company_ids=[c.id for c in companies]))
        return env

    def project(self, quants, user=None, *, companies=None):
        ids = quants if isinstance(quants, list) else quants.ids
        return self.caller(user, companies)[PROJECTION_MODEL].agent_projected_quant_unit_v1(ids)

    def only(self, response):
        (row,) = response["results"]
        return row

    def assert_answer(self, row, status, unit=False):
        self.assertEqual((row["status"], row["uom_id"]), (status, unit))


@tagged("post_install", "-at_install")
class TestUnitAuthorization(UnitCase):
    def test_an_ordinary_internal_user_is_refused(self):
        with self.assertRaises(AccessError) as refusal:
            self.project(self.quant(), self.ordinary_user)
        self.assertIn("requires-the-service-group", str(refusal.exception))

    def test_the_service_identity_is_allowed(self):
        self.assert_answer(self.only(self.project(self.quant())), "verified", self.unit.id)

    def test_superuser_mode_is_refused(self):
        with self.assertRaises(UserError) as refusal:
            self.env[PROJECTION_MODEL].sudo().agent_projected_quant_unit_v1(self.quant().ids)
        self.assertIn("refuses-superuser-mode", str(refusal.exception))

    def test_superuser_refusal_precedes_the_group_check(self):
        with self.assertRaises(UserError) as refusal:
            self.env(user=self.ordinary_user)[
                PROJECTION_MODEL
            ].sudo().agent_projected_quant_unit_v1(self.quant().ids)
        self.assertIn("refuses-superuser-mode", str(refusal.exception))


@tagged("post_install", "-at_install")
class TestUnitPinnedSemantics(UnitCase):
    """Measured on the running registry, so a module that redeclares a hop fails here."""

    def test_the_quant_unit_is_a_non_stored_elevated_related_field(self):
        field = self.env["stock.quant"]._fields["product_uom_id"]
        self.assertEqual(
            (field.type, field.comodel_name, field.related, field.store, field.compute_sudo),
            ("many2one", "uom.uom", "product_id.uom_id", False, True),
        )
        self.assertEqual(
            [str(dep) for dep in self.registry.field_depends[field]], ["product_id.uom_id"]
        )

    def test_each_hop_of_the_path(self):
        quant_product = self.env["stock.quant"]._fields["product_id"]
        self.assertEqual(
            (
                quant_product.comodel_name,
                quant_product.store,
                quant_product.required,
                quant_product.ondelete,
                quant_product.check_company,
                quant_product.related,
            ),
            ("product.product", True, True, "restrict", True, None),
        )
        delegated = self.env["product.product"]._fields["uom_id"]
        self.assertEqual(
            (delegated.inherited, delegated.related, delegated.compute_sudo, delegated.store),
            (True, "product_tmpl_id.uom_id", False, False),
        )
        link = self.env["product.product"]._fields["product_tmpl_id"]
        self.assertEqual(
            (link.comodel_name, link.store, link.required, link.ondelete),
            ("product.template", True, True, "cascade"),
        )
        unit = self.env["product.template"]._fields["uom_id"]
        self.assertEqual(
            (unit.comodel_name, unit.store, unit.required, unit.compute, unit.related),
            ("uom.uom", True, True, None, None),
        )
        for model, name in GUARDED_FIELDS:
            with self.subTest(field=f"{model}.{name}"):
                self.assertIsNone(self.env[model]._fields[name].groups)

    def test_verified_units_equal_odoos_own_value_on_consistent_data(self):
        quants = self.quant() | self.quant(product=self.product("Agent Dozens", unit=self.dozen))
        response = self.project(quants)
        self.assertEqual(
            [(row["status"], row["uom_id"]) for row in response["results"]],
            [("verified", quant.product_uom_id.id) for quant in quants],
        )
        self.assertEqual(
            [row["uom_id"] for row in response["results"]], [self.unit.id, self.dozen.id]
        )

    def test_odoos_elevated_read_hands_out_the_unit_through_a_hidden_product(self):
        """Why this is a projection and not an approval: the related walk runs as superuser."""

        product = self.product("Agent Hidden Unit Product", unit=self.dozen)
        quant = self.quant(product=product)
        self.hide_from_service("product.template", f"[('id', '!=', {product.product_tmpl_id.id})]")
        as_service = quant.with_user(self.service_user)
        self.assertTrue(as_service.has_access("read"))
        self.assertFalse(product.with_user(self.service_user).has_access("read"))
        self.env.invalidate_all()
        self.assertEqual(as_service.product_uom_id.id, self.dozen.id)  # measured: disclosed
        self.assert_answer(self.only(self.project(quant)), "product-not-visible")


@tagged("post_install", "-at_install")
class TestUnitVisibility(UnitCase):
    def test_a_restricted_service_identity_gets_a_verified_unit(self):
        user = self.service_user
        self.assertFalse(user._is_admin() or user._is_system())
        self.assertFalse(user.has_group("stock.group_stock_manager"))
        self.assert_answer(self.only(self.project(self.quant())), "verified", self.unit.id)

    def test_a_product_hidden_by_its_template_rule_refuses_a_visible_quant(self):
        product = self.product("Agent Hidden")
        quant = self.quant(product=product)
        self.hide_from_service("product.template", f"[('id', '!=', {product.product_tmpl_id.id})]")
        self.assertTrue(quant.with_user(self.service_user).has_access("read"))
        self.assert_answer(self.only(self.project(quant)), "product-not-visible")

    def test_a_template_without_read_access_refuses_a_visible_product(self):
        quant = self.quant()
        self.revoke_read("product.template", self.service_user)
        self.assertTrue(quant.product_id.with_user(self.service_user).has_access("read"))
        self.assert_answer(self.only(self.project(quant)), "template-not-visible")

    def test_a_template_value_already_in_the_cache_is_not_forwarded(self):
        quant = self.quant()
        self.revoke_read("product.template", self.service_user)
        self.env.flush_all()
        template = quant.product_id.product_tmpl_id
        self.assertEqual(template.uom_id, self.unit)  # superuser warms the cache
        self.assertEqual(template.with_user(self.service_user).uom_id, self.unit)  # unchecked
        env = self.env(user=self.service_user, su=False)
        response = env[PROJECTION_MODEL].agent_projected_quant_unit_v1(quant.ids)
        self.assert_answer(self.only(response), "template-not-visible")

    def test_a_hidden_unit_refuses_the_quant(self):
        quant = self.quant(product=self.product("Agent Dozens", unit=self.dozen))
        self.hide_from_service("uom.uom", f"[('id', '!=', {self.dozen.id})]")
        self.assert_answer(self.only(self.project(quant)), "uom-not-visible")
        self.assert_answer(self.only(self.project(self.quant())), "verified", self.unit.id)

    def test_each_field_on_the_path_restricted_by_groups_refuses_the_quant(self):
        quant = self.quant()
        for model, name in GUARDED_FIELDS:
            with (
                self.subTest(field=f"{model}.{name}"),
                patch.object(self.env[model]._fields[name], "groups", "base.group_system"),
            ):
                self.env.invalidate_all()
                self.assert_answer(self.only(self.project(quant)), "field-not-readable")
        self.assert_answer(self.only(self.project(quant)), "verified", self.unit.id)

    def test_a_hidden_quant_and_a_missing_id_look_the_same(self):
        hidden = self.quant()
        self.hide_from_service("stock.quant", f"[('id', '!=', {hidden.id})]")
        self.assertEqual(
            [
                (row["status"], row["uom_id"])
                for row in self.project([hidden.id, 99999999])["results"]
            ],
            [("quant-not-visible", False)] * 2,
        )

    def test_one_refusal_does_not_change_another_answer_and_order_is_kept(self):
        first = self.quant()
        hidden_product = self.product("Agent Hidden")
        hidden = self.quant(product=hidden_product)
        self.hide_from_service(
            "product.template", f"[('id', '!=', {hidden_product.product_tmpl_id.id})]"
        )
        response = self.project([hidden.id, first.id, hidden.id])
        self.assertEqual(
            [(row["quant_id"], row["status"], row["uom_id"]) for row in response["results"]],
            [(hidden.id, "product-not-visible", False), (first.id, "verified", self.unit.id)],
        )


@tagged("post_install", "-at_install")
class TestUnitCompanies(UnitCase):
    def test_another_companys_quant_is_not_visible(self):
        quant = self.quant(self.location_b)
        self.assert_answer(self.only(self.project(quant)), "quant-not-visible")

    def test_the_active_companies_decide_not_the_users_company_list(self):
        quant = self.quant(self.location_b, self.product("Agent Beta", company=self.company_b))
        user = self.both_companies_user
        self.assert_answer(
            self.only(self.project(quant, user, companies=[self.company_a])), "quant-not-visible"
        )
        self.assert_answer(
            self.only(self.project(quant, user, companies=[self.company_a, self.company_b])),
            "verified",
            self.unit.id,
        )

    def test_a_parent_companys_product_in_a_child_location_is_verified(self):
        """The template rule is ``parent_of``, so a child company's quant of a parent company's
        product is fully visible to a child-company user; no company row is on this path."""

        product = self.product("Agent Parent Product", unit=self.dozen, company=self.company_a)
        quant = self.quant(self.location_child, product)
        self.assert_answer(
            self.only(self.project(quant, self.child_user)), "verified", self.dozen.id
        )

    def test_a_quant_holding_another_companys_product_is_refused(self):
        """``check_company`` stops this in the ORM; written in SQL, the product is hidden from the
        quant's own company and the unit is refused."""

        quant = self.quant()
        foreign = self.product("Agent Beta Only", company=self.company_b)
        self.sql("UPDATE stock_quant SET product_id = %s WHERE id = %s", (foreign.id, quant.id))
        self.assertTrue(quant.with_user(self.service_user).has_access("read"))
        self.assert_answer(self.only(self.project(quant)), "product-not-visible")


@tagged("post_install", "-at_install")
class TestUnitStaleAndInconsistentValues(UnitCase):
    def test_the_gap_a_unit_changed_after_stock_exists_is_not_converted(self):
        """Recorded, not closed: Odoo allows the change, relabels without converting, and the
        projection -- like every read -- reports the current unit for a quantity entered in the
        old one."""

        product = self.product("Agent Relabelled")
        quant = self.quant(product=product, quantity=5.0)
        product.product_tmpl_id.write({"uom_id": self.dozen.id})
        self.env.flush_all()
        self.assertEqual(quant.quantity, 5.0)
        self.assertEqual(quant.product_uom_id, self.dozen)
        self.assert_answer(self.only(self.project(quant)), "verified", self.dozen.id)

    def test_a_unit_written_in_sql_is_read_fresh_there_is_no_copy_to_go_stale(self):
        quant = self.quant()
        self.sql(
            "UPDATE product_template SET uom_id = %s WHERE id = %s",
            (self.dozen.id, quant.product_id.product_tmpl_id.id),
        )
        self.assert_answer(self.only(self.project(quant)), "verified", self.dozen.id)

    def test_a_product_link_moved_in_sql_is_followed_under_the_caller(self):
        quant = self.quant()
        hidden = self.product("Agent Moved To", unit=self.dozen)
        self.hide_from_service("product.template", f"[('id', '!=', {hidden.product_tmpl_id.id})]")
        self.sql("UPDATE stock_quant SET product_id = %s WHERE id = %s", (hidden.id, quant.id))
        self.assert_answer(self.only(self.project(quant)), "product-not-visible")


@tagged("post_install", "-at_install")
class TestUnitBounds(UnitCase):
    def test_the_id_bound_is_inclusive_and_one_over_is_refused(self):
        env = self.caller()[PROJECTION_MODEL]
        self.assertEqual(
            len(env.agent_projected_quant_unit_v1(list(range(1, 201)))["results"]), 200
        )
        with self.assertRaises(UserError) as refusal:
            env.agent_projected_quant_unit_v1(list(range(1, 202)))
        self.assertIn("quant-ids-over-bound", str(refusal.exception))

    def test_the_argument_grammar(self):
        env = self.caller()[PROJECTION_MODEL]
        for value in ([], [0], [True], ["1"], "1", None, [1.5]):
            with self.subTest(value=value), self.assertRaises(UserError):
                env.agent_projected_quant_unit_v1(value)

    def test_the_response_envelope_is_closed(self):
        response = self.project(self.quant() | self.quant(self.location_b))
        self.assertEqual(list(response), ["schema_version", "projection", "results"])
        self.assertEqual(response["projection"], "quant_unit_v1")
        for row in response["results"]:
            self.assertEqual(list(row), ["quant_id", "status", "uom_id"])


@tagged("post_install", "-at_install")
class TestUnitRpcDispatch(HttpCase, UnitCase):
    ENDPOINT = f"/json/2/{PROJECTION_MODEL}/{METHOD}"

    def call(self, user, body):
        self.env.flush_all()
        key = (
            self.env["res.users.apikeys"]
            .with_user(user)
            ._generate(None, f"itest-{user.login}", datetime.now() + timedelta(days=1))
        )
        return self.url_open(
            self.ENDPOINT,
            data=json.dumps(body),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )

    def test_an_ordinary_internal_user_is_refused_over_rpc(self):
        response = self.call(self.ordinary_user, {"quant_ids": self.quant().ids})
        self.assertEqual(response.status_code, 403)
        self.assertIn("requires-the-service-group", response.json()["message"])

    def test_the_service_identity_gets_typed_answers_over_rpc(self):
        mine = self.quant(product=self.product("Agent Dozens", unit=self.dozen))
        other = self.quant(self.location_b)
        response = self.call(self.service_user, {"quant_ids": [mine.id, other.id]})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual(
            [(row["status"], row["uom_id"]) for row in rows],
            [("verified", self.dozen.id), ("quant-not-visible", False)],
        )
        self.assertIs(type(rows[0]["uom_id"]), int)

    def test_a_hidden_template_is_refused_over_rpc(self):
        quant = self.quant()
        self.revoke_read("product.template", self.service_user)
        response = self.call(self.service_user, {"quant_ids": quant.ids})
        self.assert_answer(response.json()["results"][0], "template-not-visible")

    def test_the_grammar_is_enforced_over_rpc(self):
        response = self.call(self.service_user, {"quant_ids": [True]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("quant-id-not-a-positive-integer", response.json()["message"])

    def test_an_unauthenticated_request_is_refused(self):
        response = self.url_open(
            self.ENDPOINT, data=json.dumps({}), headers={"Content-Type": "application/json"}
        )
        self.assertIn(response.status_code, (401, 403))
