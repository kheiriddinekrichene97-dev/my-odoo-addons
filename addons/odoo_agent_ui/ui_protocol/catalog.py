"""What the signed-in user may select, and whether demo data may exist at all.

Free of every Odoo import, so the contract suite can check it against ``odoo_agent_contracts``.

## The selector reflects backend authority, and grants none

The bootstrap is computed on the server for every request; the browser only renders it. When the
browser sends a workflow back, :func:`authorize` re-derives the same list and looks the id up
again, so an edited request, a stale page or a hand-written RPC call gains nothing by naming a
workflow. Without the signed control-plane link every real entry says so
(``control-plane-not-connected``) rather than being hidden; with it, only the topology workflow can
be available, and only to a user who can read Odoo's inventory structure.

## Demo data: exactly what the gate checks, and what it does not prove

Demo workflows are offered only when **both** hold:

* the process environment variable ``ODOO_AGENT_UI_DEMO`` is ``1``; and
* the system parameter ``database.is_neutralized`` is truthy (``True``, ``true`` or ``1``) -- the
  flag Odoo's own ``neutralize`` sets on a copied database.

Either alone is not enough. Outside demo mode the bootstrap carries no demo workflow and
:func:`authorize` refuses a demo id exactly as it refuses an unknown one.

Both signals are **operator-controlled**: whoever runs the Odoo process sets the variable, and
anyone with system-administrator rights or database access can set the parameter. They are not a
cryptographic or deployment attestation that the environment is not production. Operators must not
set both on a production database.
"""

from __future__ import annotations

from typing import Final

SCHEMA_VERSION: Final = "1"
DEMO_ENVIRONMENT_VARIABLE: Final = "ODOO_AGENT_UI_DEMO"

TOPOLOGY_WORKFLOW: Final = "inventory.topology"
"""The one real workflow (ADR 0018). It is offered only when the signed control-plane link is
configured **and** the user can read Odoo's warehouse, location and operation-type models; even
then the control plane applies its own gates on every run and may refuse."""

REAL_WORKFLOWS: Final = (
    ("general", "General read-only questions", "General"),
    (TOPOLOGY_WORKFLOW, "Warehouses and locations", "Inventory"),
    ("inventory.exceptions", "Inventory exceptions", "Inventory"),
    ("inventory.stock", "Stock and products", "Inventory"),
    ("inventory.transfers", "Transfers and deliveries", "Inventory"),
    ("purchasing.replenishment", "Purchasing replenishment", "Purchasing"),
    ("sales.follow_up", "Sales follow-up", "Sales"),
    ("finance.receivables", "Receivables follow-up", "Finance"),
    ("operations.daily_brief", "Daily operational brief", "Operations"),
)

DEMO_WORKFLOWS: Final = (("demo.inventory_exceptions", "Demo: inventory exceptions", "Demo data"),)


def demo_mode_enabled(environment_flag: object, neutralized: object) -> bool:
    """Both conditions, exactly. ``neutralized`` is the raw ``ir.config_parameter`` value."""

    return environment_flag == "1" and neutralized in ("True", "true", "1", True)


def _real_reason(workflow_id: str, *, connected: bool, topology_readable: bool) -> str | None:
    """``None`` when available, else why not. Only the topology workflow can ever be available."""

    if not connected:
        return "control-plane-not-connected"
    if workflow_id != TOPOLOGY_WORKFLOW:
        return "workflow-not-enabled"
    if not topology_readable:
        return "odoo-access-missing"
    return None


def bootstrap(
    *, demo_mode: bool, connected: bool = False, topology_readable: bool = False
) -> dict[str, object]:
    """The ``UiBootstrapV1`` payload for this request.

    ``connected`` means the signed control-plane link is **configured** in this Odoo process; it
    does not claim the control plane is reachable or will serve the run, which it decides itself.
    """

    workflows: list[dict[str, object]] = []
    for workflow_id, label, group in REAL_WORKFLOWS:
        reason = _real_reason(workflow_id, connected=connected, topology_readable=topology_readable)
        workflows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "id": workflow_id,
                "label": label,
                "group": group,
                "availability": "unavailable" if reason else "available",
                "reason": reason,
                "demo": False,
            }
        )
    if demo_mode:
        workflows += [
            {
                "schema_version": SCHEMA_VERSION,
                "id": workflow_id,
                "label": label,
                "group": group,
                "availability": "available",
                "reason": None,
                "demo": True,
            }
            for workflow_id, label, group in DEMO_WORKFLOWS
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "odoo_agent_ui.bootstrap",
        "demo_mode": demo_mode,
        "writes_enabled": False,
        "control_plane": "connected" if connected else "not-connected",
        "workflows": workflows,
    }


def authorize(
    workflow_id: str,
    *,
    demo_mode: bool,
    connected: bool = False,
    topology_readable: bool = False,
) -> tuple[bool, str]:
    """Re-derive the bootstrap and look ``workflow_id`` up in it.

    Returns ``(True, "")`` for an available workflow, else ``(False, reason)``. An unknown id and a
    demo id outside demo mode get the same answer, so the refusal does not reveal that demo exists.
    """

    payload = bootstrap(
        demo_mode=demo_mode, connected=connected, topology_readable=topology_readable
    )
    for workflow in payload["workflows"]:  # type: ignore[union-attr]
        if workflow["id"] == workflow_id:
            if workflow["availability"] == "available":
                return True, ""
            return False, str(workflow["reason"])
    return False, "workflow-not-authorized"
