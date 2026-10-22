"""The Odoo half of the signed UI gateway (ADR 0018), on real Odoo 19 over real HTTP.

A stub control plane runs in this test process on a loopback port. It records exactly what the
Odoo route sent and answers what each test scripts. It verifies nothing itself: the tests verify.
The control plane's own verification and gates are tested in ``apps/control_plane/tests/ui_gateway``
and, end to end with a real control plane, in the browser lane.

Claims under test:

* **Offered only when it can work.** Without the signed link nothing real is offered; with it,
  only ``inventory.topology``, and only to a user who can read Odoo's inventory structure. A user
  without that access is refused before the control plane is contacted.
* **Signed server-side, bound to the session.** The request carries ``env.user`` and
  ``env.company`` (the user's active company), a fresh nonce each time, and a signature that
  verifies under the configured key; the browser receives none of the URL, key, headers or body.
* **Every outcome keeps its kind.** A control-plane refusal (company, deployment gate) is forwarded
  as it is; an unreachable control plane is temporary; a rejected signature is policy-blocked; a
  malformed or demo-flagged reply is never forwarded.
* **The end user's own access decides what is shown (ADR 0019).** Every cited record is re-read as
  the session user in the active company. A record rule narrower than company scope, a record of
  another allowed company, or a record that does not exist refuses the whole answer as
  ``permission`` / ``record-access-narrower``; a citation of any other model, or a repeated one, is
  an invalid reply. Nothing of a refused answer reaches the browser.
"""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

from ..ui_protocol import gateway

UI_GROUP = "odoo_agent_ui.group_agent_ui_user"
KEY_HEX = "5a" * 32
KEY_ID = "odoo-ui-test"


def completed(request_body, *citations):
    """A completed stub answer citing ``(model, record_id)`` pairs, a warehouse by default."""

    run_id = f"run_{uuid.UUID(json.loads(request_body)['request_id']).hex}"
    cited = [
        (
            "record.cited",
            {
                "model": model,
                "record_id": record_id,
                "display_name": "Stub warehouse",
                "fields": [{"label": "Code", "value": "WH"}],
            },
        )
        for model, record_id in citations
    ]
    events = [
        ("run.accepted", {"workflow_id": "inventory.topology"}),
        ("message.completed", {"text": "Company has 1 warehouse (stub)."}),
        *cited,
        ("run.completed", {}),
    ]
    return {
        "schema_version": "1",
        "contract": "odoo_agent_ui.run",
        "run_id": run_id,
        "demo": False,
        "events": [
            {
                "schema_version": "1",
                "type": kind,
                "run_id": run_id,
                "sequence": index,
                "demo": False,
                **values,
            }
            for index, (kind, values) in enumerate(events)
        ],
    }


def refused(request_body, kind, code):
    run_id = f"run_{uuid.UUID(json.loads(request_body)['request_id']).hex}"
    return {
        "schema_version": "1",
        "contract": "odoo_agent_ui.run",
        "run_id": run_id,
        "demo": False,
        "events": [
            {
                "schema_version": "1",
                "type": "run.failed",
                "run_id": run_id,
                "sequence": 0,
                "demo": False,
                "failure": {
                    "kind": kind,
                    "code": code,
                    "title": "Refused (stub)",
                    "detail": "The stub control plane refused.",
                    "retryable": kind == "temporary",
                    "no_action_performed": True,
                },
            }
        ],
    }


class StubControlPlane:
    def __init__(self):
        self.requests = []
        self.reply = lambda body: (200, completed(body))  # replaced by each test's citations
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                headers = {name.lower(): value for name, value in self.headers.items()}
                stub.requests.append((self.path, headers, body))
                status, payload = stub.reply(body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@tagged("post_install", "-at_install")
class TestUiGatewayLink(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "Agent UI Link Alpha"})
        cls.company_b = cls.env["res.company"].create({"name": "Agent UI Link Beta"})
        groups = [cls.env.ref("base.group_user").id, cls.env.ref(UI_GROUP).id]
        cls.user = cls.env["res.users"].create(
            {
                "name": "agent.ui.link",
                "login": "agent.ui.link",
                "password": "agent.ui.link-pw",
                "group_ids": [(6, 0, groups)],
                "company_id": cls.company_a.id,
                "company_ids": [(6, 0, [cls.company_a.id, cls.company_b.id])],
            }
        )
        warehouses = cls.env["stock.warehouse"]
        cls.warehouse_a = warehouses.create(
            {"name": "Link Alpha One", "code": "LA1", "company_id": cls.company_a.id}
        )
        cls.warehouse_a2 = warehouses.create(
            {"name": "Link Alpha Two", "code": "LA2", "company_id": cls.company_a.id}
        )
        cls.warehouse_b = warehouses.create(
            {"name": "Link Beta One", "code": "LB1", "company_id": cls.company_b.id}
        )
        # A record rule narrower than company scope: members of this group see LA1 only.
        cls.narrow_group = cls.env["res.groups"].create({"name": "Agent UI link narrow scope"})
        cls.env["ir.rule"].create(
            {
                "name": "Agent UI link: one warehouse only",
                "model_id": cls.env.ref("stock.model_stock_warehouse").id,
                "groups": [(6, 0, [cls.narrow_group.id])],
                "domain_force": "[('code', '=', 'LA1')]",
                "perm_read": True,
                "perm_write": False,
                "perm_create": False,
                "perm_unlink": False,
            }
        )

    def setUp(self):
        super().setUp()
        self.stub = StubControlPlane()
        self.addCleanup(self.stub.close)
        self.stub.reply = lambda body: (
            200,
            completed(body, ("stock.warehouse", self.warehouse_a.id)),
        )
        self.environment = {
            gateway.URL_VARIABLE: self.stub.url,
            gateway.KEY_ID_VARIABLE: KEY_ID,
            gateway.KEY_VARIABLE: KEY_HEX,
        }

    def rpc(self, route, params=None, *, linked=True):
        self.authenticate(self.user.login, "agent.ui.link-pw")
        environment = self.environment if linked else {}
        with patch.dict(os.environ, environment):
            for name in gateway.URL_VARIABLE, gateway.KEY_ID_VARIABLE, gateway.KEY_VARIABLE:
                if not linked:
                    os.environ.pop(name, None)
            response = self.url_open(
                route,
                data=json.dumps(
                    {"jsonrpc": "2.0", "method": "call", "id": 1, "params": params or {}}
                ),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("error", payload, payload.get("error"))
        return payload["result"], response.text

    def ask(self, **kwargs):
        return self.rpc(
            "/odoo_agent_ui/run",
            {
                "workflow_id": "inventory.topology",
                "message": "Where are my warehouses?",
                "attempt": 1,
            },
            **kwargs,
        )

    def availability(self, boot):
        return {w["id"]: (w["availability"], w["reason"]) for w in boot["workflows"]}

    def revoke_inventory_read(self):
        access = self.env["ir.model.access"].search(
            [
                (
                    "model_id.model",
                    "in",
                    ["stock.warehouse", "stock.location", "stock.picking.type"],
                ),
                ("perm_read", "=", True),
            ]
        )
        access.filtered(lambda a: not a.group_id or a.group_id in self.user.all_group_ids).write(
            {"perm_read": False}
        )

    def failure(self, run):
        self.assertIs(run["demo"], False)
        return run["events"][-1]["failure"]

    def test_without_the_link_nothing_real_is_offered_or_sent(self):
        boot, _ = self.rpc("/odoo_agent_ui/bootstrap", linked=False)
        self.assertEqual(boot["control_plane"], "not-connected")
        self.assertEqual(
            self.availability(boot)["inventory.topology"],
            ("unavailable", "control-plane-not-connected"),
        )
        run, _ = self.ask(linked=False)
        self.assertEqual(self.failure(run)["code"], "control-plane-not-connected")
        self.assertEqual(self.stub.requests, [])

    def test_with_the_link_only_topology_is_offered_to_a_user_who_can_read_inventory(self):
        boot, text = self.rpc("/odoo_agent_ui/bootstrap")
        self.assertEqual(boot["control_plane"], "connected")
        available = {k for k, (state, _) in self.availability(boot).items() if state == "available"}
        self.assertEqual(available, {"inventory.topology"})
        self.assertEqual(
            self.availability(boot)["inventory.stock"], ("unavailable", "workflow-not-enabled")
        )
        for secret in (KEY_HEX, self.stub.url, KEY_ID):
            self.assertNotIn(secret, text)

    def test_a_user_without_inventory_read_access_is_refused_before_the_control_plane(self):
        self.revoke_inventory_read()
        boot, _ = self.rpc("/odoo_agent_ui/bootstrap")
        self.assertEqual(
            self.availability(boot)["inventory.topology"], ("unavailable", "odoo-access-missing")
        )
        run, _ = self.ask()
        self.assertEqual(
            (self.failure(run)["kind"], self.failure(run)["code"]),
            ("permission", "odoo-access-missing"),
        )
        self.assertEqual(self.stub.requests, [])

    def test_a_run_is_signed_server_side_for_the_session_user_and_active_company(self):
        self.user.company_id = self.company_b
        self.stub.reply = lambda body: (
            200,
            completed(body, ("stock.warehouse", self.warehouse_b.id)),
        )
        run, text = self.ask()
        again, _ = self.ask()
        self.assertEqual(run["events"][-1]["type"], "run.completed")
        self.assertEqual(len(self.stub.requests), 2)
        (path, headers, body), (_, second_headers, second_body) = self.stub.requests
        self.assertEqual(path, gateway.RUN_PATH)
        sent = json.loads(body)
        self.assertEqual(
            (sent["odoo_user_id"], sent["odoo_company_id"], sent["workflow_id"]),
            (self.user.id, self.company_b.id, "inventory.topology"),
        )
        expected = "sha256=" + gateway.signature_hex(
            bytes.fromhex(KEY_HEX),
            headers[gateway.KEY_ID_HEADER],
            headers[gateway.TIMESTAMP_HEADER],
            headers[gateway.NONCE_HEADER],
            body,
        )
        self.assertEqual(headers[gateway.SIGNATURE_HEADER], expected)
        self.assertNotEqual(headers[gateway.NONCE_HEADER], second_headers[gateway.NONCE_HEADER])
        self.assertNotEqual(json.loads(body)["request_id"], json.loads(second_body)["request_id"])
        self.assertEqual(run["run_id"], f"run_{uuid.UUID(sent['request_id']).hex}")
        for secret in (
            KEY_HEX,
            self.stub.url,
            headers[gateway.SIGNATURE_HEADER],
            headers[gateway.NONCE_HEADER],
        ):
            self.assertNotIn(secret, text)
        self.assertIs(again["demo"], False)

    def test_control_plane_refusals_are_forwarded_with_their_kind(self):
        for kind, code in (
            ("permission", "company-not-authorized"),
            ("policy_blocked", "deployment-attestation-required"),
        ):
            with self.subTest(code=code):
                self.stub.reply = lambda body, kind=kind, code=code: (
                    200,
                    refused(body, kind, code),
                )
                run, _ = self.ask()
                self.assertEqual(
                    (self.failure(run)["kind"], self.failure(run)["code"]), (kind, code)
                )

    def test_an_unreachable_control_plane_is_a_temporary_failure(self):
        self.stub.close()
        run, _ = self.ask()
        failure = self.failure(run)
        self.assertEqual(
            (failure["kind"], failure["code"], failure["retryable"]),
            ("temporary", "control-plane-unavailable", True),
        )

    def test_a_rejected_signature_is_a_policy_failure_and_a_bad_reply_is_never_forwarded(self):
        self.stub.reply = lambda body: (
            401,
            {"schema_version": "1", "code": "authentication-failed"},
        )
        run, _ = self.ask()
        self.assertEqual(self.failure(run)["code"], "control-plane-rejected-request")
        cited = ("stock.warehouse", self.warehouse_a.id)
        for bad in (
            lambda body: (200, {**completed(body, cited), "demo": True}),
            lambda body: (200, {**completed(body, cited), "run_id": "run_someone_else"}),
            lambda body: (200, b"not json"),
            lambda body: (200, completed(body, ("res.partner", self.user.partner_id.id))),
            lambda body: (200, completed(body, cited, cited)),
            lambda body: (200, completed(body, ("stock.warehouse", -1))),
        ):
            self.stub.reply = bad
            run, text = self.ask()
            self.assertEqual(self.failure(run)["code"], "control-plane-response-invalid")
            self.assertNotIn("Stub warehouse", text)
        self.stub.reply = lambda body: (
            503,
            {"schema_version": "1", "code": "ui-gateway-unavailable"},
        )
        run, _ = self.ask()
        self.assertEqual(self.failure(run)["code"], "control-plane-unavailable")

    def outcome(self, *citations):
        self.stub.reply = lambda body: (200, completed(body, *citations))
        run, text = self.ask()
        last = run["events"][-1]
        if last["type"] == "run.completed":
            return "completed", text
        return (last["failure"]["kind"], last["failure"]["code"]), text

    def test_an_answer_is_shown_only_when_the_user_can_read_every_cited_record(self):
        both = (("stock.warehouse", self.warehouse_a.id), ("stock.warehouse", self.warehouse_a2.id))
        location = ("stock.location", self.warehouse_a.lot_stock_id.id)
        picking_type = ("stock.picking.type", self.warehouse_a.in_type_id.id)
        self.assertEqual(self.outcome(*both, location, picking_type)[0], "completed")

        self.user.group_ids = [(4, self.narrow_group.id)]
        refused_outcome, text = self.outcome(*both)
        self.assertEqual(refused_outcome, ("permission", "record-access-narrower"))
        self.assertNotIn("Stub warehouse", text)
        self.assertEqual(self.outcome(both[0])[0], "completed")

    def test_a_record_outside_the_active_company_or_missing_refuses_the_answer(self):
        # The user is allowed company B, but the active company is A: B's record is not shown.
        other_company = ("stock.warehouse", self.warehouse_b.id)
        self.assertEqual(self.outcome(other_company)[0], ("permission", "record-access-narrower"))
        missing = ("stock.warehouse", self.warehouse_b.id + 100000)
        self.assertEqual(self.outcome(missing)[0], ("permission", "record-access-narrower"))

    def test_a_failed_run_is_forwarded_without_a_record_check(self):
        self.user.group_ids = [(4, self.narrow_group.id)]
        self.stub.reply = lambda body: (
            200,
            refused(body, "policy_blocked", "deployment-attestation-refused"),
        )
        run, _ = self.ask()
        self.assertEqual(self.failure(run)["code"], "deployment-attestation-refused")
