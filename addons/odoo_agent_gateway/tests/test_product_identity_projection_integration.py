"""Integration tests for the ``product_identity_v1`` checked projection, on real Odoo 19.

The claims under test, against Odoo itself:

* **The pinned semantics are what the core assumes.** The five ``product.product`` fields are
  measured to be unelevated ``_inherits`` delegations, and each template field's effective
  attributes are read from the registry, not from a comment.
* **A readable product forwards nothing.** When the template row, one template field, the unit or
  the company is not readable as the caller, the product is refused with no values -- including when
  Odoo's own delegated read would have raised, and when the value is already in the transaction's
  cache.
* **Refusals are per product.** One refused product never changes another's answer.

Restricted identities are real users holding only ``base.group_user``, ``stock.group_stock_user``
and the service group. Hidden rows are produced with real record rules scoped to the service group,
missing rights by revoking read ACLs inside the test transaction, and field restrictions by the
same ``groups`` attribute a custom module or Studio would set. Nothing here grants a right.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..projection.product_identity import PRODUCT_TYPES, PROJECTED_FIELDS

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
PROJECTION_MODEL = "odoo.agent.product.identity.projection"
METHOD = "agent_projected_product_identity_v1"


class ProductCase(TransactionCase):
    """Two companies and a child of the first, three identities, and a helper per fixture."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent Product Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent Product Beta"})
        cls.child_a = cls.env["res.company"].create(
            {"name": "Agent Product Alpha Child", "parent_id": cls.company_a.id}
        )
        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.service_user = cls._user("agent.product.service", [cls.company_a], service=True)
        cls.ordinary_user = cls._user("agent.product.ordinary", [cls.company_a], service=False)
        cls.both_companies_user = cls._user(
            "agent.product.both", [cls.company_a, cls.company_b], service=True
        )
        cls.child_user = cls._user("agent.product.child", [cls.child_a], service=True)

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

    def product(self, name="Agent Widget", *, company=None, **values):
        return self.env["product.product"].create(
            {
                "name": name,
                "type": "consu",
                "is_storable": True,
                "uom_id": self.unit.id,
                "company_id": (company or self.company_a).id if company is not False else False,
                **values,
            }
        )

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
        held = access.filtered(lambda a: not a.group_id or a.group_id in user.all_group_ids)
        held.write({"perm_read": False})
        self.assertFalse(self.env[model].with_user(user).has_access("read"))

    def sql(self, statement, params):
        self.env.flush_all()
        self.env.cr.execute(statement, params)
        self.env.invalidate_all()

    def identity(self, products, user=None, *, companies=None, lang=None):
        self.env.flush_all()
        env = self.env(user=user or self.service_user, su=False)
        context = dict(env.context)
        if companies is not None:
            context["allowed_company_ids"] = [company.id for company in companies]
        if lang is not None:
            context["lang"] = lang
        ids = products if isinstance(products, list) else products.ids
        return env(context=context)[PROJECTION_MODEL].agent_projected_product_identity_v1(ids)

    def only(self, response):
        (row,) = response["results"]
        return row

    def assert_refused(self, row, status):
        self.assertEqual(row["status"], status)
        for key in ("name", "type", "is_storable", "uom_id", "company_id"):
            self.assertIs(row[key], False, key)


@tagged("post_install", "-at_install")
class TestIdentityAuthorization(ProductCase):
    def test_an_ordinary_internal_user_is_refused(self):
        with self.assertRaises(AccessError) as refusal:
            self.identity(self.product(), self.ordinary_user)
        self.assertIn("requires-the-service-group", str(refusal.exception))

    def test_the_service_identity_is_allowed(self):
        self.assertEqual(self.only(self.identity(self.product()))["status"], "verified")

    def test_superuser_mode_is_refused(self):
        with self.assertRaises(UserError) as refusal:
            self.env[PROJECTION_MODEL].sudo().agent_projected_product_identity_v1(
                self.product().ids
            )
        self.assertIn("refuses-superuser-mode", str(refusal.exception))

    def test_superuser_refusal_precedes_the_group_check(self):
        with self.assertRaises(UserError) as refusal:
            self.env(user=self.ordinary_user)[
                PROJECTION_MODEL
            ].sudo().agent_projected_product_identity_v1(self.product().ids)
        self.assertIn("refuses-superuser-mode", str(refusal.exception))


@tagged("post_install", "-at_install")
class TestIdentityPinnedSemantics(ProductCase):
    """Measured on the running registry, so a module that redeclares a field fails here."""

    def test_the_five_fields_are_unelevated_delegations_of_the_template(self):
        product_fields = self.env["product.product"]._fields
        for name in PROJECTED_FIELDS:
            with self.subTest(field=name):
                field = product_fields[name]
                self.assertTrue(field.inherited)
                self.assertEqual(field.related, f"product_tmpl_id.{name}")
                self.assertFalse(field.related_sudo)
                self.assertFalse(field.compute_sudo)
                self.assertFalse(field.store)
        link = product_fields["product_tmpl_id"]
        self.assertEqual(
            (link.type, link.comodel_name, link.store, link.required, link.ondelete, link.groups),
            ("many2one", "product.template", True, True, "cascade", None),
        )

    def test_each_template_fields_effective_attributes(self):
        fields = self.env["product.template"]._fields
        for name in PROJECTED_FIELDS:
            with self.subTest(field=name):
                field = fields[name]
                self.assertIsNone(field.groups)
                self.assertFalse(field.company_dependent)
                self.assertTrue(field.store)
                self.assertFalse(field.related)
        name, kind, storable, unit, company = (fields[n] for n in PROJECTED_FIELDS)
        self.assertEqual((name.type, name.required, bool(name.translate)), ("char", True, True))
        self.assertEqual(kind.type, "selection")
        self.assertEqual(
            tuple(value for value, _ in kind._description_selection(self.env)), PRODUCT_TYPES
        )
        self.assertEqual(
            (storable.type, storable.compute, storable.readonly, storable.compute_sudo),
            ("boolean", "compute_is_storable", False, True),
        )
        # Its one compute depends only on its own record's `type`: no cross-record elevation.
        self.assertEqual([str(dep) for dep in self.registry.field_depends[storable]], ["type"])
        self.assertEqual(
            (unit.type, unit.comodel_name, unit.required), ("many2one", "uom.uom", True)
        )
        self.assertEqual(
            (company.type, company.comodel_name, company.required),
            ("many2one", "res.company", False),
        )

    def test_the_template_rule_reaches_the_product_but_the_template_acl_does_not(self):
        """Why the projection re-reads the template instead of trusting the product row."""

        domain = (
            self.env["ir.rule"]
            .with_user(self.service_user)
            ._compute_domain("product.product", "read")
        )
        self.assertIn("product_tmpl_id", str(domain))
        product = self.product()
        self.revoke_read("product.template", self.service_user)
        as_service = product.with_user(self.service_user)
        self.assertTrue(as_service.has_access("read"))
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            as_service.name  # noqa: B018 -- Odoo's own delegated read refuses, it does not leak

    def test_verified_values_equal_odoos_own_delegated_read(self):
        shared = self.product("Agent Shared", company=False)
        service = self.product("Agent Service", type="service", is_storable=False)
        response = self.identity(shared | service)
        for row, record in zip(response["results"], shared | service, strict=True):
            self.assertEqual(
                row,
                {
                    "product_id": record.id,
                    "status": "verified",
                    "name": record.name,
                    "type": record.type,
                    "is_storable": record.is_storable,
                    "uom_id": record.uom_id.id,
                    "company_id": record.company_id.id or False,
                },
            )
        self.assertEqual(response["lang"], "en_US")


@tagged("post_install", "-at_install")
class TestIdentityVisibility(ProductCase):
    def test_a_restricted_service_identity_gets_verified_values(self):
        user = self.service_user
        self.assertFalse(user._is_admin() or user._is_system())
        self.assertFalse(user.has_group("stock.group_stock_manager"))
        row = self.only(self.identity(self.product()))
        self.assertEqual(
            (row["status"], row["type"], row["is_storable"]), ("verified", "consu", True)
        )

    def test_a_template_hidden_by_a_rule_hides_its_product(self):
        hidden = self.product("Agent Hidden Template")
        visible = self.product("Agent Visible")
        self.hide_from_service("product.template", f"[('id', '!=', {hidden.product_tmpl_id.id})]")
        response = self.identity(hidden | visible)
        self.assert_refused(response["results"][0], "product-not-visible")
        self.assertEqual(response["results"][1]["status"], "verified")

    def test_a_readable_product_with_an_unreadable_template_forwards_nothing(self):
        product = self.product()
        self.revoke_read("product.template", self.service_user)
        self.assertTrue(product.with_user(self.service_user).has_access("read"))
        self.assert_refused(self.only(self.identity(product)), "template-not-visible")

    def test_a_value_already_in_the_cache_is_not_forwarded(self):
        """Measured: the ORM serves a cached value without re-checking *record* access (ACL and
        rules); the projection's explicit template check is what refuses it."""

        product = self.product("Agent Cached")
        self.revoke_read("product.template", self.service_user)
        self.env.flush_all()
        self.assertEqual(product.product_tmpl_id.name, "Agent Cached")  # superuser warms the cache
        as_service = product.product_tmpl_id.with_user(self.service_user)
        self.assertEqual(as_service.name, "Agent Cached")  # a cache hit skips the record check
        env = self.env(user=self.service_user, su=False)
        response = env[PROJECTION_MODEL].agent_projected_product_identity_v1(product.ids)
        self.assert_refused(self.only(response), "template-not-visible")

    def test_a_template_field_restricted_by_groups_refuses_the_product(self):
        """Measured: unlike record access, a field's ``groups`` is enforced on every read, cached or
        not, so Odoo refuses these reads itself. The projection's explicit field check is defence in
        depth and names the refusal; either way no value is returned."""

        product = self.product()
        template = product.product_tmpl_id
        for name in PROJECTED_FIELDS:
            field = self.env["product.template"]._fields[name]
            for warm in (False, True):
                with (
                    self.subTest(field=name, warm=warm),
                    patch.object(field, "groups", "base.group_system"),
                ):
                    self.env.invalidate_all()
                    if warm:
                        template.read(list(PROJECTED_FIELDS))
                        with self.assertRaises(AccessError):
                            template.with_user(self.service_user)[name]  # checked even when cached
                    self.assert_refused(self.only(self.identity(product)), "field-not-readable")
        self.assertEqual(self.only(self.identity(product))["status"], "verified")

    def test_an_unreadable_template_link_refuses_the_product(self):
        product = self.product()
        field = self.env["product.product"]._fields["product_tmpl_id"]
        with patch.object(field, "groups", "base.group_system"):
            self.assert_refused(self.only(self.identity(product)), "field-not-readable")

    def test_a_hidden_unit_refuses_the_product(self):
        dozen = self.env.ref("uom.product_uom_dozen")
        product = self.product(uom_id=dozen.id)
        self.hide_from_service("uom.uom", f"[('id', '!=', {dozen.id})]")
        self.assert_refused(self.only(self.identity(product)), "uom-not-visible")
        self.assertEqual(self.only(self.identity(self.product()))["status"], "verified")

    def test_one_refusal_does_not_change_another_answer_and_order_is_kept(self):
        first, hidden, last = self.product("One"), self.product("Two"), self.product("Three")
        self.hide_from_service("product.template", f"[('id', '!=', {hidden.product_tmpl_id.id})]")
        response = self.identity([last.id, hidden.id, first.id, last.id, 999999])
        self.assertEqual(
            [(row["product_id"], row["status"]) for row in response["results"]],
            [
                (last.id, "verified"),
                (hidden.id, "product-not-visible"),
                (first.id, "verified"),
                (999999, "product-not-visible"),
            ],
        )


@tagged("post_install", "-at_install")
class TestIdentityCompanies(ProductCase):
    def test_another_companys_product_is_not_visible(self):
        product = self.product(company=self.company_b)
        self.assert_refused(self.only(self.identity(product)), "product-not-visible")

    def test_the_active_companies_decide_not_the_users_company_list(self):
        product = self.product(company=self.company_b)
        user = self.both_companies_user
        self.assert_refused(
            self.only(self.identity(product, user, companies=[self.company_a])),
            "product-not-visible",
        )
        row = self.only(self.identity(product, user, companies=[self.company_a, self.company_b]))
        self.assertEqual((row["status"], row["company_id"]), ("verified", self.company_b.id))

    def test_a_shared_product_is_verified_without_a_company(self):
        row = self.only(self.identity(self.product(company=False), self.child_user))
        self.assertEqual((row["status"], row["company_id"]), ("verified", False))

    def test_a_parent_companys_product_is_visible_but_its_company_is_not(self):
        """The template rule is ``parent_of``; ``res.company``'s employee rule is ``id in
        company_ids``. Odoo's delegated read hands out the company id anyway; the projection does
        not."""

        product = self.product(company=self.company_a)
        as_child = product.with_user(self.child_user).with_context(
            allowed_company_ids=[self.child_a.id]
        )
        self.assertTrue(as_child.has_access("read"))
        self.assertEqual(as_child.company_id.id, self.company_a.id)
        self.assertFalse(as_child.company_id.has_access("read"))
        self.assert_refused(
            self.only(self.identity(product, self.child_user)), "company-not-visible"
        )

    def test_a_parent_companys_product_is_verified_when_the_parent_is_active_too(self):
        user = self.child_user
        user.company_ids = [(4, self.company_a.id)]
        product = self.product(company=self.company_a)
        row = self.only(self.identity(product, user, companies=[self.child_a, self.company_a]))
        self.assertEqual((row["status"], row["company_id"]), ("verified", self.company_a.id))


@tagged("post_install", "-at_install")
class TestIdentityStaleValues(ProductCase):
    def test_a_storable_flag_left_on_a_service_is_refused(self):
        product = self.product(type="service", is_storable=False)
        self.sql(
            "UPDATE product_template SET is_storable = TRUE WHERE id = %s",
            (product.product_tmpl_id.id,),
        )
        self.assert_refused(self.only(self.identity(product)), "storability-conflicting")
        # Odoo's own compute agrees the stored value was stale.
        product.product_tmpl_id.compute_is_storable()
        self.assertFalse(product.product_tmpl_id.is_storable)
        self.assertEqual(self.only(self.identity(product))["status"], "verified")

    def test_a_type_outside_the_pinned_selection_is_refused(self):
        product = self.product()
        self.sql(
            "UPDATE product_template SET type = 'product' WHERE id = %s",
            (product.product_tmpl_id.id,),
        )
        self.assert_refused(self.only(self.identity(product)), "type-unrecognized")

    def test_values_are_read_fresh_from_the_template(self):
        product = self.product("Agent Before")
        self.identity(product)
        self.sql(
            "UPDATE product_template SET name = %s WHERE id = %s",
            (json.dumps({"en_US": "Agent After"}), product.product_tmpl_id.id),
        )
        self.assertEqual(self.only(self.identity(product))["name"], "Agent After")

    def test_the_name_is_the_callers_language_and_the_envelope_says_which(self):
        self.env["res.lang"]._activate_lang("fr_FR")
        product = self.product("Agent Widget")
        product.product_tmpl_id.with_context(lang="fr_FR").name = "Agent Bidule"
        untranslated = self.product("Agent Untranslated")
        french = self.identity(product | untranslated, lang="fr_FR")
        self.assertEqual(french["lang"], "fr_FR")
        self.assertEqual(
            [row["name"] for row in french["results"]], ["Agent Bidule", "Agent Untranslated"]
        )
        english = self.identity(product)
        self.assertEqual((english["lang"], self.only(english)["name"]), ("en_US", "Agent Widget"))


@tagged("post_install", "-at_install")
class TestIdentityBounds(ProductCase):
    def test_the_id_bound_is_inclusive_and_one_over_is_refused(self):
        env = self.env(user=self.service_user, su=False)[PROJECTION_MODEL]
        response = env.agent_projected_product_identity_v1(list(range(1, 201)))
        self.assertEqual(len(response["results"]), 200)
        with self.assertRaises(UserError) as refusal:
            env.agent_projected_product_identity_v1(list(range(1, 202)))
        self.assertIn("product-ids-over-bound", str(refusal.exception))

    def test_the_argument_grammar(self):
        env = self.env(user=self.service_user, su=False)[PROJECTION_MODEL]
        for value in ([], [0], [True], ["1"], "1", None, [1.5]):
            with self.subTest(value=value), self.assertRaises(UserError):
                env.agent_projected_product_identity_v1(value)

    def test_the_response_envelope_is_closed(self):
        product = self.product()
        hidden = self.product(company=self.company_b)
        response = self.identity(product | hidden)
        self.assertEqual(list(response), ["schema_version", "projection", "lang", "results"])
        self.assertEqual(response["projection"], "product_identity_v1")
        for row in response["results"]:
            self.assertEqual(
                list(row),
                ["product_id", "status", "name", "type", "is_storable", "uom_id", "company_id"],
            )


@tagged("post_install", "-at_install")
class TestIdentityRpcDispatch(HttpCase, ProductCase):
    ENDPOINT = f"/json/2/{PROJECTION_MODEL}/{METHOD}"

    def api_key_for(self, user):
        return (
            self.env["res.users.apikeys"]
            .with_user(user)
            ._generate(None, f"itest-{user.login}", datetime.now() + timedelta(days=1))
        )

    def call(self, user, body):
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
        response = self.call(self.ordinary_user, {"product_ids": self.product().ids})
        self.assertEqual(response.status_code, 403)
        self.assertIn("requires-the-service-group", response.json()["message"])

    def test_the_service_identity_gets_verified_and_refused_rows_over_rpc(self):
        product = self.product()
        hidden = self.product(company=self.company_b)
        response = self.call(self.service_user, {"product_ids": [product.id, hidden.id]})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [(row["product_id"], row["status"]) for row in payload["results"]],
            [(product.id, "verified"), (hidden.id, "product-not-visible")],
        )
        self.assertEqual(payload["results"][0]["uom_id"], self.unit.id)
        self.assertIs(payload["results"][1]["name"], False)

    def test_an_unreadable_template_is_refused_over_rpc(self):
        product = self.product()
        self.revoke_read("product.template", self.service_user)
        response = self.call(self.service_user, {"product_ids": product.ids})
        self.assertEqual(response.status_code, 200)
        self.assert_refused(response.json()["results"][0], "template-not-visible")

    def test_the_callers_context_language_is_named_over_rpc(self):
        self.env["res.lang"]._activate_lang("fr_FR")
        product = self.product("Agent Widget")
        product.product_tmpl_id.with_context(lang="fr_FR").name = "Agent Bidule"
        response = self.call(
            self.service_user, {"product_ids": product.ids, "context": {"lang": "fr_FR"}}
        )
        payload = response.json()
        self.assertEqual(
            (payload["lang"], payload["results"][0]["name"]), ("fr_FR", "Agent Bidule")
        )

    def test_the_grammar_is_enforced_over_rpc(self):
        response = self.call(self.service_user, {"product_ids": [True]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("product-id-not-a-positive-integer", response.json()["message"])
        self.assertEqual(self.call(self.service_user, {"ids": [1]}).status_code, 422)

    def test_an_unauthenticated_request_is_refused(self):
        response = self.url_open(
            self.ENDPOINT, data=json.dumps({}), headers={"Content-Type": "application/json"}
        )
        self.assertIn(response.status_code, (401, 403))
