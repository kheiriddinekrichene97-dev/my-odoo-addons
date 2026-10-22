# Autonomous-agent addons (vendored)

`odoo_agent_gateway`, `odoo_agent_ui` and `odoo_agent_attestation` are copied, unmodified, from
[`dali012/odoo-autonomous-agent`](https://github.com/dali012/odoo-autonomous-agent) at commit
`45a7ae67c281f761e255a635556e2d23ee58e69c` (`addons/<module>/`). Two directories are left out
because they are not Odoo code: `unit_tests/` (pytest suites with a fake ORM) and
`odoo_agent_ui/static/tests/` (Node test fixtures). All three are LGPL-3.

Edit them only in the source repository, then re-copy the three directories at the new commit and
update the commit above. `odoo_agent_attestation` fingerprints installed module source for the
deployment-side attestation signer (ADR 0020 there): any local edit here is reported as drift, by
design.

## What they do, and what they grant on install

| Module | Depends on | Purpose | On install |
|---|---|---|---|
| `odoo_agent_gateway` | `stock` | Checked read-only projections for the agent control plane | Adds a group nobody holds; grants nothing |
| `odoo_agent_ui` | `web` | Read-only agent screen; inert until `ODOO_AGENT_UI_*` are set in the environment | Adds a group nobody holds; the screen shows the agent as unavailable |
| `odoo_agent_attestation` | `base` | In-process measurement for the attestation signer (ADR 0020) | Adds a group nobody holds; grants nothing |

None of them writes business data, runs a scheduled action or sends mail.

## Staging (Odoo.sh branch `data-imports`)

New modules are not installed automatically on a staging build. After the build for this commit is
up, an administrator installs the three modules from **Apps** (*Update Apps List* first). The user
setup that follows is in the source repository's `docs/OPERATIONS_ATTESTATION_SIGNER.md` §12,
step 1. Do not install these on production until that runbook says so.

### Upgrading to a new source commit

A re-copy that changes a module's manifest `version` is upgraded by the Odoo.sh build when the
branch is set to *Update previous build*. Confirm the installed version in **Apps** (debug mode); if
it did not change, press **Upgrade** on the module. The procedure for the other components is the
source repository's `docs/OPERATIONS_STAGING_DEPLOYMENT.md` §F.

- `5b857a7` (ADR 0021 there): `odoo_agent_attestation` → `19.0.1.1.0`. Studio and imported
  data-only modules are measured instead of refused.
- `45a7ae6` (PR #68 there, review finding F5): only `odoo_agent_gateway/tests/` changes. It adds
  a real-Odoo test proving that the agent's topology `search_read` with `"load": null` computes
  no many2one label. **No module version changes and nothing needs upgrading**: Odoo loads test
  modules only under `--test-enable`. The rebuild still changes the measured source tree of
  `odoo_agent_gateway`, because the attestation measures tests too, so the signer's review is
  re-run afterwards.
