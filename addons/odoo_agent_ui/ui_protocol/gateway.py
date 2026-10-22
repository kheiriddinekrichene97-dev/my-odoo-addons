"""The Odoo side of the signed UI gateway (ADR 0018): configuration, request, signature, reply.

Free of every Odoo import (standard library only), so the control-plane suite can check this signer
against its own verifier with pinned vectors. The HTTP call itself is made by the controller.

## Where the secret lives, and where it never goes

The control-plane URL and the signing key come from the **Odoo process environment** --
``ODOO_AGENT_UI_CONTROL_PLANE_URL``, ``ODOO_AGENT_UI_SIGNING_KEY_ID``,
``ODOO_AGENT_UI_SIGNING_KEY`` -- never from the database, a system parameter, a request or the
browser. The browser only ever talks to Odoo's own ``/odoo_agent_ui/*`` routes and receives the
UI contract; it never sees the URL, the key, the headers or the signed body.

## What one request binds

``odoo_user_id`` and ``odoo_company_id`` are the authenticated session's ``env.user`` and
``env.company``, chosen by the server. They sit inside the signed body with a random
``request_id``; a fresh random nonce and the current timestamp are signed with it. So a request is
bound to one user, one company and one moment, and the control plane accepts each nonce once.

## What the end user may see (ADR 0019)

The control plane reads with its own restricted service identity, so its answer reflects what
*that identity* can see in an allowed company. Odoo record rules can be narrower than company scope
for a particular user. So before anything reaches the browser, the controller re-reads every record
the answer cites **as the user**, in the active company only, with exactly the fields shown
(:data:`CITABLE_FIELDS`). One unreadable record refuses the whole answer. The control plane cites
every record its message counts and no field names an uncited record, so the check covers the
entire answer, not a sample of it. :func:`cited_records` extracts what to check, and refuses a
citation of any other model.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

URL_VARIABLE: Final = "ODOO_AGENT_UI_CONTROL_PLANE_URL"
KEY_ID_VARIABLE: Final = "ODOO_AGENT_UI_SIGNING_KEY_ID"
KEY_VARIABLE: Final = "ODOO_AGENT_UI_SIGNING_KEY"
RUN_PATH: Final = "/ui/runs/v1"

SIGNING_DOMAIN: Final = b"odoo-agent.ui-run.signature.v1"
KEY_ID_HEADER: Final = "x-agent-ui-key-id-v1"
TIMESTAMP_HEADER: Final = "x-agent-ui-timestamp-v1"
NONCE_HEADER: Final = "x-agent-ui-nonce-v1"
SIGNATURE_HEADER: Final = "x-agent-ui-signature-v1"

_KEY_ID: Final = re.compile(r"[a-z][a-z0-9-]{2,31}\Z")
_KEY_HEX: Final = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID: Final = re.compile(r"run_[A-Za-z0-9_-]{4,64}\Z")
_LOOPBACK_NAMES: Final = frozenset({"localhost"})

CITABLE_FIELDS: Final = {
    "stock.warehouse": ("name", "code"),
    "stock.location": ("name", "usage"),
    "stock.picking.type": ("name", "code"),
}
"""Every model a real run may cite, and the fields its citation shows. The controller re-reads
exactly these, as the end user, before forwarding an answer."""
MAX_CITED_RECORDS: Final = 100


@dataclass(frozen=True, slots=True, repr=False)
class GatewayConfig:
    url: str
    key_id: str
    key: bytes

    def __repr__(self) -> str:
        return "GatewayConfig([redacted])"


def _loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def config_from_environment(environ: dict[str, str]) -> GatewayConfig | None:
    """All three variables valid, or nothing. ``https`` is required except to a loopback host."""

    url = environ.get(URL_VARIABLE, "")
    key_id = environ.get(KEY_ID_VARIABLE, "")
    key_hex = environ.get(KEY_VARIABLE, "")
    if not (url and key_id and key_hex):
        return None
    if _KEY_ID.fullmatch(key_id) is None or _KEY_HEX.fullmatch(key_hex) is None:
        return None
    parts = urlsplit(url)
    if parts.query or parts.fragment or parts.username or parts.password or not parts.hostname:
        return None
    if parts.scheme != "https" and not (parts.scheme == "http" and _loopback(parts.hostname)):
        return None
    return GatewayConfig(url=url.rstrip("/") + RUN_PATH, key_id=key_id, key=bytes.fromhex(key_hex))


def _framed(*parts: bytes) -> bytes:
    return len(parts).to_bytes(1, "big") + b"".join(
        len(part).to_bytes(8, "big") + part for part in parts
    )


def signature_hex(key: bytes, key_id: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = _framed(
        SIGNING_DOMAIN,
        key_id.encode("ascii"),
        timestamp.encode("ascii"),
        nonce.encode("ascii"),
        body,
    )
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def request_body(
    *,
    request_id: uuid.UUID,
    workflow_id: str,
    message: str,
    attempt: int,
    user_id: int,
    company_id: int,
) -> bytes:
    """The exact bytes that are signed and sent. Compact, sorted, ASCII-escaped JSON."""

    return json.dumps(
        {
            "schema_version": "1",
            "request_id": str(request_id),
            "workflow_id": workflow_id,
            "message": message,
            "attempt": attempt,
            "odoo_user_id": user_id,
            "odoo_company_id": company_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def signed_headers(
    config: GatewayConfig, body: bytes, *, now_unix: int, nonce: str | None = None
) -> dict[str, str]:
    nonce = nonce or secrets.token_hex(16)
    timestamp = str(now_unix)
    return {
        "Content-Type": "application/json",
        KEY_ID_HEADER: config.key_id,
        TIMESTAMP_HEADER: timestamp,
        NONCE_HEADER: nonce,
        SIGNATURE_HEADER: "sha256="
        + signature_hex(config.key, config.key_id, timestamp, nonce, body),
    }


def accept_control_plane_run(payload: object) -> bool:
    """Whether a ``200`` reply is shaped like a real ``UiRunResponseV1``. The browser validates the
    full contract again; this only refuses to forward something obviously not one, and refuses any
    reply that claims to be demo data -- a real workflow never produces demo data."""

    if not isinstance(payload, dict):
        return False
    events = payload.get("events")
    run_id = payload.get("run_id")
    if (
        payload.get("schema_version") != "1"
        or payload.get("contract") != "odoo_agent_ui.run"
        or payload.get("demo") is not False
        or not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
        or not isinstance(events, list)
        or not 1 <= len(events) <= 100
    ):
        return False
    for index, event in enumerate(events):
        if (
            not isinstance(event, dict)
            or event.get("sequence") != index
            or event.get("run_id") != run_id
            or event.get("demo") is not False
        ):
            return False
    return events[-1].get("type") in ("run.completed", "run.failed")


def cited_records(payload: dict[str, object]) -> dict[str, list[int]] | None:
    """The records an accepted run cites, by model; ``None`` if any citation is not checkable.

    Call only on a payload :func:`accept_control_plane_run` accepted. A citation of a model outside
    :data:`CITABLE_FIELDS`, a non-positive or non-integer id, or a repeated record makes the whole
    answer unverifiable, and the caller refuses it.
    """

    events = payload["events"]
    assert isinstance(events, list)
    found: dict[str, list[int]] = {}
    seen: set[tuple[str, int]] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "record.cited":
            continue
        model = event.get("model")
        record_id = event.get("record_id")
        if model not in CITABLE_FIELDS or type(record_id) is not int or record_id <= 0:
            return None
        if (model, record_id) in seen or len(seen) >= MAX_CITED_RECORDS:
            return None
        seen.add((model, record_id))
        found.setdefault(model, []).append(record_id)
    return found
