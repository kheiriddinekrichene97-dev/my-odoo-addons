"""Measure the running deployment for the deployment-side attestation signer (ADR 0020).

One RPC-callable method, ``agent_measure_deployment_v1``. It reads; it cannot write
(``@api.readonly`` gives it a readonly cursor on an RPC call). It returns facts, not claims: the
signer turns them into claim digests with the recipes it pins, compares each against the
operator-reviewed profile, and refuses to sign if any differs.

## Why in-process

Two of the facts can only be measured from inside the Odoo process:

- **effective field attributes** -- what the registry actually built for each field (type, store,
  compute, related, groups, company dependence), after every installed module has had its say.
  Declarations on disk are not that; ADR 0009 exists because they differ.
- **resolved method owners** -- which addon's override of ``search_read`` (and the rest of the read
  path) runs, walked from the model class's real MRO.

Everything else here could be read over RPC by a sufficiently privileged user. Measuring it in the
same call, on the same cursor, makes the answer one consistent snapshot.

## Who may call it

Only a holder of :data:`MEASUREMENT_GROUP`, and only one who is **not** the service identity being
measured, not in superuser mode, and not a Settings or Access Rights administrator. The group is
unassigned on install. Odoo's RPC dispatch runs no access check before calling a public method
(``odoo/service/model.py:45``), so the check is here, before the arguments are even parsed.

## What it reads, and how

Technical models (``ir.module.module``, ``ir.model.access``, ``ir.rule``, ``ir.model.data``,
``ir.model.fields``) are read with ``sudo()`` **after** authorization, because a measurement that
depends on the measuring user's own visibility would measure that user rather than the deployment.
The service identity's access is evaluated **as that identity** (``env(user=...)``), without
superuser mode.

Source trees are hashed once per Odoo process, on first request, and remembered: a module's Python
is imported once, so bytes changed on disk after that are not what this process runs either. A
restart re-reads them.

## Executable and data-only modules (ADR 0021)

Odoo 19's own loader leaves ``studio_customization`` and every ``imported`` module out of its load
graph: they carry data only, loaded once when imported, and have no source on disk. They are
installed and their records are live, so they are measured, as ``data_modules``: kind, version,
dependencies, and a digest of the records they own. The split is made by
:func:`~..measurement.core.classify_modules` from Odoo's own classification plus on-disk and
dependency evidence; any installed module missing from the graph for another reason still refuses.
"""

import threading
from collections import defaultdict
from pathlib import Path

from odoo import api, models, release  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]
from odoo.modules.module import Manifest, get_module_path  # type: ignore[import-not-found]
from odoo.modules.module_graph import ModuleGraph  # type: ignore[import-not-found]
from odoo.tools import SQL  # type: ignore[import-not-found]

from ..measurement.core import (
    MAX_CONTENT_BYTES_PER_MODULE,
    READ_PATH_METHODS,
    InstalledModule,
    MeasurementRefusedError,
    build_measurement,
    classify_modules,
    content_value,
    data_module_digest,
    is_within,
    method_owners,
    source_tree_digest,
    timestamp_value,
    validate_request,
)

MEASUREMENT_GROUP = "odoo_agent_attestation.group_deployment_measurement"
"""The only identity permitted to measure. Held by the signer's measurement user and nobody else."""

ADMINISTRATOR_GROUPS = ("base.group_system", "base.group_erp_manager")
"""A measurement identity must not be able to change what it measures."""

OPERATIONS = ("read", "write", "create", "unlink")

_SOURCE_LOCK = threading.Lock()
_SOURCE_CACHE: dict[tuple[tuple[str, str], ...], tuple[str, dict[str, tuple[str, str]]]] = {}


def _refuse(code):
    raise UserError(f"odoo-agent-measurement-{code}")


class DeploymentMeasurement(models.AbstractModel):
    """Hosts the measurement on its own abstract model; patches no standard model."""

    _name = "odoo.agent.deployment.measurement"
    _description = "Deployment measurement for the attestation signer"

    @api.model
    @api.readonly
    def agent_measure_deployment_v1(self, service_uid, surface_fields):
        """Measure this deployment for ``surface_fields`` and the service user ``service_uid``.

        ``surface_fields`` is a sorted, unique list of ``model.field`` names -- the reviewed read
        surface. ``service_uid`` is the id the service credential itself reported
        (``res.users.context_get``). Nothing else is accepted.
        """

        if self.env.su:
            _refuse("refuses-superuser-mode")
        user = self.env.user
        if not user._has_group(MEASUREMENT_GROUP):
            raise AccessError("odoo-agent-measurement-requires-the-measurement-group")
        if any(user._has_group(group) for group in ADMINISTRATOR_GROUPS):
            raise AccessError("odoo-agent-measurement-refuses-an-administrator")
        try:
            request = validate_request(service_uid, surface_fields)
        except MeasurementRefusedError as error:
            _refuse(error.code)
        if request.service_uid == user.id:
            _refuse("measurer-is-the-service-identity")

        system = self.sudo()
        service = (
            system.env["res.users"].with_context(active_test=False).browse(request.service_uid)
        )
        if not service.exists():
            _refuse("service-user-unknown")
        for model, name in request.surface_fields:
            if model not in system.env or name not in system.env[model]._fields:
                _refuse("surface-field-unknown")

        try:
            modules, data_only = self._measure_modules(system)
            core_tree, module_trees = self._measure_sources(modules)
            data_modules = self._measure_data_modules(system, data_only)
            document = build_measurement(
                database=self.env.cr.dbname,
                series=release.serie,
                version=release.version,
                modules=modules,
                core_tree=core_tree,
                module_trees=module_trees,
                manual_models=system.env["ir.model"]
                .search([("state", "=", "manual")])
                .mapped("model"),
                manual_fields=[
                    f"{field.model}.{field.name}:{field.ttype}"
                    for field in system.env["ir.model.fields"].search([("state", "=", "manual")])
                ],
                customizations=[
                    f"{data.model}:{data.name}"
                    for data in system.env["ir.model.data"].search(
                        [("module", "=", "studio_customization")]
                    )
                ],
                fields=self._measure_fields(system, request.surface_fields),
                methods=self._measure_methods(system, request),
                acl=self._measure_acl(system, request.models),
                rules=self._measure_rules(system, request.models),
                service=self._measure_service(system, service, request.models),
                data_modules=data_modules,
            )
        except MeasurementRefusedError as error:
            _refuse(error.code)
        return document

    # -- modules and source ---------------------------------------------------------------------

    def _measure_modules(self, system):
        """Executable modules in Odoo's own load order, and the installed data-only modules."""

        registry = system.env["ir.module.module"]
        has_imported = "imported" in registry._fields
        installed = {}
        for module in registry.search([("state", "=", "installed")]):
            # Exactly the test ModuleGraph.extend applies before removing a module.
            manifest = Manifest.for_addon(module.name, display_warning=False) or {}
            installed[module.name] = InstalledModule(
                version=module.latest_version or "",
                imported=bool(module.imported) if has_imported else None,
                installable_on_disk=bool(manifest.get("installable")),
                depends=tuple(sorted(module.dependencies_id.mapped("name"))),
            )
        graph = ModuleGraph(self.env.cr, mode="load")
        graph.extend(sorted(installed))
        # Odoo's own list of modules it drops as data-only. A loader without it is not the one
        # this classification was established against: refuse rather than guess.
        imported_by_odoo = getattr(graph, "_imported_modules", None)
        if imported_by_odoo is None:
            raise MeasurementRefusedError("module-loader-unsupported")
        return classify_modules(installed, [node.name for node in graph], imported_by_odoo)

    def _measure_data_modules(self, system, data_only):
        """Each data-only module's facts, and a digest of the records it owns.

        Every existing record is measured on a named basis (ADR 0021): its ``write_date`` in full
        microsecond precision when the model tracks writes and the row has one, otherwise the
        content of the stored row itself. A record without either is never measured as "unchanged".
        """

        records = system.env["ir.model.data"].with_context(active_test=False)
        measured = []
        for module in data_only:
            rows = records.search_read(
                [("module", "=", module.name)], ["model", "name", "res_id", "noupdate"]
            )
            by_model = defaultdict(set)
            for row in rows:
                by_model[row["model"]].add(row["res_id"])
            state = {}
            content_bytes = 0
            for model, ids in by_model.items():
                if model not in system.env or system.env[model]._abstract:
                    continue  # nothing to point at: measured as "does not exist"
                target = system.env[model].with_context(active_test=False)
                tracked = bool(target._log_access) and "write_date" in target._fields
                untracked = []
                for record in target.browse(sorted(ids)).exists():
                    written = record.write_date if tracked else None
                    if written:
                        state[(model, record.id)] = ("write_date", timestamp_value(written))
                    else:
                        untracked.append(record.id)
                if untracked:
                    # The stored row itself: every column, as PostgreSQL's canonical jsonb text.
                    self.env.cr.execute(
                        SQL(
                            "SELECT id, to_jsonb(t)::text FROM %s AS t WHERE id = ANY(%s)",
                            SQL.identifier(target._table),
                            untracked,
                        )
                    )
                    for record_id, row_json in self.env.cr.fetchall():
                        content_bytes += len(row_json.encode("utf-8"))
                        if content_bytes > MAX_CONTENT_BYTES_PER_MODULE:
                            raise MeasurementRefusedError("data-module-too-large")
                        state[(model, record_id)] = ("content", content_value(row_json))
            count, digest = data_module_digest(
                [
                    row["model"],
                    row["name"],
                    row["res_id"],
                    row["noupdate"],
                    (row["model"], row["res_id"]) in state,
                    *state.get((row["model"], row["res_id"]), (None, None)),
                ]
                for row in rows
            )
            measured.append(
                {
                    "name": module.name,
                    "version": module.version,
                    "kind": module.kind,
                    "depends": list(module.depends),
                    "records": count,
                    "data": digest,
                }
            )
        return measured

    def _measure_sources(self, modules):
        key = tuple(modules)
        with _SOURCE_LOCK:
            cached = _SOURCE_CACHE.get(key)
            if cached is None:
                # `odoo` is a namespace package in 19 (no `__file__`); `release.py` is at its root.
                core_root = Path(release.__file__).resolve().parent
                trees = {}
                for name, _version in modules:
                    path = get_module_path(name, display_warning=False)
                    if not path:
                        raise MeasurementRefusedError("module-source-missing")
                    root = Path(path)
                    trees[name] = (
                        "core" if is_within(root, core_root) else "addon",
                        source_tree_digest(root),
                    )
                cached = (source_tree_digest(core_root, exclude_top=("addons",)), trees)
                _SOURCE_CACHE.clear()
                _SOURCE_CACHE[key] = cached
        return cached

    # -- fields and methods ---------------------------------------------------------------------

    def _measure_fields(self, system, surface_fields):
        registry_fields = system.env["ir.model.fields"]
        measured = []
        for model, name in surface_fields:
            field = system.env[model]._fields[name]
            record = registry_fields.search([("model", "=", model), ("name", "=", name)], limit=1)
            compute = field.compute
            if compute is not None and not isinstance(compute, str):
                compute = f"callable:{getattr(compute, '__qualname__', 'anonymous')}"
            measured.append(
                {
                    "field": f"{model}.{name}",
                    "type": field.type,
                    "store": bool(field.store),
                    "compute": compute,
                    "related": field.related or None,
                    "company_dependent": bool(field.company_dependent),
                    "groups": field.groups or None,
                    "readonly": bool(field.readonly),
                    "required": bool(field.required),
                    "comodel": field.comodel_name or None,
                    "modules": sorted(
                        item.strip() for item in (record.modules or "").split(",") if item.strip()
                    ),
                }
            )
        return measured

    def _measure_methods(self, system, request):
        owners = {}
        computes = {}
        for model, name in request.surface_fields:
            compute = system.env[model]._fields[name].compute
            if isinstance(compute, str):
                computes.setdefault(model, set()).add(compute)
        for model in request.models:
            cls = type(system.env[model])
            for method in sorted(set(READ_PATH_METHODS) | computes.get(model, set())):
                owners[f"{model}.{method}"] = method_owners(cls, method)
        return owners

    # -- access -----------------------------------------------------------------------------------

    def _xmlids(self, records):
        """The first (oldest) external id of each record, or ``None``."""

        found = records._get_external_ids()
        return {record.id: (sorted(found[record.id])[:1] or [None])[0] for record in records}

    def _measure_acl(self, system, model_names):
        acls = (
            system.env["ir.model.access"]
            .with_context(active_test=False)
            .search([("model_id.model", "in", list(model_names))])
        )
        groups = self._xmlids(acls.mapped("group_id"))
        return [
            {
                "model": acl.model_id.model,
                # `None` is a global ACL; "?" a group with no external id (reviewable only by eye).
                "group": (groups.get(acl.group_id.id) or "?") if acl.group_id else None,
                "active": bool(acl.active),
                "read": bool(acl.perm_read),
                "write": bool(acl.perm_write),
                "create": bool(acl.perm_create),
                "unlink": bool(acl.perm_unlink),
            }
            for acl in acls
        ]

    def _measure_rules(self, system, model_names):
        rules = (
            system.env["ir.rule"]
            .with_context(active_test=False)
            .search([("model_id.model", "in", list(model_names))])
        )
        groups = self._xmlids(rules.mapped("groups"))
        return [
            {
                "model": rule.model_id.model,
                "groups": sorted(groups.get(group.id) or "?" for group in rule.groups),
                "global": bool(rule["global"]),
                "domain": rule.domain_force or "",
                "active": bool(rule.active),
                "read": bool(rule.perm_read),
                "write": bool(rule.perm_write),
                "create": bool(rule.perm_create),
                "unlink": bool(rule.perm_unlink),
            }
            for rule in rules
        ]

    def _measure_service(self, system, service, model_names):
        """The service identity as Odoo resolves it: effective groups, companies, model access."""

        groups = service.all_group_ids
        names = self._xmlids(groups)
        as_service = self.env(user=service.id, su=False)
        return {
            "uid": service.id,
            "login": service.login,
            "active": bool(service.active),
            "share": bool(service.share),
            "groups": sorted(name for name in names.values() if name),
            "unnamed_groups": sum(1 for name in names.values() if not name),
            "companies": sorted(service.company_ids.ids),
            "access": [
                {
                    "model": model,
                    **{
                        operation: bool(as_service[model].has_access(operation))
                        for operation in OPERATIONS
                    },
                }
                for model in model_names
            ],
        }
