"""Integration tests for the ``picking_company_v1`` checked projection, against a real Odoo 19 DB.

Run by Odoo's own runner against a real database with real record rules, real ACLs, real
multi-company behaviour and the real RPC dispatcher::

    odoo -d <db> -i odoo_agent_gateway --test-enable --test-tags /odoo_agent_gateway

These complement, and do not replace, the fake-ORM suite in ``unit_tests/``. The fake pins the order
and the fail-closed shape of our own decisions; these pin that the ORM actually behaves the way the
projection assumes. Where the two disagree, the ORM is right.

## The finding these exist because of

An earlier revision of this addon granted its model's ACL to ``base.group_user`` and argued that the
projection was unreachable because the control plane's ``_READ_METHOD_ALLOWLIST`` did not name it.
That argument was wrong, and measurably so: Odoo dispatches ``/json/2/<model>/<method>`` through
``get_public_method`` (``odoo/service/model.py:45``) and then calls the function, with **no**
``check_access`` on the path. An allowlist in our client constrains our client. It does not
constrain anybody else's, and it is not an Odoo authorization boundary.

``TestProjectionRpcDispatch`` is the regression test for that gap, and it goes over real HTTP
through ``/json/2/`` rather than calling the method in process, because the claim being tested is
about Odoo's dispatcher and not about our method.
"""

import json
from datetime import datetime, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
PROJECTION_MODEL = "odoo.agent.picking.company.projection"
METHOD = "agent_projected_picking_company_v1"


class ProjectionCase(TransactionCase):
    """Two companies, two operation types, two pickings, and three identities."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent Test Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent Test Beta"})

        cls.type_a = cls._operation_type(cls.company_a, "Alpha In")
        cls.type_b = cls._operation_type(cls.company_b, "Beta In")
        cls.picking_a = cls._picking(cls.type_a)
        cls.picking_b = cls._picking(cls.type_b)

        cls.service_user = cls._user("agent.service", [cls.company_a], service=True)
        cls.ordinary_user = cls._user("agent.ordinary", [cls.company_a], service=False)
        cls.both_companies_user = cls._user(
            "agent.both", [cls.company_a, cls.company_b], service=True
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
    def _picking(cls, picking_type):
        company = picking_type.company_id
        stock = cls.env["stock.location"].search(
            [("usage", "=", "internal"), ("company_id", "in", [company.id, False])], limit=1
        )
        customers = cls.env.ref("stock.stock_location_customers")
        return cls.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": stock.id,
                "location_dest_id": customers.id,
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

    # -- invocation ---------------------------------------------------------------------

    def project(self, user, picking_ids, companies=None):
        """Call the projection exactly as a caller would, as ``user``, never as superuser."""

        env = self.env(user=user, su=False)
        if companies is not None:
            env = env(context=dict(env.context, allowed_company_ids=[c.id for c in companies]))
        return env[PROJECTION_MODEL].agent_projected_picking_company_v1(picking_ids)

    def statuses(self, response):
        return [
            (row["picking_id"], row["status"], row["company_id"]) for row in response["results"]
        ]

    def force_stored_company(self, picking, company):
        """Write the stored related column directly, creating a divergence Odoo cannot.

        ``stock.picking.company_id`` is ``related='picking_type_id.company_id'`` and stored, so the
        ORM keeps it in step. A real database can still hold a stale value -- an interrupted
        recompute, a data migration, a direct write -- and that is exactly the case the projection's
        tripwire exists for, so the test manufactures it at the SQL level.
        """

        self.env.cr.execute(
            "UPDATE stock_picking SET company_id = %s WHERE id = %s", (company.id, picking.id)
        )
        self.env.invalidate_all()


@tagged("post_install", "-at_install")
class TestProjectionAuthorization(ProjectionCase):
    """Who may invoke the method at all. This is the gate, and it is not the ACL."""

    def test_an_ordinary_internal_user_is_refused(self):
        """A user with Inventory rights but not the service group cannot call it.

        Regression test. Before the service group existed this returned HTTP 200 and a well-formed
        projection response to any internal user, because Odoo's RPC dispatch performs no access
        check before calling a public method.
        """

        with self.assertRaises(AccessError) as refusal:
            self.project(self.ordinary_user, [self.picking_a.id])
        self.assertIn("requires-the-service-group", str(refusal.exception))

    def test_the_service_identity_is_allowed(self):
        response = self.project(self.service_user, [self.picking_a.id])
        self.assertEqual(
            self.statuses(response), [(self.picking_a.id, "verified", self.company_a.id)]
        )

    def test_the_service_group_is_unassigned_on_install(self):
        """Nobody holds it out of the box, including the root and admin users.

        If an upgrade ever attaches it to a user or lets another group imply it, the default state
        stops being "no one can call this" and this fails.
        """

        group = self.env.ref(SERVICE_GROUP)
        installed = group.user_ids.filtered(
            lambda user: user.login not in {"agent.service", "agent.both"}
        )
        self.assertFalse(installed, installed.mapped("login"))
        self.assertFalse(group.implied_ids)
        # And nothing else implies it, so no existing role silently confers it -- checked
        # transitively via `all_implied_ids`, because a two-hop implication would confer it just
        # as effectively as a direct one.
        self.assertFalse(self.env["res.groups"].search([("implied_ids", "in", group.ids)]))
        self.assertNotIn(group, self.env.ref("base.group_user").all_implied_ids)
        conferring = self.env["res.groups"].search([("all_implied_ids", "in", group.ids)]) - group
        self.assertFalse(conferring, conferring.mapped("name"))

    def test_superuser_mode_is_refused(self):
        """Under ``env.su`` every access check short-circuits, so the proof would be vacuous."""

        with self.assertRaises(UserError) as refusal:
            self.env[PROJECTION_MODEL].sudo().agent_projected_picking_company_v1(
                [self.picking_a.id]
            )
        self.assertIn("refuses-superuser-mode", str(refusal.exception))

    def test_superuser_refusal_precedes_the_group_check(self):
        """A sudo call by an unauthorized user is refused for being sudo, not for the group.

        Order matters: if the group check ran first, ``sudo()`` would satisfy it and the superuser
        refusal would never be reached.
        """

        with self.assertRaises(UserError) as refusal:
            self.env(user=self.ordinary_user)[
                PROJECTION_MODEL
            ].sudo().agent_projected_picking_company_v1([self.picking_a.id])
        self.assertIn("refuses-superuser-mode", str(refusal.exception))


@tagged("post_install", "-at_install")
class TestProjectionVisibility(ProjectionCase):
    """What the projection concludes when a dependency is genuinely invisible."""

    def test_a_picking_in_another_company_is_not_visible(self):
        response = self.project(self.service_user, [self.picking_b.id])
        self.assertEqual(
            self.statuses(response), [(self.picking_b.id, "picking-not-visible", False)]
        )

    def test_a_missing_picking_is_indistinguishable_from_a_hidden_one(self):
        missing = self.project(self.service_user, [2_000_000_000])
        hidden = self.project(self.service_user, [self.picking_b.id])
        self.assertEqual(missing["results"][0]["status"], hidden["results"][0]["status"])
        self.assertEqual(missing["results"][0]["company_id"], False)

    def test_a_visible_picking_whose_operation_type_is_hidden_fails_closed(self):
        """The case the projection exists for, reproduced through real record rules.

        The stored company is forced to the caller's own company, so ``stock.picking``'s
        multi-company rule admits the row; its operation type still belongs to the other company, so
        ``stock.picking.type``'s rule hides it. Odoo's related field would have resolved that hop as
        superuser and handed back a company. The projection refuses.
        """

        self.force_stored_company(self.picking_b, self.company_a)
        response = self.project(self.service_user, [self.picking_b.id])
        self.assertEqual(
            self.statuses(response), [(self.picking_b.id, "operation-type-not-visible", False)]
        )

    def test_a_field_the_caller_may_not_read_fails_closed(self):
        """Field-level access is not row-level access, and an ``AccessError`` is an answer.

        ``groups`` on a field is how a real deployment restricts a column, so the test restricts
        ``picking_type_id`` to a group the service identity does not hold and checks the projection
        refuses instead of raising out of the method.
        """

        field = self.env["stock.picking"]._fields["picking_type_id"]
        self.patch(field, "groups", "base.group_system")
        response = self.project(self.service_user, [self.picking_a.id])
        self.assertEqual(
            self.statuses(response), [(self.picking_a.id, "operation-type-missing", False)]
        )


@tagged("post_install", "-at_install")
class TestProjectionCompanies(ProjectionCase):
    """Multi-company behaviour, and the divergence tripwire."""

    def test_a_company_outside_the_active_set_is_refused(self):
        """Visible is not permitted. The user may reach company B, but has not activated it."""

        response = self.project(
            self.both_companies_user, [self.picking_b.id], companies=[self.company_a]
        )
        self.assertEqual(
            self.statuses(response), [(self.picking_b.id, "picking-not-visible", False)]
        )

    def test_both_companies_active_resolves_each_picking_to_its_own_company(self):
        response = self.project(
            self.both_companies_user,
            [self.picking_a.id, self.picking_b.id],
            companies=[self.company_a, self.company_b],
        )
        self.assertEqual(
            self.statuses(response),
            [
                (self.picking_a.id, "verified", self.company_a.id),
                (self.picking_b.id, "verified", self.company_b.id),
            ],
        )

    def test_a_divergent_stored_company_is_refused_not_reconciled(self):
        """A stale elevated copy is an incident, and the projection picks no winner.

        Both companies are active so nothing is hidden; the only problem is that the stored value
        disagrees with the one proven under the caller. No company is returned.
        """

        self.force_stored_company(self.picking_b, self.company_a)
        response = self.project(
            self.both_companies_user,
            [self.picking_b.id],
            companies=[self.company_a, self.company_b],
        )
        self.assertEqual(
            self.statuses(response), [(self.picking_b.id, "company-conflicting", False)]
        )

    def test_a_mixed_request_answers_per_picking(self):
        response = self.project(
            self.service_user, [self.picking_a.id, self.picking_b.id], companies=[self.company_a]
        )
        self.assertEqual(
            self.statuses(response),
            [
                (self.picking_a.id, "verified", self.company_a.id),
                (self.picking_b.id, "picking-not-visible", False),
            ],
        )


@tagged("post_install", "-at_install")
class TestProjectionBounds(ProjectionCase):
    """The argument grammar and the response shape, against the real dispatcher."""

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
        response = self.project(self.service_user, [self.picking_a.id, self.picking_b.id])
        self.assertEqual(list(response), ["schema_version", "projection", "results"])
        self.assertEqual(response["schema_version"], 1)
        self.assertEqual(response["projection"], "picking_company_v1")
        for row in response["results"]:
            self.assertEqual(list(row), ["picking_id", "status", "company_id"])

    def test_the_projection_never_returns_the_stored_company_when_it_diverges(self):
        """Belt and braces: the one value that must never leave is the elevated copy."""

        self.force_stored_company(self.picking_a, self.company_b)
        response = self.project(
            self.both_companies_user,
            [self.picking_a.id],
            companies=[self.company_a, self.company_b],
        )
        self.assertNotEqual(response["results"][0]["company_id"], self.company_b.id)
        self.assertEqual(response["results"][0]["status"], "company-conflicting")


@tagged("post_install", "-at_install")
class TestProjectionRpcDispatch(HttpCase, ProjectionCase):
    """The real dispatcher, over real HTTP, with real API-key authentication.

    The in-process tests above call the method directly. That is not what an attacker does, and it
    is not where the original mistake lived: the claim was that Odoo would not dispatch the call,
    and only a request through ``/json/2/`` can falsify it. Odoo routes that path through
    ``get_public_method`` and then calls the function with no ``check_access``, so the method's own
    group check is the only thing standing between an ordinary internal user and a result.
    """

    ENDPOINT = f"/json/2/{PROJECTION_MODEL}/{METHOD}"

    def api_key_for(self, user):
        return (
            self.env["res.users.apikeys"]
            .with_user(user)
            ._generate(None, f"itest-{user.login}", datetime.now() + timedelta(days=1))
        )

    def call(self, user, picking_ids):
        response = self.url_open(
            self.ENDPOINT,
            data=json.dumps({"picking_ids": picking_ids}),
            headers={
                "Authorization": f"Bearer {self.api_key_for(user)}",
                "Content-Type": "application/json",
            },
        )
        return response

    def test_an_ordinary_internal_user_is_refused_over_rpc(self):
        """The regression test. This returned HTTP 200 and a projection before the group existed."""

        response = self.call(self.ordinary_user, [self.picking_a.id])
        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["name"], "odoo.exceptions.AccessError")
        self.assertIn("requires-the-service-group", payload["message"])

    def test_the_service_identity_is_served_over_rpc(self):
        response = self.call(self.service_user, [self.picking_a.id])
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["projection"], "picking_company_v1")
        self.assertEqual(
            payload["results"],
            [
                {
                    "picking_id": self.picking_a.id,
                    "status": "verified",
                    "company_id": self.company_a.id,
                }
            ],
        )

    def test_the_rpc_path_still_enforces_the_bounds(self):
        """Authorization is not the only thing that has to survive the real transport.

        The status is **422**, not 400: Odoo 19 maps ``UserError`` to Unprocessable Entity on the
        JSON-2 path and ``AccessError`` to 403. Pinned as measured rather than as assumed, because
        a caller distinguishing "refused" from "malformed" will read these codes.
        """

        response = self.call(self.service_user, list(range(1, 202)))
        self.assertEqual(response.status_code, 422)
        self.assertIn("picking-ids-over-bound", response.json()["message"])

    def test_the_two_refusal_kinds_have_distinct_status_codes(self):
        """Measured mapping, so a change in either direction is visible in a diff."""

        unauthorized = self.call(self.ordinary_user, [self.picking_a.id])
        malformed = self.call(self.service_user, [])
        self.assertEqual(unauthorized.status_code, 403)
        self.assertEqual(unauthorized.json()["name"], "odoo.exceptions.AccessError")
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(malformed.json()["name"], "odoo.exceptions.UserError")

    def test_an_unauthenticated_request_is_refused(self):
        response = self.url_open(
            self.ENDPOINT,
            data=json.dumps({"picking_ids": [self.picking_a.id]}),
            headers={"Content-Type": "application/json"},
        )
        self.assertIn(response.status_code, (401, 403))
