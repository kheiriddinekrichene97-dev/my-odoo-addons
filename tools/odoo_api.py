"""Minimal client for Odoo's JSON-2 API, configured from the repository's .env.

.env keys: ADRESS (or ADDRESS) = Odoo URL, API_KEY = API key of the user to act as.
Everything runs with that user's access rights.

From Python:
    from odoo_api import OdooAPI
    api = OdooAPI()
    api.call("res.partner", "search_read", domain=[["is_company", "=", True]], fields=["name"], limit=5)

From the command line (keyword arguments as JSON):
    python tools/odoo_api.py res.users search_read '{"domain": [], "fields": ["login"], "limit": 3}'
"""
import json
import re
import sys
from pathlib import Path

import requests

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env(path=ENV_FILE):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


class OdooAPIError(Exception):
    pass


class OdooAPI:
    def __init__(self, url=None, api_key=None, database=None, timeout=120):
        env = load_env()
        url = url or env.get("ADDRESS") or env.get("ADRESS")
        url = url if url.startswith("http") else "https://" + url
        self.url = re.sub(r"/(odoo|web)(/.*)?$", "", url.rstrip("/"))
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"bearer {api_key or env['API_KEY']}",
            "Content-Type": "application/json",
        })
        if database or env.get("ODOO_DB"):
            self.session.headers["X-Odoo-Database"] = database or env["ODOO_DB"]
        self.timeout = timeout

    def call(self, model, method, ids=None, context=None, **kwargs):
        """Call `method` on `model`; `ids` for record methods, other arguments by keyword."""
        payload = dict(kwargs)
        if ids is not None:
            payload["ids"] = ids
        if context:
            payload["context"] = context
        response = self.session.post(f"{self.url}/json/2/{model}/{method}", json=payload, timeout=self.timeout)
        if response.status_code != 200:
            try:
                message = response.json().get("message")
            except ValueError:
                message = response.text[:500]
            raise OdooAPIError(f"{model}.{method}: HTTP {response.status_code}: {message}")
        return response.json()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    kwargs = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    print(json.dumps(OdooAPI().call(sys.argv[1], sys.argv[2], **kwargs), indent=2, ensure_ascii=False, default=str))
