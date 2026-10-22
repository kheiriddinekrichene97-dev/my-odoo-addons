"""Integration tests for the ``quant_company_v1`` checked projection, on real Odoo 19.

The claims under test, against Odoo itself:

* **The pinned semantics are what the core assumes.** ``stock.quant.company_id`` is measured to be
  a stored, elevated related field on ``location_id.company_id``; ``stock.location.company_id`` a
  plain, optional, typed ``Many2one('res.company')``; and both record rules filter on those stored
  values.
* **The stored related copy is never an answer.** The company comes from the location, re-read as
  the caller, only after the quant row, the three fields, the location row and any company row are
  proven readable. A stale copy is refused, not repaired and not forwarded.
* **What cannot be proven is pinned, not argued away.** A stale copy can hide a quant from the
  company it really belongs to; no caller-rights read reveals that.

Hidden rows come from real record rules scoped to the service group, missing rights from revoking
read ACLs inside the test transaction, field restrictions from the ``groups`` attribute, and stale
values from SQL (the ORM refuses to change a location's company, and recomputes the stored copy
when it could). Nothing here grants a right.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
PROJECTION_MODEL = "odoo.agent.quant.company.projection"
METHOD = "agent_projected_quant_company_v1"


class QuantCase(TransactionCase):
    """Two companies with a location each, a shared location, and three identities."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent Quant Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent Quant Beta"})
        cls.location_a = cls._location("Agent Quant Alpha Shelf", cls.company_a)
        cls.location_b = cls._location("Agent Quant Beta Shelf", cls.company_b)
        cls.shared = cls._location("Agent Quant Shared Shelf", False)
        cls.product = cls.env["product.product"].create(
            {"name": "Agent Quant Widget", "type": "consu", "is_storable": True}
        )
        cls.service_user = cls._user("agent.quant.service", [cls.company_a], service=True)
        cls.ordinary_user = cls._user("agent.quant.ordinary", [cls.company_a], service=False)
        cls.both_companies_user = cls._user(
            "agent.quant.both", [cls.company_a, cls.company_b], service=True
        )
        cls.beta_user = cls._user("agent.quant.beta", [cls.company_b], service=True)

    @classmethod
    def _location(cls, name, company):
        return cls.env["stock.location"].create(
            {"name": name, "usage": "internal", "company_id": company.id if company else False}
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

    def quant(self, location, quantity=5.0):
        self.env["stock.quant"]._update_available_quantity(self.product, location, quantity)
        quant = self.env["stock.quant"]._gather(self.product, location, strict=True)
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

    def companies_of(self, user=None, companies=None):
        self.env.flush_all()
        env = self.env(user=user or self.service_user, su=False)
        if companies is not None:
            env = env(context=dict(env.context, allowed_company_ids=[c.id for c in companies]))
        return env

    def project(self, quants, user=None, *, companies=None):
        ids = quants if isinstance(quants, list) else quants.ids
        env = self.companies_of(user, companies)
        return env[PROJECTION_MODEL].agent_projected_quant_company_v1(ids)

    def only(self, response):
        (row,) = response["results"]
        return row

    def assert_answer(self, row, status, company=False):
        self.assertEqual((row["status"], row["company_id"]), (status, company))


@tagged("post_install", "-at_install")
class TestQuantAuthorization(QuantCase):
    def test_an_ordinary_internal_user_is_refused(self):
        with self.assertRaises(AccessError) as refusal:
            self.project(self.quant(self.location_a), self.ordinary_user)
        self.assertIn("requires-the-service-group", str(refusal.exception))

    def test_the_service_identity_is_allowed(self):
        row = self.only(self.project(self.quant(self.location_a)))
        self.assert_answer(row, "verified", self.company_a.id)

    def test_superuser_mode_is_refused(self):
        with self.assertRaises(UserError) as refusal:
            self.env[PROJECTION_MODEL].sudo().agent_projected_quant_company_v1(
                self.quant(self.location_a).ids
            )
        self.assertIn("refuses-superuser-mode", str(refusal.exception))

    def test_superuser_refusal_precedes_the_group_check(self):
        with self.assertRaises(UserError) as refusal:
            self.env(user=self.ordinary_user)[
                PROJECTION_MODEL
            ].sudo().agent_projected_quant_company_v1(self.quant(self.location_a).ids)
        self.assertIn("refuses-superuser-mode", str(refusal.exception))


@tagged("post_install", "-at_install")
class TestQuantPinnedSemantics(QuantCase):
    """Measured on the running registry, so a module that redeclares a field fails here."""

    def test_the_quant_company_is_a_stored_elevated_related_copy_of_the_location(self):
        field = self.env["stock.quant"]._fields["company_id"]
        self.assertEqual(
            (field.type, field.related, field.store, field.compute_sudo),
            ("many2one", "location_id.company_id", True, True),
        )
        link = self.env["stock.quant"]._fields["location_id"]
        self.assertEqual(
            (
                link.comodel_name,
                link.store,
                link.required,
                link.ondelete,
                link.related,
                link.groups,
            ),
            ("stock.location", True, True, "restrict", None, None),
        )

    def test_the_location_company_is_a_plain_optional_typed_many2one(self):
        field = self.env["stock.location"]._fields["company_id"]
        self.assertEqual(
            (
                field.type,
                field.comodel_name,
                field.store,
                field.required,
                field.compute,
                field.related,
                field.groups,
                field.company_dependent,
            ),
            ("many2one", "res.company", True, False, None, None, None, False),
        )
        # The ORM refuses to move a location to another company, so a stored copy can only
        # disagree with its location after an out-of-ORM write.
        with self.assertRaises(UserError):
            self.location_a.write({"company_id": self.company_b.id})

    def test_both_record_rules_filter_on_the_stored_company(self):
        for model in ("stock.quant", "stock.location"):
            with self.subTest(model=model):
                rules = self.env["ir.rule"].search(
                    [("model_id.model", "=", model), ("global", "=", True)]
                )
                self.assertEqual(
                    rules.mapped("domain_force"),
                    ["[('company_id', 'in', company_ids + [False])]"],
                )

    def test_verified_answers_equal_odoos_own_value_on_consistent_data(self):
        quants = self.quant(self.location_a) | self.quant(self.shared)
        response = self.project(quants)
        self.assertEqual(
            [row["company_id"] for row in response["results"]],
            [quant.company_id.id or False for quant in quants],
        )
        self.assertEqual([row["status"] for row in response["results"]], ["verified"] * 2)


@tagged("post_install", "-at_install")
class TestQuantVisibility(QuantCase):
    def test_a_restricted_service_identity_gets_a_verified_company(self):
        user = self.service_user
        self.assertFalse(user._is_admin() or user._is_system())
        self.assertFalse(user.has_group("stock.group_stock_manager"))
        self.assert_answer(
            self.only(self.project(self.quant(self.location_a))), "verified", self.company_a.id
        )

    def test_a_location_hidden_by_a_rule_refuses_a_visible_quant(self):
        quant = self.quant(self.location_a)
        self.hide_from_service("stock.location", f"[('id', '!=', {self.location_a.id})]")
        self.assertTrue(quant.with_user(self.service_user).has_access("read"))
        self.assert_answer(self.only(self.project(quant)), "location-not-visible")

    def test_a_location_without_read_access_refuses_a_visible_quant(self):
        quant = self.quant(self.location_a)
        self.revoke_read("stock.location", self.service_user)
        self.assertTrue(quant.with_user(self.service_user).has_access("read"))
        self.assert_answer(self.only(self.project(quant)), "location-not-visible")

    def test_a_location_value_already_in_the_cache_is_not_forwarded(self):
        quant = self.quant(self.location_a)
        self.revoke_read("stock.location", self.service_user)
        self.env.flush_all()
        self.assertEqual(self.location_a.company_id, self.company_a)  # superuser warms the cache
        as_service = self.location_a.with_user(self.service_user)
        self.assertEqual(as_service.company_id.id, self.company_a.id)  # served without a check
        env = self.env(user=self.service_user, su=False)
        response = env[PROJECTION_MODEL].agent_projected_quant_company_v1(quant.ids)
        self.assert_answer(self.only(response), "location-not-visible")

    def test_a_hidden_company_refuses_the_quant(self):
        """``res.company``'s standard rules are group rules, OR-ed together, so hiding a company
        takes a global rule (AND-ed). The location stays readable; its company row does not."""

        quant = self.quant(self.location_b)
        self.env["ir.rule"].create(
            {
                "name": "agent test: hide one company row from every non-superuser",
                "model_id": self.env["ir.model"]._get_id("res.company"),
                "domain_force": f"[('id', '!=', {self.company_b.id})]",
            }
        )
        user, companies = self.both_companies_user, [self.company_a, self.company_b]
        location = self.location_b.with_user(user).with_context(
            allowed_company_ids=[c.id for c in companies]
        )
        self.assertTrue(location.has_access("read"))
        self.assertFalse(location.company_id.has_access("read"))
        self.assert_answer(
            self.only(self.project(quant, user, companies=companies)), "company-not-visible"
        )

    def test_each_field_restricted_by_groups_refuses_the_quant(self):
        quant = self.quant(self.location_a)
        for model, name in (
            ("stock.quant", "location_id"),
            ("stock.quant", "company_id"),
            ("stock.location", "company_id"),
        ):
            with (
                self.subTest(field=f"{model}.{name}"),
                patch.object(self.env[model]._fields[name], "groups", "base.group_system"),
            ):
                self.env.invalidate_all()
                self.assert_answer(self.only(self.project(quant)), "field-not-readable")
        self.assert_answer(self.only(self.project(quant)), "verified", self.company_a.id)

    def test_a_hidden_quant_and_a_missing_id_look_the_same(self):
        hidden = self.quant(self.location_a)
        self.hide_from_service("stock.quant", f"[('id', '!=', {hidden.id})]")
        response = self.project([hidden.id, 99999999])
        self.assertEqual(
            [(row["status"], row["company_id"]) for row in response["results"]],
            [("quant-not-visible", False)] * 2,
        )

    def test_one_refusal_does_not_change_another_answer_and_order_is_kept(self):
        first, second = self.quant(self.location_a), self.quant(self.shared)
        hidden = self.quant(self.location_b)
        response = self.project([second.id, hidden.id, first.id, second.id])
        self.assertEqual(
            [(row["quant_id"], row["status"], row["company_id"]) for row in response["results"]],
            [
                (second.id, "verified", False),
                (hidden.id, "quant-not-visible", False),
                (first.id, "verified", self.company_a.id),
            ],
        )


@tagged("post_install", "-at_install")
class TestQuantCompanies(QuantCase):
    def test_another_companys_quant_is_not_visible(self):
        self.assert_answer(
            self.only(self.project(self.quant(self.location_b))), "quant-not-visible"
        )

    def test_the_active_companies_decide_not_the_users_company_list(self):
        quant = self.quant(self.location_b)
        user = self.both_companies_user
        self.assert_answer(
            self.only(self.project(quant, user, companies=[self.company_a])), "quant-not-visible"
        )
        self.assert_answer(
            self.only(self.project(quant, user, companies=[self.company_a, self.company_b])),
            "verified",
            self.company_b.id,
        )

    def test_a_quant_in_a_shared_location_is_verified_as_shared_for_every_company(self):
        quant = self.quant(self.shared)
        for user in (self.service_user, self.beta_user):
            with self.subTest(user=user.login):
                self.assert_answer(self.only(self.project(quant, user)), "verified", False)


@tagged("post_install", "-at_install")
class TestQuantStaleValues(QuantCase):
    def stale(self, quant, company):
        self.sql(
            "UPDATE stock_quant SET company_id = %s WHERE id = %s",
            (company.id if company else None, quant.id),
        )

    def test_a_stale_copy_naming_another_active_company_is_refused(self):
        quant = self.quant(self.location_a)
        self.stale(quant, self.company_b)
        user = self.both_companies_user
        companies = [self.company_a, self.company_b]
        self.assertEqual(quant.company_id, self.company_b)  # the stored copy, read back
        self.assertEqual(quant.location_id.company_id, self.company_a)  # its source
        self.assert_answer(
            self.only(self.project(quant, user, companies=companies)), "company-conflicting"
        )

    def test_a_location_moved_in_sql_leaves_the_copy_stale_and_is_refused(self):
        quant = self.quant(self.location_a)
        self.sql(
            "UPDATE stock_location SET company_id = %s WHERE id = %s",
            (self.company_b.id, self.location_a.id),
        )
        user = self.both_companies_user
        self.assert_answer(
            self.only(self.project(quant, user, companies=[self.company_a, self.company_b])),
            "company-conflicting",
        )

    def test_a_stale_shared_copy_shows_a_quant_whose_location_is_refused(self):
        """Odoo's quant rule admits the row on the stale ``False``; the location rule does not
        admit its location. Refused, and to its own company the copy disagrees."""

        quant = self.quant(self.location_a)
        self.stale(quant, False)
        self.assertTrue(quant.with_user(self.beta_user).has_access("read"))
        self.assert_answer(self.only(self.project(quant, self.beta_user)), "location-not-visible")
        self.assert_answer(self.only(self.project(quant)), "company-conflicting")

    def test_the_gap_a_stale_copy_hides_a_quant_from_its_own_company(self):
        """Recorded, not closed: the quant rule filters on the elevated copy, so a quant that
        belongs to the caller's company by its location is invisible and indistinguishable from
        a missing id."""

        quant = self.quant(self.location_a)
        self.stale(quant, self.company_b)
        self.assertEqual(quant.location_id.company_id, self.company_a)
        self.assertFalse(quant.with_user(self.service_user).has_access("read"))
        response = self.project([quant.id, 99999999])
        self.assertEqual(response["results"][0]["status"], "quant-not-visible")
        self.assertEqual(
            {k: v for k, v in response["results"][0].items() if k != "quant_id"},
            {k: v for k, v in response["results"][1].items() if k != "quant_id"},
        )


@tagged("post_install", "-at_install")
class TestQuantBounds(QuantCase):
    def test_the_id_bound_is_inclusive_and_one_over_is_refused(self):
        env = self.companies_of()[PROJECTION_MODEL]
        self.assertEqual(
            len(env.agent_projected_quant_company_v1(list(range(1, 201)))["results"]), 200
        )
        with self.assertRaises(UserError) as refusal:
            env.agent_projected_quant_company_v1(list(range(1, 202)))
        self.assertIn("quant-ids-over-bound", str(refusal.exception))

    def test_the_argument_grammar(self):
        env = self.companies_of()[PROJECTION_MODEL]
        for value in ([], [0], [True], ["1"], "1", None, [1.5]):
            with self.subTest(value=value), self.assertRaises(UserError):
                env.agent_projected_quant_company_v1(value)

    def test_the_response_envelope_is_closed(self):
        response = self.project(self.quant(self.location_a) | self.quant(self.location_b))
        self.assertEqual(list(response), ["schema_version", "projection", "results"])
        self.assertEqual(response["projection"], "quant_company_v1")
        for row in response["results"]:
            self.assertEqual(list(row), ["quant_id", "status", "company_id"])


@tagged("post_install", "-at_install")
class TestQuantRpcDispatch(HttpCase, QuantCase):
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
        response = self.call(self.ordinary_user, {"quant_ids": self.quant(self.location_a).ids})
        self.assertEqual(response.status_code, 403)
        self.assertIn("requires-the-service-group", response.json()["message"])

    def test_the_service_identity_gets_typed_answers_over_rpc(self):
        mine, shared, other = (
            self.quant(self.location_a),
            self.quant(self.shared),
            self.quant(self.location_b),
        )
        response = self.call(self.service_user, {"quant_ids": [mine.id, shared.id, other.id]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(row["status"], row["company_id"]) for row in response.json()["results"]],
            [("verified", self.company_a.id), ("verified", False), ("quant-not-visible", False)],
        )
        self.assertIs(type(response.json()["results"][0]["company_id"]), int)

    def test_a_hidden_location_is_refused_over_rpc(self):
        quant = self.quant(self.location_a)
        self.hide_from_service("stock.location", f"[('id', '!=', {self.location_a.id})]")
        response = self.call(self.service_user, {"quant_ids": quant.ids})
        self.assert_answer(response.json()["results"][0], "location-not-visible")

    def test_the_grammar_is_enforced_over_rpc(self):
        response = self.call(self.service_user, {"quant_ids": [True]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("quant-id-not-a-positive-integer", response.json()["message"])

    def test_an_unauthenticated_request_is_refused(self):
        response = self.url_open(
            self.ENDPOINT, data=json.dumps({}), headers={"Content-Type": "application/json"}
        )
        self.assertIn(response.status_code, (401, 403))
