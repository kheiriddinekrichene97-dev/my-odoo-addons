"""The deterministic demo run, and the refusal for a workflow that cannot run.

Free of every Odoo import. Everything here produces ``UiRunResponseV1`` payloads; the contract
suite validates each one against ``odoo_agent_contracts``.

The demo is **deterministic**: the same workflow, message and attempt always produce the same run
id and the same events. It exercises every state the UI must render, chosen by keywords in the
message:

* ``permission``            -- the caller may not read what was asked (``permission``);
* ``policy`` or ``blocked`` -- the capability is not publishable yet (``policy_blocked``);
* ``offline`` or ``timeout``-- a transient failure on the first attempt, then an answer on retry
  (``temporary``);
* anything else             -- an answer with workflow steps and cited records.

Every event carries ``demo: true``; every cited record is named ``DEMO ...`` and uses an id from a
reserved range that no real record is claimed to have. Demo citations are never linked in the UI.
"""

from __future__ import annotations

import hashlib
from typing import Final

SCHEMA_VERSION: Final = "1"
MAX_MESSAGE_LENGTH: Final = 2000
MAX_ATTEMPTS: Final = 3
DEMO_RECORD_ID_BASE: Final = 990_000_000
"""Demo citations use ids from here up. They are labels in a fixture, not references to Odoo."""


def run_id_for(workflow_id: str, message: str, attempt: int, *, demo: bool) -> str:
    """Deterministic. The ``demo_`` prefix follows the run's ``demo`` flag, not the workflow name:
    a refusal of a demo id outside demo mode carries no demo data and is not labelled as demo."""

    digest = hashlib.sha256(f"{workflow_id}\n{attempt}\n{message}".encode()).hexdigest()
    return f"run_{'demo_' if demo else ''}{digest[:16]}"


class _Run:
    def __init__(self, run_id: str, demo: bool) -> None:
        self.run_id = run_id
        self.demo = demo
        self.events: list[dict[str, object]] = []

    def emit(self, event_type: str, **values: object) -> None:
        self.events.append(
            {
                "schema_version": SCHEMA_VERSION,
                "type": event_type,
                "run_id": self.run_id,
                "sequence": len(self.events),
                "demo": self.demo,
                **values,
            }
        )

    def step(self, step_id: str, label: str) -> None:
        self.emit("step.started", step_id=step_id, label=label)
        self.emit("step.completed", step_id=step_id, label=label)

    def fail(self, kind: str, code: str, title: str, detail: str) -> None:
        self.emit(
            "run.failed",
            failure={
                "kind": kind,
                "code": code,
                "title": title,
                "detail": detail,
                "retryable": kind == "temporary",
                "no_action_performed": True,
            },
        )

    def response(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "contract": "odoo_agent_ui.run",
            "run_id": self.run_id,
            "demo": self.demo,
            "events": self.events,
        }


def refused_run(workflow_id: str, message: str, attempt: int, reason: str) -> dict[str, object]:
    """A run that never starts: the workflow is not available to this user now."""

    run = _Run(run_id_for(workflow_id, message, attempt, demo=False), demo=False)
    if reason == "control-plane-not-connected":
        run.fail(
            "policy_blocked",
            reason,
            "This workflow isn't available yet",
            "The agent control plane is not connected to this Odoo database, so nothing can be "
            "asked through this workflow.",
        )
    elif reason == "workflow-not-enabled":
        run.fail(
            "policy_blocked",
            reason,
            "This workflow isn't enabled yet",
            "Only the warehouses-and-locations workflow is enabled; this one is not.",
        )
    elif reason == "odoo-access-missing":
        run.fail(
            "permission",
            reason,
            "Access denied",
            "Your Odoo account cannot read warehouses, locations and operation types, so the "
            "agent will not answer this for you.",
        )
    else:
        run.fail(
            "policy_blocked",
            "workflow-not-authorized",
            "This workflow isn't available to you",
            "The selected workflow is not in the list this server authorizes for your account.",
        )
    return run.response()


GATEWAY_FAILURES: Final = {
    "control-plane-unavailable": (
        "temporary",
        "The agent is unreachable",
        "Odoo could not reach the agent control plane in time. Nothing was changed.",
    ),
    "control-plane-response-invalid": (
        "temporary",
        "Unexpected answer from the agent",
        "The control plane answered in a form Odoo does not accept, so nothing is shown.",
    ),
    "record-access-narrower": (
        "permission",
        "Not visible with your access",
        "This answer includes Odoo records your own access does not show you, so it is not "
        "shown. Nothing was changed.",
    ),
    "control-plane-rejected-request": (
        "policy_blocked",
        "The agent refused this server",
        "The control plane did not accept this Odoo server's signed request. An administrator "
        "must check the agent link configuration.",
    ),
}
"""Failures of the signed link itself, decided by Odoo from the transport outcome. None carries a
control-plane message, URL, status body or header."""


def gateway_failure_run(request_id_hex: str, code: str) -> dict[str, object]:
    """A real run that failed at the transport, under the run id the control plane would use."""

    kind, title, detail = GATEWAY_FAILURES[code]
    run = _Run(f"run_{request_id_hex}", demo=False)
    run.fail(kind, code, title, detail)
    return run.response()


def demo_run(workflow_id: str, message: str, attempt: int) -> dict[str, object]:
    """The deterministic demo interaction. Callers must have authorized ``workflow_id`` first."""

    assert workflow_id.startswith("demo.")
    run = _Run(run_id_for(workflow_id, message, attempt, demo=True), demo=True)
    text = message.lower()
    run.emit("run.accepted", workflow_id=workflow_id)

    if "permission" in text:
        run.emit("step.started", step_id="read_stock", label="Reading stock levels (demo)")
        run.fail(
            "permission",
            "record-access-denied",
            "Access denied",
            "Your Odoo account cannot read the stock locations this question needs. Ask an "
            "administrator for access. (Demo data.)",
        )
        return run.response()

    if "policy" in text or "blocked" in text:
        run.fail(
            "policy_blocked",
            "capability-not-publishable",
            "This operation isn't available yet",
            "Reading transfers and stock moves is blocked until its provenance review is "
            "complete. (Demo data.)",
        )
        return run.response()

    if ("offline" in text or "timeout" in text) and attempt == 1:
        run.step("resolve_scope", "Resolving your company and warehouses (demo)")
        run.emit("step.started", step_id="read_stock", label="Reading stock levels (demo)")
        run.fail(
            "temporary",
            "odoo-unreachable",
            "Odoo didn't respond",
            "The request to Odoo timed out before any data was returned. Retrying is safe: "
            "nothing was changed. (Demo data.)",
        )
        return run.response()

    run.step("resolve_scope", "Resolving your company and warehouses (demo)")
    run.step("read_stock", "Reading stock levels (demo)")
    run.step("evaluate", "Evaluating inventory rules (demo)")
    run.emit(
        "message.completed",
        text=(
            "Two demo products need attention. DEMO Widget A is below its minimum at DEMO "
            "Shelf 1, and DEMO Widget B has no stock at DEMO Shelf 2 with an open demand. "
            "These are demonstration records, not data from your Odoo database."
        ),
    )
    for offset, (name, location, on_hand, minimum) in enumerate(
        (
            ("DEMO Widget A", "DEMO Shelf 1", "3 Units", "10 Units"),
            ("DEMO Widget B", "DEMO Shelf 2", "0 Units", "4 Units"),
        ),
        start=1,
    ):
        run.emit(
            "record.cited",
            model="product.product",
            record_id=DEMO_RECORD_ID_BASE + offset,
            display_name=name,
            fields=[
                {"label": "Location", "value": location},
                {"label": "On hand", "value": on_hand},
                {"label": "Minimum", "value": minimum},
            ],
        )
    run.emit("run.completed")
    return run.response()
