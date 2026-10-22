"""The topology reads' ``"load": null`` on real Odoo 19, over real ``/json/2/`` (review finding F5).

The control plane's ``inventory_topology`` reader calls ``search_read`` on ``stock.location``,
``stock.warehouse`` and ``stock.picking.type`` and keeps only the id of each many2one. Without a
``load`` argument Odoo 19 defaults to ``_classic_read``, and ``Many2one.convert_to_read`` renders
every value as ``(id, value.sudo().display_name)``: the related record's label is computed **as
superuser** -- for a location, its parent's full name -- and sent back for nothing.

The claims under test, against Odoo itself and over the real dispatcher:

* with ``"load": null`` in the JSON body -- the exact shape the reader sends -- every many2one comes
  back as a bare id or ``False``;
* ``_compute_display_name`` of the related ``stock.location`` and ``res.company`` records is **not
  called** for those requests;
* the same request without ``load`` does call it, as superuser, for the related records, and
  answers the same ids. That control proves the spy is live, so the absence above is evidence and
  not a spy that never fires.

The service identity is an Internal User with nothing else, like the restricted staging read
identity. The spies wrap the registry's own method and call through, so nothing about the answer
changes.
"""

import json
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

LOCATION_USAGES = ["internal", "transit", "view"]
LOCATION_FIELDS = ["id", "name", "usage", "active", "company_id", "location_id"]
WAREHOUSE_FIELDS = ["id", "name", "code", "company_id", "view_location_id", "lot_stock_id"]
PICKING_TYPE_FIELDS = ["id", "name", "code", "company_id"]
LABELLED_MODELS = ("stock.location", "res.company")


@tagged("post_install", "-at_install")
class TestTopologyReadLoadDispatch(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env["stock.warehouse"].create(
            {"name": "Agent Topology Load", "code": "ATL", "company_id": cls.company.id}
        )
        cls.service_user = cls.env["res.users"].create(
            {
                "name": "Agent topology load service",
                "login": "agent.topology.load",
                "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
                "company_id": cls.company.id,
                "company_ids": [(6, 0, [cls.company.id])],
            }
        )

    # -- the request, exactly as the reader shapes it --------------------------------------------

    def search_read(self, model, domain, fields, *, load_null):
        body = {"domain": domain, "fields": fields, "offset": 0, "limit": 80, "order": "id asc"}
        if load_null:
            body["load"] = None
        self.env.flush_all()
        self.env.invalidate_all()
        key = (
            self.env["res.users.apikeys"]
            .with_user(self.service_user)
            ._generate(None, "itest-topology-load", datetime.now() + timedelta(days=1))
        )
        response = self.url_open(
            f"/json/2/{model}/search_read",
            data=json.dumps(body, separators=(",", ":")),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def location_rows(self, *, load_null):
        domain = [["company_id", "in", [self.company.id]], ["usage", "in", LOCATION_USAGES]]
        return self.search_read("stock.location", domain, LOCATION_FIELDS, load_null=load_null)

    def warehouse_rows(self, *, load_null):
        domain = [["company_id", "in", [self.company.id]]]
        return self.search_read("stock.warehouse", domain, WAREHOUSE_FIELDS, load_null=load_null)

    def picking_type_rows(self, *, load_null):
        domain = [["company_id", "in", [self.company.id]]]
        return self.search_read(
            "stock.picking.type", domain, PICKING_TYPE_FIELDS, load_null=load_null
        )

    @contextmanager
    def display_name_spies(self):
        """Record every ``_compute_display_name`` call on the labelled models, then call through."""

        calls = []
        with ExitStack() as stack:
            for model in LABELLED_MODELS:
                registry_class = type(self.env[model])
                original = registry_class._compute_display_name

                def spy(records, _original=original):
                    calls.append((records._name, frozenset(records.ids), records.env.su))
                    return _original(records)

                stack.enter_context(patch.object(registry_class, "_compute_display_name", spy))
            yield calls

    def assert_bare_ids(self, rows, relations):
        self.assertTrue(rows)
        for row in rows:
            for name in relations:
                value = row[name]
                self.assertTrue(value is False or type(value) is int, (name, value))

    # -- load null ---------------------------------------------------------------------------------

    def test_location_search_with_load_null_returns_bare_ids_and_computes_no_label(self):
        with self.display_name_spies() as calls:
            rows = self.location_rows(load_null=True)

        self.assert_bare_ids(rows, ("company_id", "location_id"))
        self.assertIn(self.warehouse.lot_stock_id.id, {row["id"] for row in rows})
        self.assertTrue(any(row["location_id"] for row in rows))  # parents are really present
        self.assertEqual(calls, [])

    def test_warehouse_search_with_load_null_computes_no_location_label(self):
        with self.display_name_spies() as calls:
            rows = self.warehouse_rows(load_null=True)

        self.assert_bare_ids(rows, ("company_id", "view_location_id", "lot_stock_id"))
        mine = next(row for row in rows if row["id"] == self.warehouse.id)
        self.assertEqual(
            (mine["view_location_id"], mine["lot_stock_id"]),
            (self.warehouse.view_location_id.id, self.warehouse.lot_stock_id.id),
        )
        self.assertEqual(calls, [])

    def test_picking_type_search_with_load_null_computes_no_company_label(self):
        with self.display_name_spies() as calls:
            rows = self.picking_type_rows(load_null=True)

        self.assert_bare_ids(rows, ("company_id",))
        self.assertEqual({row["company_id"] for row in rows}, {self.company.id})
        self.assertEqual(calls, [])

    # -- the control: Odoo's default load --------------------------------------------------------

    def test_without_load_the_same_requests_compute_every_label_as_superuser(self):
        readers = (
            (self.location_rows, ("company_id", "location_id")),
            (self.warehouse_rows, ("company_id", "view_location_id", "lot_stock_id")),
            (self.picking_type_rows, ("company_id",)),
        )
        for read, relations in readers:
            with self.subTest(read=read.__name__):
                bare = read(load_null=True)
                with self.display_name_spies() as calls:
                    labelled = read(load_null=False)

                # The same rows and the same ids; only the label differs.
                pairs = {
                    (row["id"], name): row[name][0] if row[name] else False
                    for row in labelled
                    for name in relations
                }
                ids = {(row["id"], name): row[name] for row in bare for name in relations}
                self.assertEqual(pairs, ids)
                self.assertTrue(
                    all(
                        isinstance(row[name], list)
                        for row in labelled
                        for name in relations
                        if row[name]
                    )
                )

                related = {}
                for row in labelled:
                    for name in relations:
                        if row[name]:
                            model = "res.company" if name == "company_id" else "stock.location"
                            related.setdefault(model, set()).add(row[name][0])
                self.assertTrue(calls)
                self.assertTrue(all(su for _, _, su in calls), "labels are computed as superuser")
                for model, expected in related.items():
                    computed = set().union(*(ids for name, ids, _ in calls if name == model))
                    self.assertLessEqual(expected, computed, model)
