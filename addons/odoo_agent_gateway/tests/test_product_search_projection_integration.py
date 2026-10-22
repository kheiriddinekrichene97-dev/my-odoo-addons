"""Integration tests for the ``product_search_v1`` bounded search projection, on real Odoo 19.

The claims under test, against Odoo itself:

* **Same question, same answer where the caller may read.** Over a mixed fixture set, the included
  products equal what the product pack's own domain returns today, run as the same caller.
* **No match on what the caller may not read.** Today's domain is measured to filter on template
  values through a subquery that skips the template's ACL; the projection matches names only on
  templates the caller can read, proves every candidate's identity, and lists what it cannot prove
  as ``refused`` rather than dropping it.
* **Bounded and deterministic.** Rows in id order, cut only at ``max_rows`` with ``truncated``; any
  candidate bound overflow refuses the whole request.

Hidden rows come from real record rules scoped to the service group, missing rights from revoking
read ACLs inside the test transaction, and field restrictions from the ``groups`` attribute.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..projection.product_search import PRODUCT_SEARCH_FIELDS, TEMPLATE_SEARCH_FIELDS

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
PROJECTION_MODEL = "odoo.agent.product.search.projection"
METHOD = "agent_projected_product_search_v1"


def pack_domain(company_id, term):
    """The product pack's domain, verbatim (``ProductStockReader._product_search_domain``; the
    control-plane suite pins that source against this copy)."""

    escaped = term.strip()
    for character, replacement in (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_")):
        escaped = escaped.replace(character, replacement)
    pattern = f"%{escaped}%"
    return [
        ("active", "=", True),
        ("type", "=", "consu"),
        ("is_storable", "=", True),
        ("company_id", "in", [company_id, False]),
        "|",
        "|",
        ("name", "ilike", pattern),
        ("default_code", "ilike", pattern),
        ("barcode", "ilike", pattern),
    ]


class SearchCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent Search Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent Search Beta"})
        cls.child_a = cls.env["res.company"].create(
            {"name": "Agent Search Alpha Child", "parent_id": cls.company_a.id}
        )
        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.dozen = cls.env.ref("uom.product_uom_dozen")
        cls.service_user = cls._user("agent.search.service", [cls.company_a], service=True)
        cls.ordinary_user = cls._user("agent.search.ordinary", [cls.company_a], service=False)
        cls.both_companies_user = cls._user(
            "agent.search.both", [cls.company_a, cls.company_b], service=True
        )
        cls.child_user = cls._user("agent.search.child", [cls.child_a], service=True)

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

    def product(self, name, *, code=None, barcode=None, company=None, **values):
        return self.env["product.product"].create(
            {
                "name": name,
                "default_code": code,
                "barcode": barcode,
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
        access = self.env["ir.model.access"].search(
            [("model_id.model", "=", model), ("perm_read", "=", True)]
        )
        access.filtered(lambda a: not a.group_id or a.group_id in user.all_group_ids).write(
            {"perm_read": False}
        )
        self.assertFalse(self.env[model].with_user(user).has_access("read"))

    def caller(self, user=None, companies=None, lang=None):
        self.env.flush_all()
        env = self.env(user=user or self.service_user, su=False)
        context = dict(env.context)
        if companies is not None:
            context["allowed_company_ids"] = [c.id for c in companies]
        if lang is not None:
            context["lang"] = lang
        return env(context=context)

    def search(self, term, *, company=None, max_rows=50, user=None, companies=None, lang=None):
        env = self.caller(user, companies, lang)
        return env[PROJECTION_MODEL].agent_projected_product_search_v1(
            (company or self.company_a).id, term, max_rows
        )

    def today(self, term, *, company=None, user=None, companies=None, lang=None):
        """The pack's own domain, run as the same caller: what the pack sees today."""

        env = self.caller(user, companies, lang)
        company_id = (company or self.company_a).id
        return set(env["product.product"].search(pack_domain(company_id, term), order="id").ids)

    def listed(self, response, disposition=None):
        self.assertEqual(response["status"], "answered")
        return [
            row["product_id"]
            for row in response["rows"]
            if disposition is None or row["disposition"] == disposition
        ]

    def assert_refused_request(self, response, status):
        self.assertEqual(
            (response["status"], response["totals"], response["rows"]), (status, False, [])
        )


@tagged("post_install", "-at_install")
class TestSearchAuthorization(SearchCase):
    def test_an_ordinary_internal_user_is_refused(self):
        with self.assertRaises(AccessError) as refusal:
            self.search("widget", user=self.ordinary_user)
        self.assertIn("requires-the-service-group", str(refusal.exception))

    def test_the_service_identity_is_allowed(self):
        product = self.product("Agent Search Widget")
        self.assertEqual(self.listed(self.search("search widget"), "included"), product.ids)

    def test_superuser_mode_is_refused(self):
        with self.assertRaises(UserError) as refusal:
            self.env[PROJECTION_MODEL].sudo().agent_projected_product_search_v1(
                self.company_a.id, "widget", 10
            )
        self.assertIn("refuses-superuser-mode", str(refusal.exception))

    def test_superuser_refusal_precedes_the_group_check(self):
        with self.assertRaises(UserError) as refusal:
            self.env(user=self.ordinary_user)[
                PROJECTION_MODEL
            ].sudo().agent_projected_product_search_v1(self.company_a.id, "widget", 10)
        self.assertIn("refuses-superuser-mode", str(refusal.exception))


@tagged("post_install", "-at_install")
class TestSearchSemantics(SearchCase):
    def test_included_products_equal_todays_domain_for_a_fully_permitted_caller(self):
        """A mixed set: name, code and barcode matches; services, consumables, other and shared
        companies; archived products and an archived template with an active variant."""

        by_name = self.product("Agent Match Gear")
        by_code = self.product("Agent Other One", code="MATCH-7")
        by_barcode = self.product("Agent Other Two", barcode="99MATCH01")
        shared = self.product("Agent Match Shared", company=False)
        self.product("Agent Match Service", type="service", is_storable=False)
        self.product("Agent Match Consumable", is_storable=False)
        self.product("Agent Match Beta", company=self.company_b)
        archived = self.product("Agent Match Archived")
        archived.active = False
        template_archived = self.product("Agent Match Old Template")
        template_archived.product_tmpl_id.active = False
        template_archived.active = True
        self.product("Agent Unrelated")
        response = self.search("match")
        today = self.today("match")
        self.assertEqual(set(self.listed(response, "included")), today)
        self.assertTrue({by_name.id, by_code.id, by_barcode.id, shared.id} <= today)
        self.assertEqual(self.listed(response, "refused"), [])

    def test_like_wildcards_in_the_term_are_matched_literally(self):
        percent = self.product("Agent Blend 50% Cotton")
        self.product("Agent Blend 500 Cotton")
        underscore = self.product("Agent a_b Part")
        self.product("Agent axb Part")
        self.assertEqual(self.listed(self.search("blend 50%"), "included"), percent.ids)
        self.assertEqual(self.listed(self.search("a_b"), "included"), underscore.ids)
        self.assertEqual(set(self.listed(self.search("a_b"))), self.today("a_b"))

    def test_todays_domain_matches_names_through_a_subquery_that_skips_the_template_acl(self):
        """Measured: the product pack's domain still finds a product by its template name for a
        caller with no right to read templates. The projection refuses to answer at all."""

        product = self.product("Agent Bypass Gear")
        self.revoke_read("product.template", self.service_user)
        self.assertTrue(product.with_user(self.service_user).has_access("read"))
        self.assertEqual(self.today("bypass gear"), set(product.ids))
        self.assert_refused_request(self.search("bypass gear"), "model-not-readable")

    def test_the_delegated_search_path_is_the_bypassed_link(self):
        link = self.env["product.product"]._fields["product_tmpl_id"]
        self.assertTrue(link.bypass_search_access)
        for name in ("name", "type", "is_storable", "company_id"):
            with self.subTest(field=name):
                field = self.env["product.product"]._fields[name]
                self.assertEqual(
                    (field.inherited, field.related), (True, f"product_tmpl_id.{name}")
                )
        for name in ("default_code", "barcode", "active"):
            with self.subTest(field=name):
                field = self.env["product.product"]._fields[name]
                self.assertEqual((field.store, field.related, field.inherited), (True, None, False))


@tagged("post_install", "-at_install")
class TestSearchVisibility(SearchCase):
    def test_a_restricted_service_identity_gets_verified_identities(self):
        user = self.service_user
        self.assertFalse(user._is_admin() or user._is_system())
        product = self.product("Agent Restricted Gear", uom_id=self.dozen.id)
        (row,) = self.search("restricted gear")["rows"]
        self.assertEqual(
            (row["product_id"], row["disposition"], row["uom_id"], row["company_id"]),
            (product.id, "included", self.dozen.id, self.company_a.id),
        )

    def test_a_template_hidden_by_a_rule_is_found_neither_by_name_nor_by_code(self):
        hidden = self.product("Agent Hidden Gear", code="HID-1")
        self.hide_from_service("product.template", f"[('id', '!=', {hidden.product_tmpl_id.id})]")
        for term in ("hidden gear", "HID-1"):
            with self.subTest(term=term):
                response = self.search(term)
                self.assertEqual(self.listed(response), [])
                self.assertEqual(response["totals"]["candidates"], 0)

    def test_a_template_without_read_access_refuses_the_whole_request(self):
        self.product("Agent Acl Gear", code="ACL-1")
        self.revoke_read("product.template", self.service_user)
        for term in ("acl gear", "ACL-1"):
            with self.subTest(term=term):
                self.assert_refused_request(self.search(term), "model-not-readable")

    def test_each_filtered_field_restricted_by_groups_refuses_the_whole_request(self):
        self.product("Agent Field Gear")
        for model, names in (
            ("product.template", TEMPLATE_SEARCH_FIELDS),
            ("product.product", PRODUCT_SEARCH_FIELDS),
        ):
            for name in names:
                with (
                    self.subTest(field=f"{model}.{name}"),
                    patch.object(self.env[model]._fields[name], "groups", "base.group_system"),
                ):
                    self.assert_refused_request(self.search("field gear"), "field-not-readable")
        self.assertEqual(len(self.listed(self.search("field gear"), "included")), 1)

    def test_a_code_match_whose_identity_cannot_be_proven_is_listed_as_refused(self):
        product = self.product("Agent Unproven", code="UNP-1", uom_id=self.dozen.id)
        self.hide_from_service("uom.uom", f"[('id', '!=', {self.dozen.id})]")
        response = self.search("UNP-1")
        (row,) = response["rows"]
        self.assertEqual(
            (row["product_id"], row["disposition"], row["identity_status"], row["name"]),
            (product.id, "refused", "uom-not-visible", False),
        )
        self.assertEqual(response["totals"]["refused"], 1)

    def test_a_hidden_product_is_absent(self):
        product = self.product("Agent Absent Gear")
        self.hide_from_service("product.product", f"[('id', '!=', {product.id})]")
        self.assertEqual(self.search("absent gear")["totals"]["candidates"], 0)

    def test_a_verified_non_member_found_by_code_is_counted_not_listed(self):
        """A service, and goods that are not storable: both proven, neither a member -- as today."""

        self.product("Agent Service Code", code="SVC-1", type="service", is_storable=False)
        self.product("Agent Consumable Code", code="SVC-2", is_storable=False)
        response = self.search("SVC-")
        self.assertEqual(response["rows"], [])
        self.assertEqual(
            response["totals"], {"candidates": 2, "included": 0, "refused": 0, "excluded": 2}
        )
        self.assertEqual(self.today("SVC-"), set())


@tagged("post_install", "-at_install")
class TestSearchCompanies(SearchCase):
    def test_another_companys_product_is_not_found_or_not_a_member(self):
        """Hidden from a caller who cannot see the company; found by code, proven and excluded for a
        caller who can -- exactly as today's domain excludes it."""

        beta = self.product("Agent Beta Gear", code="BETA-1", company=self.company_b)
        self.assertEqual(self.search("beta gear")["totals"]["candidates"], 0)
        self.assertEqual(self.search("BETA-1")["totals"]["candidates"], 0)
        user, companies = self.both_companies_user, [self.company_a, self.company_b]
        response = self.search("BETA-1", user=user, companies=companies)
        self.assertEqual(response["rows"], [])
        self.assertEqual(
            response["totals"], {"candidates": 1, "included": 0, "refused": 0, "excluded": 1}
        )
        self.assertEqual(self.today("BETA-1", user=user, companies=companies), set())
        self.assertEqual(
            self.listed(
                self.search("BETA-1", company=self.company_b, user=user, companies=companies),
                "included",
            ),
            beta.ids,
        )

    def test_the_requested_company_must_be_active(self):
        product = self.product("Agent Beta Active", company=self.company_b)
        user = self.both_companies_user
        self.assert_refused_request(
            self.search(
                "beta active", company=self.company_b, user=user, companies=[self.company_a]
            ),
            "company-not-allowed",
        )
        response = self.search(
            "beta active",
            company=self.company_b,
            user=user,
            companies=[self.company_a, self.company_b],
        )
        self.assertEqual(self.listed(response, "included"), product.ids)
        self.assertEqual(
            set(self.listed(response)),
            self.today(
                "beta active",
                company=self.company_b,
                user=user,
                companies=[self.company_a, self.company_b],
            ),
        )

    def test_a_parent_companys_product_is_not_a_member_and_its_company_is_unprovable(self):
        """By name it is outside the company clause, as today. By code it is a candidate whose
        company row the child cannot read, so it is refused rather than excluded on a guess."""

        parent = self.product("Agent Parent Gear", code="PAR-1", company=self.company_a)
        user, companies = self.child_user, [self.child_a]
        self.assertEqual(
            self.search("parent gear", company=self.child_a, user=user, companies=companies)[
                "totals"
            ]["candidates"],
            0,
        )
        (row,) = self.search("PAR-1", company=self.child_a, user=user, companies=companies)["rows"]
        self.assertEqual(
            (row["product_id"], row["disposition"], row["identity_status"]),
            (parent.id, "refused", "company-not-visible"),
        )
        self.assertEqual(
            self.today("PAR-1", company=self.child_a, user=user, companies=companies), set()
        )


@tagged("post_install", "-at_install")
class TestSearchLanguage(SearchCase):
    def test_the_name_matches_in_the_callers_language_as_today(self):
        self.env["res.lang"]._activate_lang("fr_FR")
        product = self.product("Agent Widget Lang")
        product.product_tmpl_id.with_context(lang="fr_FR").name = "Agent Bidule Lang"
        french = self.search("bidule lang", lang="fr_FR")
        self.assertEqual((french["lang"], self.listed(french, "included")), ("fr_FR", product.ids))
        self.assertEqual(set(self.listed(french)), self.today("bidule lang", lang="fr_FR"))
        self.assertEqual(self.listed(self.search("bidule lang")), [])
        self.assertEqual(self.listed(self.search("widget lang", lang="fr_FR")), [])

    def test_an_untranslated_name_falls_back_to_english_as_today(self):
        self.env["res.lang"]._activate_lang("fr_FR")
        product = self.product("Agent Untranslated Lang")
        response = self.search("untranslated lang", lang="fr_FR")
        self.assertEqual(self.listed(response, "included"), product.ids)
        self.assertEqual(set(self.listed(response)), self.today("untranslated lang", lang="fr_FR"))


@tagged("post_install", "-at_install")
class TestSearchBounds(SearchCase):
    def test_rows_are_in_id_order_and_cut_only_at_max_rows(self):
        products = self.env["product.product"]
        for index in range(5):
            products |= self.product(f"Agent Page Gear {index}")
        full = self.search("page gear")
        self.assertEqual(self.listed(full), sorted(products.ids))
        self.assertIs(full["truncated"], False)
        cut = self.search("page gear", max_rows=3)
        self.assertEqual(self.listed(cut), sorted(products.ids)[:3])
        self.assertIs(cut["truncated"], True)
        self.assertEqual(cut["totals"], full["totals"])
        exact = self.search("page gear", max_rows=5)
        self.assertIs(exact["truncated"], False)

    def test_the_template_match_bound_refuses_the_whole_request(self):
        self.env["product.product"].create(
            [
                {"name": f"Agent Flood {i:03}", "type": "consu", "is_storable": True}
                for i in range(201)
            ]
        )
        self.assert_refused_request(self.search("agent flood"), "template-matches-over-bound")

    def test_the_code_branch_bound_refuses_the_whole_request(self):
        self.env["product.product"].create(
            [
                {"name": f"Agent Coded {i}", "default_code": f"CODEFLOOD-{i:03}", "type": "service"}
                for i in range(201)
            ]
        )
        self.assert_refused_request(self.search("CODEFLOOD"), "branch-products-over-bound")

    def test_the_argument_grammar(self):
        env = self.caller()[PROJECTION_MODEL]
        good = dict(company_id=self.company_a.id, search_term="widget", max_rows=10)
        bad = {
            "company_id": [0, True, "1", None],
            "search_term": ["", "   ", "x" * 65, None, ["x"]],
            "max_rows": [0, 51, True, None],
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(UserError):
                    env.agent_projected_product_search_v1(**{**good, name: value})


@tagged("post_install", "-at_install")
class TestSearchRpcDispatch(HttpCase, SearchCase):
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

    def body(self, **overrides):
        return {
            "company_id": self.company_a.id,
            "search_term": "rpc gear",
            "max_rows": 10,
            **overrides,
        }

    def test_an_ordinary_internal_user_is_refused_over_rpc(self):
        response = self.call(self.ordinary_user, self.body())
        self.assertEqual(response.status_code, 403)
        self.assertIn("requires-the-service-group", response.json()["message"])

    def test_the_service_identity_gets_an_answer_over_rpc(self):
        product = self.product("Agent Rpc Gear")
        response = self.call(self.service_user, self.body())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual((payload["status"], payload["lang"]), ("answered", "en_US"))
        self.assertEqual([row["product_id"] for row in payload["rows"]], product.ids)
        self.assertIs(type(payload["rows"][0]["uom_id"]), int)

    def test_the_callers_context_language_is_used_and_named_over_rpc(self):
        self.env["res.lang"]._activate_lang("fr_FR")
        product = self.product("Agent Rpc Widget")
        product.product_tmpl_id.with_context(lang="fr_FR").name = "Agent Rpc Bidule"
        payload = self.call(
            self.service_user, self.body(search_term="rpc bidule", context={"lang": "fr_FR"})
        ).json()
        self.assertEqual(
            (payload["lang"], [row["product_id"] for row in payload["rows"]]),
            ("fr_FR", product.ids),
        )

    def test_the_grammar_is_enforced_over_rpc(self):
        response = self.call(self.service_user, self.body(max_rows=51))
        self.assertEqual(response.status_code, 422)
        self.assertIn("max-rows-out-of-bounds", response.json()["message"])
        self.assertEqual(self.call(self.service_user, self.body(domain=[])).status_code, 422)

    def test_an_unauthenticated_request_is_refused(self):
        response = self.url_open(
            self.ENDPOINT, data=json.dumps({}), headers={"Content-Type": "application/json"}
        )
        self.assertIn(response.status_code, (401, 403))
