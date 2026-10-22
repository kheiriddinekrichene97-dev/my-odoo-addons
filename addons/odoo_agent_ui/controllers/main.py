"""The UI's only server surface: two read-only JSON-RPC routes for signed-in users.

* ``/odoo_agent_ui/bootstrap`` -- the ``UiBootstrapV1`` payload for this user, now.
* ``/odoo_agent_ui/run``       -- one ``UiRunResponseV1``. The workflow is **re-authorized on every
  call** (``catalog.authorize``); a selection made in the browser is a request, never a grant.

Both require ``group_agent_ui_user``, which ships unassigned, checked in the route body: an
authenticated user without it gets ``AccessError``. Both are ``readonly`` and write nothing.

## The one real workflow (ADR 0018)

``inventory.topology`` is offered only when the signed control-plane link is configured in this
Odoo **process** (``ui_protocol/gateway.py``: URL and key from environment variables, never the
database) **and** the user can read ``stock.warehouse``, ``stock.location`` and
``stock.picking.type``. A run is then sent **server-side**: the body names ``env.user`` and
``env.company`` -- the session's user and active company, never browser input -- and is signed with
a fresh nonce and timestamp. The browser receives only the control plane's ``UiRunResponseV1``, or
a transport failure built here; it never sees the URL, the key, a header or the signed body.

The UI group grants no business read. The control plane applies its own workflow, provenance,
deployment and company gates on every run and may refuse; Odoo forwards that refusal as it is.

## The end user's own access decides what is shown (ADR 0019)

The control plane reads as its restricted service identity, so its answer is that identity's view
of an allowed company. Before a completed answer is forwarded, every record it cites is re-read
here **as the session user**, in the active company only, with exactly the fields the citation
shows. Odoo's own ACLs, record rules and field groups decide; if any cited record is not readable,
the whole answer is replaced by ``permission`` / ``record-access-narrower`` and nothing of it
reaches the browser. The answer cites every record it counts, so nothing escapes the check.

## ``sudo``

The single ``sudo()`` reads the system parameter ``database.is_neutralized`` -- a flag Odoo itself
sets when it neutralizes a copy -- because ordinary users cannot read ``ir.config_parameter``. Its
value is used for one boolean and never returned.
"""

import os
import re
import time
import uuid

import requests
from odoo import http
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import request

from ..ui_protocol import catalog, demo, gateway

UI_GROUP = "odoo_agent_ui.group_agent_ui_user"
TOPOLOGY_MODELS = ("stock.warehouse", "stock.location", "stock.picking.type")
TIMEOUT_SECONDS = 20
"""One bound for connect and read together. A single number rather than a ``(connect, read)``
tuple: Odoo's own test harness intercepts ``requests`` and compares the timeout to a number."""
_WORKFLOW_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_REJECTED = frozenset({400, 401, 413, 415})


class AgentUiController(http.Controller):
    @http.route("/odoo_agent_ui/bootstrap", type="jsonrpc", auth="user", readonly=True)
    def bootstrap(self):
        self._require_ui_user()
        return catalog.bootstrap(
            demo_mode=self._demo_mode(),
            connected=self._gateway() is not None,
            topology_readable=self._topology_readable(),
        )

    @http.route("/odoo_agent_ui/run", type="jsonrpc", auth="user", readonly=True)
    def run(self, workflow_id, message, attempt=1):
        self._require_ui_user()
        if (
            type(workflow_id) is not str
            or len(workflow_id) > 80
            or not _WORKFLOW_ID.match(workflow_id)
        ):
            raise UserError("odoo-agent-ui-workflow-id-invalid")
        if type(message) is not str or not message.strip():
            raise UserError("odoo-agent-ui-message-empty")
        if len(message) > demo.MAX_MESSAGE_LENGTH:
            raise UserError("odoo-agent-ui-message-too-long")
        if type(attempt) is not int or not 1 <= attempt <= demo.MAX_ATTEMPTS:
            raise UserError("odoo-agent-ui-attempt-out-of-bounds")

        config = self._gateway()
        allowed, reason = catalog.authorize(
            workflow_id,
            demo_mode=self._demo_mode(),
            connected=config is not None,
            topology_readable=self._topology_readable(),
        )
        if not allowed:
            return demo.refused_run(workflow_id, message, attempt, reason)
        if workflow_id.startswith("demo."):
            return demo.demo_run(workflow_id, message.strip(), attempt)
        return self._ask_control_plane(config, workflow_id, message.strip(), attempt)

    @staticmethod
    def _ask_control_plane(config, workflow_id, message, attempt):
        """One signed server-to-server call. Every outcome becomes a ``UiRunResponseV1``."""

        request_id = uuid.uuid4()
        body = gateway.request_body(
            request_id=request_id,
            workflow_id=workflow_id,
            message=message,
            attempt=attempt,
            user_id=request.env.user.id,
            company_id=request.env.company.id,
        )
        headers = gateway.signed_headers(config, body, now_unix=int(time.time()))
        try:
            response = requests.post(
                config.url,
                data=body,
                headers=headers,
                timeout=TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException:
            return demo.gateway_failure_run(request_id.hex, "control-plane-unavailable")
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if (
                not gateway.accept_control_plane_run(payload)
                or payload["run_id"] != f"run_{request_id.hex}"
            ):
                return demo.gateway_failure_run(request_id.hex, "control-plane-response-invalid")
            cited = gateway.cited_records(payload)
            if cited is None:
                return demo.gateway_failure_run(request_id.hex, "control-plane-response-invalid")
            if not AgentUiController._user_can_read(cited):
                return demo.gateway_failure_run(request_id.hex, "record-access-narrower")
            return payload
        if response.status_code in _REJECTED:
            return demo.gateway_failure_run(request_id.hex, "control-plane-rejected-request")
        return demo.gateway_failure_run(request_id.hex, "control-plane-unavailable")

    @staticmethod
    def _user_can_read(cited):
        """Whether the session user can read every cited record, in the active company only."""

        env = request.env
        scoped = env(context=dict(env.context, allowed_company_ids=[env.company.id]))
        for model, record_ids in cited.items():
            if model not in env.registry:
                return False
            records = scoped[model].browse(record_ids)
            if len(records.exists()) != len(record_ids):
                return False
            try:
                # ``read`` applies the model ACL, every record rule and field-level groups for
                # exactly the fields the citation shows. Nothing read is returned.
                records.read(list(gateway.CITABLE_FIELDS[model]))
            except (AccessError, MissingError):
                return False
        return True

    @staticmethod
    def _gateway():
        return gateway.config_from_environment(dict(os.environ))

    @staticmethod
    def _topology_readable():
        env = request.env
        return all(
            model in env.registry and env[model].browse().has_access("read")
            for model in TOPOLOGY_MODELS
        )

    @staticmethod
    def _require_ui_user():
        if not request.env.user._has_group(UI_GROUP):
            raise AccessError("odoo-agent-ui-requires-the-ui-group")

    @staticmethod
    def _demo_mode():
        neutralized = request.env["ir.config_parameter"].sudo().get_param("database.is_neutralized")
        return catalog.demo_mode_enabled(
            os.environ.get(catalog.DEMO_ENVIRONMENT_VARIABLE), neutralized
        )
