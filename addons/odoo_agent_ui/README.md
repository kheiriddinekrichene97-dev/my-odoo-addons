# Odoo agent UI (`odoo_agent_ui`)

The read-only **Autonomous Agent** screen: an Owl client action, its menu, and two read-only
JSON-RPC routes that speak the versioned UI contract (`odoo_agent_contracts.v1.ui_events`).

- **No real workflow is connected yet.** Every real workflow is shown disabled ("not connected yet").
- **Nothing is granted.** The menu and routes require `group_agent_ui_user`, which ships unassigned
  and grants no data access. The server re-authorizes every selected workflow.
- **No writes.** Both routes are `readonly`; the contract fixes `writes_enabled` to `false`.
- **Demo mode** (for development) is offered only when `ODOO_AGENT_UI_DEMO=1` is set in the Odoo
  process *and* `database.is_neutralized` is truthy. Both signals are operator-controlled -- not a
  cryptographic or deployment attestation of a non-production environment -- so **operators must
  not set both on production**. Demo data is labelled everywhere.

Guide, contract, tests and what remains: [`docs/UI.md`](../../docs/UI.md). Decision, including why
OpenAI ChatKit was not adopted: [ADR 0017](../../docs/decisions/0017-odoo-native-agent-ui.md).
