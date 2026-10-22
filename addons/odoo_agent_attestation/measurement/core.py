"""The pure half of the deployment measurement (ADR 0020). Imports nothing from Odoo.

The Odoo model in ``models/deployment_measurement.py`` gathers facts from the running registry and
hands them here. This module decides everything that can be decided without Odoo:

- which arguments are acceptable (:func:`validate_request`), before any work is done;
- how a source tree is fingerprinted (:func:`source_tree_digest`), byte for byte;
- the exact shape of the answer (:func:`build_measurement`), sorted and bounded, so that two
  measurements of an unchanged deployment are the same JSON document.

Nothing here hashes a *claim*. The deployment-side signer does that from this document, with the
recipes it pins. The digests computed here fingerprint **file contents**, which are the running
bytes themselves, not identifiers.

Every refusal is a :class:`MeasurementRefusedError` with one closed code and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "2"
"""``2`` adds ``data_modules`` (ADR 0021). A version-1 document cannot describe a database with an
installed Studio or imported data module, so the signer refuses version 1 outright."""

SOURCE_TREE_SCHEMA = "odoo-agent.source-tree.v1"
DATA_MODULE_RECORDS_SCHEMA = "odoo-agent.data-module-records.v2"
"""``v2``: every existing record is measured by a named basis, ``write_date`` (microseconds) or
``content``; ``v1`` truncated timestamps to the second and accepted "no timestamp" as evidence."""
STUDIO_MODULE = "studio_customization"
"""The one name Odoo 19 itself hard-codes as data-only (``ModuleGraph._imported_modules``)."""

MAX_DATA_MODULE_RECORDS = 200_000
MAX_CONTENT_ROW_BYTES = 1024 * 1024
"""A content-measured row larger than this refuses (``data-module-too-large``) rather than being
hashed partially or skipped."""

MAX_CONTENT_BYTES_PER_MODULE = 64 * 1024 * 1024

RECORD_BASES = frozenset({"write_date", "content"})

MAX_SURFACE_FIELDS = 64
MAX_SURFACE_MODELS = 16
MAX_TREE_FILES = 50_000
MAX_FILE_BYTES = 32 * 1024 * 1024

READ_PATH_METHODS = (
    "_compute_display_name",
    "_fetch_query",
    "_read_format",
    "_search",
    "check_access",
    "fetch",
    "has_access",
    "read",
    "search",
    "search_fetch",
    "search_read",
)
"""The methods a read of a surface model passes through, measured on every surface model.

Fixed here rather than chosen by the caller: a measurement whose scope the caller picks is a
measurement that can be pointed away from the override that matters. Each field's own compute
method is added to these.
"""

SKIPPED_DIRECTORIES = frozenset({"__pycache__", ".git", "i18n", "static"})
"""Not server behaviour. ``static`` is browser assets and ``i18n`` is translations; neither is on
the path of an RPC read. Everything else in a module directory -- Python, XML data, CSV ACLs,
SQL, manifests -- is hashed."""

SKIPPED_SUFFIXES = (".pyc", ".pyo", ".po", ".pot")

_FIELD = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+\.[a-z_][a-z0-9_]*$")


class MeasurementRefusedError(Exception):
    """The measurement cannot be made. One closed code; nothing else."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MeasurementRequest:
    service_uid: int
    surface_fields: tuple[tuple[str, str], ...]

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(sorted({model for model, _ in self.surface_fields}))


def validate_request(service_uid: object, surface_fields: object) -> MeasurementRequest:
    """Refuse a malformed request before any work is done on its behalf."""

    if type(service_uid) is not int or service_uid <= 0:
        raise MeasurementRefusedError("service-uid-invalid")
    if not isinstance(surface_fields, list) or not surface_fields:
        raise MeasurementRefusedError("surface-invalid")
    if len(surface_fields) > MAX_SURFACE_FIELDS:
        raise MeasurementRefusedError("surface-too-large")
    if any(type(item) is not str or not _FIELD.fullmatch(item) for item in surface_fields):
        raise MeasurementRefusedError("surface-invalid")
    if surface_fields != sorted(set(surface_fields)):
        raise MeasurementRefusedError("surface-invalid")
    pairs = tuple(tuple(item.rsplit(".", 1)) for item in surface_fields)
    if len({model for model, _ in pairs}) > MAX_SURFACE_MODELS:
        raise MeasurementRefusedError("surface-too-large")
    return MeasurementRequest(
        service_uid=service_uid,
        surface_fields=tuple((model, name) for model, name in pairs),
    )


# --- installed modules: executable or data-only ---------------------------------------------------


@dataclass(frozen=True)
class InstalledModule:
    """What the database and the disk say about one installed module, gathered by the Odoo model.

    ``imported`` is the ``ir.module.module.imported`` column, or ``None`` when the column does not
    exist (``base_import_module`` not installed). ``installable_on_disk`` is exactly the test Odoo's
    ``ModuleGraph.extend`` applies: an installable manifest found on an addons path.
    """

    version: str
    imported: bool | None
    installable_on_disk: bool
    depends: tuple[str, ...]


@dataclass(frozen=True)
class DataOnlyModule:
    name: str
    version: str
    kind: str  # "imported" | "studio"
    depends: tuple[str, ...]


def classify_modules(
    installed: Mapping[str, InstalledModule],
    load_order: Sequence[str],
    imported_by_odoo: Iterable[str],
) -> tuple[list[tuple[str, str]], list[DataOnlyModule]]:
    """Split the installed modules into executable (in Odoo's load order) and data-only ones.

    Odoo 19's ``ModuleGraph.extend`` removes every module without an installable manifest, and
    does so silently for the modules in its ``_imported_modules`` (``studio_customization`` and
    every ``imported`` module): those carry only data, loaded once at import. ``_remove`` then also
    removes every module depending on a removed one. So an installed module may be missing from
    the load graph for a legitimate reason or for drift, and the two are told apart here, on
    evidence beyond the name:

    - **data-only**: in Odoo's own ``_imported_modules``, **and** no installable manifest on disk
      (the reason Odoo removed it), **and** either flagged ``imported`` or the Studio module,
      **and** every recorded dependency installed and itself loaded or data-only, **and** no loaded
      module depending on it;
    - **refused** otherwise: a missing module outside ``_imported_modules`` or with an installable
      manifest (``module-graph-inconsistent``, ``module-classification-ambiguous``), a loaded module
      flagged ``imported`` (``module-classification-ambiguous``), or a broken dependency
      (``data-module-dependency-inconsistent``).
    """

    loaded = list(load_order)
    if len(set(loaded)) != len(loaded) or not set(loaded) <= set(installed):
        raise MeasurementRefusedError("module-graph-inconsistent")
    odoo_data_only = frozenset(imported_by_odoo)
    missing = sorted(set(installed) - set(loaded))

    for name in loaded:
        if installed[name].imported:
            # Odoo never loads an imported module; one in the graph has code on disk under an
            # imported module's name. Which one runs is not reviewable.
            raise MeasurementRefusedError("module-classification-ambiguous")

    data_only: list[DataOnlyModule] = []
    for name in missing:
        module = installed[name]
        if name not in odoo_data_only:
            raise MeasurementRefusedError("module-graph-inconsistent")
        if module.installable_on_disk:
            # Odoo only drops an installable module for a missing dependency: executable code that
            # is installed and not running.
            raise MeasurementRefusedError("module-classification-ambiguous")
        if module.imported:
            kind = "imported"
        elif name == STUDIO_MODULE:
            kind = "studio"
        else:
            raise MeasurementRefusedError("module-classification-ambiguous")
        data_only.append(DataOnlyModule(name, module.version, kind, tuple(sorted(module.depends))))

    data_names = {module.name for module in data_only}
    for module in data_only:
        for dependency in module.depends:
            if dependency not in installed or (
                dependency not in loaded and dependency not in data_names
            ):
                raise MeasurementRefusedError("data-module-dependency-inconsistent")
    for name in loaded:
        if data_names & set(installed[name].depends):
            raise MeasurementRefusedError("data-module-dependency-inconsistent")

    return [(name, installed[name].version) for name in loaded], data_only


def data_module_digest(records: Iterable[Sequence[object]]) -> tuple[int, str]:
    """Count and SHA-256 of a data-only module's records, each
    ``[model, xmlid name, res_id, noupdate, exists, basis, value]``.

    ``basis`` names what stands for the record's state, and is never left implicit:

    - ``write_date``: the record's ``write_date`` as ISO 8601 with **microseconds**, as PostgreSQL
      stores it. The ORM sets it to the transaction timestamp on every write it makes.
    - ``content``: SHA-256 of the stored row itself (``to_jsonb``), for a model without write
      tracking (``_log_access = False``) or a tracked row whose ``write_date`` is empty.

    A missing target is ``exists: false`` with basis and value ``null``. Anything else -- an
    existing record with no basis, an unknown basis, a value that is not a string -- refuses: "no
    timestamp" is never taken as evidence that nothing changed.
    """

    rows = []
    for row in records:
        model, name, res_id, noupdate, exists, basis, value = row
        if bool(exists):
            if basis not in RECORD_BASES or not isinstance(value, str) or not value:
                raise MeasurementRefusedError("data-module-record-invalid")
        elif basis is not None or value is not None:
            raise MeasurementRefusedError("data-module-record-invalid")
        rows.append(
            [str(model), str(name), int(res_id), bool(noupdate), bool(exists), basis, value]
        )
    rows.sort(key=lambda row: (row[0], row[1]))
    if len(rows) > MAX_DATA_MODULE_RECORDS:
        raise MeasurementRefusedError("data-module-too-large")
    document = {"schema": DATA_MODULE_RECORDS_SCHEMA, "records": rows}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(rows), hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def timestamp_value(value: object) -> str:
    """A ``write_date`` in its full stored precision: ``YYYY-MM-DDTHH:MM:SS.ffffff``."""

    isoformat = getattr(value, "isoformat", None)
    if isoformat is None or getattr(value, "tzinfo", None) is not None:
        raise MeasurementRefusedError("data-module-record-invalid")
    return str(isoformat(timespec="microseconds"))


def content_value(row_json: str) -> str:
    """SHA-256 of one stored row's canonical ``to_jsonb`` text, bounded in size."""

    encoded = row_json.encode("utf-8")
    if len(encoded) > MAX_CONTENT_ROW_BYTES:
        raise MeasurementRefusedError("data-module-too-large")
    return hashlib.sha256(encoded).hexdigest()


# --- source trees -------------------------------------------------------------------------------


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_tree_entries(root: Path, *, exclude_top: Iterable[str] = ()) -> list[list[str]]:
    """Every file under ``root`` as ``[kind, relative posix path, value]``, sorted by path.

    ``kind`` is ``f`` (value: SHA-256 of the bytes) or ``l`` (value: the link target as written,
    never followed). Directories in :data:`SKIPPED_DIRECTORIES` and files with a skipped suffix are
    left out; so is any top-level directory named in ``exclude_top`` (the core tree excludes its own
    ``addons`` directory, which is measured module by module). Anything else that is not a regular
    file or a link -- a socket, a device -- refuses the measurement.
    """

    excluded = frozenset(exclude_top)
    entries: list[list[str]] = []
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        here = Path(directory)
        relative_dir = here.relative_to(root)
        keep = []
        for name in subdirectories:
            if name in SKIPPED_DIRECTORIES:
                continue
            if relative_dir == Path(".") and name in excluded:
                continue
            if (here / name).is_symlink():
                files.append(name)
                continue
            keep.append(name)
        subdirectories[:] = keep
        for name in files:
            if name.endswith(SKIPPED_SUFFIXES):
                continue
            path = here / name
            relative = (relative_dir / name).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                entries.append(["l", relative, os.readlink(path)])
            elif stat.S_ISREG(mode):
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise MeasurementRefusedError("source-file-too-large")
                entries.append(["f", relative, _file_digest(path)])
            else:
                raise MeasurementRefusedError("source-entry-unsupported")
            if len(entries) > MAX_TREE_FILES:
                raise MeasurementRefusedError("source-tree-too-large")
    entries.sort(key=lambda entry: entry[1])
    return entries


def source_tree_digest(root: Path, *, exclude_top: Iterable[str] = ()) -> str:
    """SHA-256 over the canonical JSON of :func:`source_tree_entries`, under a schema label."""

    if not root.is_dir():
        raise MeasurementRefusedError("source-missing")
    document = {
        "schema": SOURCE_TREE_SCHEMA,
        "entries": source_tree_entries(root, exclude_top=exclude_top),
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# --- the answer ---------------------------------------------------------------------------------


def _sorted_strings(values: Iterable[str]) -> list[str]:
    result = sorted(set(values))
    return result


def build_measurement(
    *,
    database: str,
    series: str,
    version: str,
    modules: Sequence[tuple[str, str]],
    core_tree: str,
    module_trees: Mapping[str, tuple[str, str]],
    manual_models: Iterable[str],
    manual_fields: Iterable[str],
    customizations: Iterable[str],
    fields: Iterable[Mapping[str, object]],
    methods: Mapping[str, Sequence[str]],
    acl: Iterable[Mapping[str, object]],
    rules: Iterable[Mapping[str, object]],
    service: Mapping[str, object],
    data_modules: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """The one JSON document the measurement returns. Sorted everywhere order is not the value.

    ``modules`` is in Odoo's load order, which *is* the value: it decides which override wins. Every
    other collection is sorted, so an unchanged deployment always measures to the same bytes.
    """

    names = [name for name, _ in modules]
    if len(set(names)) != len(names) or set(names) != set(module_trees):
        raise MeasurementRefusedError("module-set-inconsistent")
    data_names = [str(module["name"]) for module in data_modules]
    if len(set(data_names)) != len(data_names) or set(data_names) & set(names):
        raise MeasurementRefusedError("module-set-inconsistent")
    return {
        "schema_version": SCHEMA_VERSION,
        "database": database,
        "odoo": {"series": series, "version": version},
        "modules": [{"name": name, "version": version} for name, version in modules],
        "data_modules": sorted((dict(module) for module in data_modules), key=lambda m: m["name"]),
        "source": {
            "core": core_tree,
            "modules": [
                {"name": name, "root": module_trees[name][0], "tree": module_trees[name][1]}
                for name in sorted(module_trees)
            ],
        },
        "studio": {
            "manual_models": _sorted_strings(manual_models),
            "manual_fields": _sorted_strings(manual_fields),
            "customizations": _sorted_strings(customizations),
        },
        "fields": sorted((dict(item) for item in fields), key=lambda item: str(item["field"])),
        "methods": [
            {"method": method, "owners": list(methods[method])} for method in sorted(methods)
        ],
        "access": {
            "acl": sorted((dict(item) for item in acl), key=_access_key),
            "rules": sorted((dict(item) for item in rules), key=_access_key),
        },
        "service": dict(service),
    }


def _access_key(item: Mapping[str, object]) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def method_owners(cls: type, name: str) -> list[str]:
    """Every class in ``cls``'s MRO that defines ``name`` itself, as its Odoo module, in MRO order.

    The first entry is the override that runs; the rest are what it may call through ``super()``.
    A class that is an Odoo model carries ``_module`` (the addon); anything else -- ``BaseModel``,
    a mixin outside an addon -- is named by its Python module.
    """

    owners: list[str] = []
    for klass in cls.__mro__:
        if name in vars(klass):
            module = getattr(klass, "_module", None) if "_module" in vars(klass) else None
            owners.append(str(module) if module else f"python:{klass.__module__}")
    return owners
