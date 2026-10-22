"""The deployment measurement inside a real, disposable Odoo 19 (ADR 0020).

Run by Odoo's own runner in the ``odoo-addon-integration`` job (and gated by a pinned count in
``scripts/verify_odoo_addon_tests.py``). The surface here is ``res.company`` and ``res.partner``,
because this addon depends on ``base`` alone; the topology surface is measured end to end, through
the real signer, in the browser lane.

What is shown:

- **who may measure** -- only a holder of the measurement group; not the service identity itself,
  not an administrator, not superuser mode;
- **what is refused** before any work -- a malformed uid, an unsorted or oversized surface, an
  unknown field or user;
- **what is measured** -- each fact checked against Odoo independently (the load order against
  ``ModuleGraph``, method owners against the MRO, field attributes against ``_fields``);
- **that drift moves it** -- a manual field, a new record rule, one more group, each changes exactly
  the part of the answer it should;
- **the real transport** -- ``/json/2/`` with real API keys, including the two standard calls the
  signer makes with the *service* credential (``context_get`` and reading its own login).
"""

import io
import json
import zipfile
from datetime import datetime, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.modules.module_graph import ModuleGraph
from odoo.tests import HttpCase, TransactionCase, tagged

MEASUREMENT_GROUP = "odoo_agent_attestation.group_deployment_measurement"
MODEL = "odoo.agent.deployment.measurement"
METHOD = "agent_measure_deployment_v1"
SURFACE = ["res.company.id", "res.company.name", "res.partner.name"]


class MeasurementCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        internal = cls.env.ref("base.group_user")
        measure = cls.env.ref(MEASUREMENT_GROUP)
        cls.service = cls.env["res.users"].create(
            {
                "name": "Measured service",
                "login": "measured.service",
                "group_ids": [(6, 0, [internal.id])],
            }
        )
        cls.measurer = cls.env["res.users"].create(
            {
                "name": "Measurer",
                "login": "measurer",
                "group_ids": [(6, 0, [internal.id, measure.id])],
            }
        )
        cls.ordinary = cls.env["res.users"].create(
            {"name": "Ordinary", "login": "ordinary.user", "group_ids": [(6, 0, [internal.id])]}
        )

    def measure(self, user=None, service_uid=None, surface=None):
        model = self.env[MODEL].with_user(user or self.measurer)
        return model.agent_measure_deployment_v1(
            self.service.id if service_uid is None else service_uid,
            list(SURFACE if surface is None else surface),
        )


@tagged("post_install", "-at_install")
class TestMeasurementAuthorization(MeasurementCase):
    def test_the_group_is_unassigned_and_implied_by_nothing_on_install(self):
        group = self.env.ref(MEASUREMENT_GROUP)
        preexisting = group.user_ids - self.measurer
        self.assertFalse(preexisting)
        implying = self.env["res.groups"].search([("implied_ids", "in", group.id)])
        self.assertFalse(implying)
        self.assertFalse(self.env.ref("base.user_admin").has_group(MEASUREMENT_GROUP))

    def test_a_user_without_the_group_is_refused(self):
        with self.assertRaisesRegex(AccessError, "requires-the-measurement-group"):
            self.measure(user=self.ordinary)

    def test_superuser_mode_is_refused(self):
        with self.assertRaisesRegex(UserError, "refuses-superuser-mode"):
            self.env[MODEL].sudo().agent_measure_deployment_v1(self.service.id, SURFACE)

    def test_an_administrator_holding_the_group_is_refused(self):
        admin = self.env["res.users"].create(
            {
                "name": "Measuring admin",
                "login": "measuring.admin",
                "group_ids": [
                    (
                        6,
                        0,
                        [self.env.ref("base.group_system").id, self.env.ref(MEASUREMENT_GROUP).id],
                    )
                ],
            }
        )
        with self.assertRaisesRegex(AccessError, "refuses-an-administrator"):
            self.measure(user=admin)

    def test_the_measurer_cannot_measure_itself_as_the_service_identity(self):
        with self.assertRaisesRegex(UserError, "measurer-is-the-service-identity"):
            self.measure(service_uid=self.measurer.id)

    def test_the_method_runs_on_a_readonly_cursor_over_rpc(self):
        method = getattr(type(self.env[MODEL]), METHOD)
        self.assertTrue(getattr(method, "_readonly", False))


@tagged("post_install", "-at_install")
class TestMeasurementBounds(MeasurementCase):
    def test_malformed_arguments_are_refused_with_a_closed_code(self):
        cases = [
            ({"service_uid": 0}, "service-uid-invalid"),
            ({"service_uid": -3}, "service-uid-invalid"),
            ({"surface": []}, "surface-invalid"),
            ({"surface": ["res.partner.name", "res.company.name"]}, "surface-invalid"),
            ({"surface": ["res.partner.name", "res.partner.name"]}, "surface-invalid"),
            ({"surface": ["not a field"]}, "surface-invalid"),
            (
                {"surface": [f"res.partner.x_{index:03d}" for index in range(65)]},
                "surface-too-large",
            ),
            ({"surface": ["res.partner.no_such_field"]}, "surface-field-unknown"),
            ({"surface": ["no.such.model.name"]}, "surface-field-unknown"),
            ({"service_uid": 10**9}, "service-user-unknown"),
        ]
        for arguments, code in cases:
            with (
                self.subTest(code=code, arguments=arguments),
                self.assertRaisesRegex(UserError, code),
            ):
                self.measure(**arguments)

    def test_a_non_integer_uid_is_refused(self):
        with self.assertRaisesRegex(UserError, "service-uid-invalid"):
            self.env[MODEL].with_user(self.measurer).agent_measure_deployment_v1("1", SURFACE)


@tagged("post_install", "-at_install")
class TestMeasuredFacts(MeasurementCase):
    def test_the_database_series_and_module_load_order_are_odoos_own(self):
        document = self.measure()
        self.assertEqual(document["database"], self.env.cr.dbname)
        self.assertEqual(document["odoo"]["series"], "19.0")

        installed = self.env["ir.module.module"].search([("state", "=", "installed")])
        graph = ModuleGraph(self.env.cr, mode="load")
        graph.extend(sorted(installed.mapped("name")))
        self.assertEqual([m["name"] for m in document["modules"]], [node.name for node in graph])
        self.assertEqual(document["modules"][0]["name"], "base")
        versions = {module.name: module.latest_version for module in installed}
        for module in document["modules"]:
            self.assertEqual(module["version"], versions[module["name"]])

    def test_source_trees_cover_every_installed_module_and_separate_core_from_addons(self):
        document = self.measure()
        trees = {tree["name"]: tree for tree in document["source"]["modules"]}
        self.assertEqual(set(trees), {m["name"] for m in document["modules"]})
        self.assertEqual(trees["base"]["root"], "core")
        self.assertEqual(trees["odoo_agent_attestation"]["root"], "addon")
        for tree in trees.values():
            self.assertRegex(tree["tree"], r"^[0-9a-f]{64}$")
        self.assertRegex(document["source"]["core"], r"^[0-9a-f]{64}$")

    def test_field_attributes_are_what_the_registry_built(self):
        document = self.measure()
        fields = {field["field"]: field for field in document["fields"]}
        self.assertEqual(sorted(fields), SURFACE)
        name = self.env["res.partner"]._fields["name"]
        self.assertEqual(fields["res.partner.name"]["type"], name.type)
        self.assertEqual(fields["res.partner.name"]["store"], bool(name.store))
        self.assertIn("base", fields["res.partner.name"]["modules"])
        company_id = fields["res.company.id"]
        self.assertEqual((company_id["type"], company_id["readonly"]), ("integer", True))

    def test_method_owners_follow_the_real_mro(self):
        document = self.measure()
        owners = {entry["method"]: entry["owners"] for entry in document["methods"]}
        cls = type(self.env["res.partner"])
        expected = []
        for klass in cls.__mro__:
            if "search_read" in vars(klass):
                module = vars(klass).get("_module")
                expected.append(module if module else f"python:{klass.__module__}")
        self.assertEqual(owners["res.partner.search_read"], expected)
        self.assertEqual(owners["res.partner.search_read"][-1], "python:odoo.orm.models")
        self.assertIn("res.company.read", owners)

    def test_the_service_identity_is_measured_as_odoo_resolves_it(self):
        document = self.measure()
        service = document["service"]
        self.assertEqual((service["uid"], service["login"]), (self.service.id, "measured.service"))
        self.assertTrue(service["active"])
        self.assertFalse(service["share"])
        self.assertIn("base.group_user", service["groups"])
        expected = sorted(
            sorted(self.service.all_group_ids._get_external_ids()[group.id])[0]
            for group in self.service.all_group_ids
        )
        self.assertEqual(service["groups"], expected)
        self.assertEqual(service["unnamed_groups"], 0)
        self.assertEqual(service["companies"], sorted(self.service.company_ids.ids))
        access = {entry["model"]: entry for entry in service["access"]}
        self.assertTrue(access["res.partner"]["read"])
        self.assertFalse(access["res.company"]["unlink"])

    def test_access_control_lists_the_acls_and_rules_of_the_surface_models(self):
        document = self.measure()
        acl_models = {entry["model"] for entry in document["access"]["acl"]}
        self.assertEqual(acl_models, {"res.company", "res.partner"})
        rules = (
            self.env["ir.rule"]
            .with_context(active_test=False)
            .search([("model_id.model", "in", ["res.company", "res.partner"])])
        )
        self.assertEqual(len(document["access"]["rules"]), len(rules))

    def test_an_unchanged_deployment_measures_to_the_same_document(self):
        self.assertEqual(self.measure(), self.measure())


@tagged("post_install", "-at_install")
class TestMeasurementDrift(MeasurementCase):
    def test_a_manual_field_appears_in_studio_state(self):
        before = self.measure()
        self.env["ir.model.fields"].create(
            {
                "name": "x_measured_drift",
                "model_id": self.env.ref("base.model_res_partner").id,
                "ttype": "char",
                "field_description": "Measured drift",
            }
        )
        after = self.measure()
        self.assertEqual(before["studio"]["manual_fields"], [])
        self.assertIn("res.partner.x_measured_drift:char", after["studio"]["manual_fields"])
        self.assertEqual(before["fields"], after["fields"])

    def test_a_new_record_rule_appears_in_access_control(self):
        before = self.measure()
        self.env["ir.rule"].create(
            {
                "name": "Measured drift rule",
                "model_id": self.env.ref("base.model_res_partner").id,
                "domain_force": "[('id', '!=', 0)]",
            }
        )
        after = self.measure()
        self.assertEqual(len(after["access"]["rules"]), len(before["access"]["rules"]) + 1)
        self.assertEqual(before["studio"], after["studio"])

    def test_one_more_group_on_the_service_user_appears_in_its_effective_groups(self):
        before = self.measure()
        self.service.group_ids = [(4, self.env.ref("base.group_partner_manager").id)]
        after = self.measure()
        self.assertNotIn("base.group_partner_manager", before["service"]["groups"])
        self.assertIn("base.group_partner_manager", after["service"]["groups"])


DATA_MODULE = "x_agent_data_only"
DATA_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="agent_data_only_tag" model="res.partner.category">
        <field name="name">Agent data-only tag</field>
    </record>
    <record id="agent_data_only_second_tag" model="res.partner.category">
        <field name="name">Agent data-only second tag</field>
    </record>
</odoo>
"""


def import_data_module(env, name=DATA_MODULE, version="1.0"):
    """Import a data-only module exactly as Settings > Apps > Import Module does."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        manifest = {
            "name": "Agent data-only test module",
            "version": version,
            "depends": ["base"],
            "data": ["data.xml"],
            "license": "LGPL-3",
        }
        archive.writestr(f"{name}/__manifest__.py", repr(manifest))
        archive.writestr(f"{name}/data.xml", DATA_XML)
    buffer.seek(0)
    env["ir.module.module"].sudo()._import_zipfile(buffer)
    return env["ir.module.module"].sudo().search([("name", "=", name)])


def graph_names(env):
    installed = env["ir.module.module"].sudo().search([("state", "=", "installed")])
    graph = ModuleGraph(env.cr, mode="load")
    graph.extend(sorted(installed.mapped("name")))
    return {node.name for node in graph}


@tagged("post_install", "-at_install")
class TestDataOnlyModules(MeasurementCase):
    """Studio and imported modules: installed, live, and outside Odoo's load graph (ADR 0021)."""

    def data_modules(self, document):
        return {module["name"]: module for module in document["data_modules"]}

    def test_imported_modules_are_real_here(self):
        """``base_import_module`` is installed, so the ``imported`` column is real, not assumed."""

        self.assertIn("imported", self.env["ir.module.module"]._fields)
        self.assertEqual(self.env.ref("base.module_base_import_module").state, "installed")

    def test_a_legitimate_imported_module_is_measured_as_data_only(self):
        module = import_data_module(self.env)
        self.assertTrue(module.imported)
        self.assertEqual(module.state, "installed")
        self.assertNotIn(DATA_MODULE, graph_names(self.env), "Odoo itself does not load it")

        document = self.measure()
        measured = self.data_modules(document)[DATA_MODULE]
        owned = self.env["ir.model.data"].search([("module", "=", DATA_MODULE)])
        self.assertEqual(
            (measured["kind"], measured["version"], measured["depends"], measured["records"]),
            ("imported", module.latest_version, ["base"], len(owned)),
        )
        self.assertGreaterEqual(measured["records"], 2)
        self.assertRegex(measured["data"], r"^[0-9a-f]{64}$")
        self.assertNotIn(DATA_MODULE, [m["name"] for m in document["modules"]])
        self.assertNotIn(DATA_MODULE, [t["name"] for t in document["source"]["modules"]])

    def test_a_studio_module_is_data_only_and_its_customizations_are_still_measured(self):
        studio = self.env["ir.module.module"].create(
            {"name": "studio_customization", "state": "installed", "latest_version": "19.0.1.0"}
        )
        self.env["ir.module.module.dependency"].create({"module_id": studio.id, "name": "base"})
        tag = self.env["res.partner.category"].create({"name": "Studio-made tag"})
        self.env["ir.model.data"].create(
            {
                "module": "studio_customization",
                "name": "studio_tag",
                "model": "res.partner.category",
                "res_id": tag.id,
            }
        )
        self.assertNotIn("studio_customization", graph_names(self.env))

        document = self.measure()
        measured = self.data_modules(document)["studio_customization"]
        self.assertEqual((measured["kind"], measured["records"]), ("studio", 1))
        self.assertIn("res.partner.category:studio_tag", document["studio"]["customizations"])

    def test_an_unexplained_installed_module_absent_from_the_graph_is_refused(self):
        self.env["ir.module.module"].create({"name": "x_agent_ghost", "state": "installed"})
        self.assertNotIn("x_agent_ghost", graph_names(self.env))
        with self.assertRaisesRegex(UserError, "module-graph-inconsistent"):
            self.measure()

    def test_executable_code_flagged_imported_is_refused_as_ambiguous(self):
        self.env.ref("base.module_odoo_agent_ui").imported = True
        with self.assertRaisesRegex(UserError, "module-classification-ambiguous"):
            self.measure()

    def test_a_data_only_module_with_a_dependency_that_is_not_installed_is_refused(self):
        module = import_data_module(self.env)
        self.env["ir.module.module.dependency"].create(
            {"module_id": module.id, "name": "x_agent_never_installed"}
        )
        with self.assertRaisesRegex(UserError, "data-module-dependency-inconsistent"):
            self.measure()

    def test_data_only_drift_changes_exactly_the_data_only_section(self):
        module = import_data_module(self.env)
        tag = self.env.ref(f"{DATA_MODULE}.agent_data_only_tag")
        base = self.measure()
        entry = lambda document: self.data_modules(document)[DATA_MODULE]  # noqa: E731

        # A record the module owns is edited later (its write_date moves).
        self.env.cr.execute(
            "UPDATE res_partner_category SET write_date = write_date + interval '1 minute' "
            "WHERE id = %s",
            [tag.id],
        )
        self.env.invalidate_all()
        edited = self.measure()
        self.assertNotEqual(entry(edited)["data"], entry(base)["data"])
        self.assertEqual(entry(edited)["records"], entry(base)["records"])

        # A record is added to the module.
        extra = self.env["res.partner.category"].create({"name": "Added later"})
        self.env["ir.model.data"].create(
            {"module": DATA_MODULE, "name": "added_later", "model": extra._name, "res_id": extra.id}
        )
        added = self.measure()
        self.assertEqual(entry(added)["records"], entry(edited)["records"] + 1)

        # A record the module owns is deleted (the ORM removes its external id with it).
        self.env.ref(f"{DATA_MODULE}.agent_data_only_second_tag").unlink()
        removed = self.measure()
        self.assertEqual(entry(removed)["records"], entry(added)["records"] - 1)
        self.assertNotEqual(entry(removed)["data"], entry(added)["data"])

        # The module is re-imported at another version.
        module.latest_version = "1.1"
        upgraded = self.measure()
        self.assertEqual(entry(upgraded)["version"], "1.1")

        # Nothing else in the document moved.
        for section in ("modules", "source", "fields", "methods", "access", "service"):
            self.assertEqual(upgraded[section], base[section], section)

    def entry(self, document):
        return self.data_modules(document)[DATA_MODULE]

    def test_two_orm_edits_in_the_same_second_are_told_apart_by_microseconds(self):
        """``write_date`` is the transaction timestamp; PostgreSQL keeps it to the microsecond."""

        import_data_module(self.env)
        tag = self.env.ref(f"{DATA_MODULE}.agent_data_only_tag")
        first = datetime(2026, 9, 30, 10, 0, 0, 100_000)
        second = first.replace(microsecond=200_000)
        cursor = self.env.cr
        previous = cursor._now
        try:
            cursor._now = first  # what cr.now() returns for this transaction
            tag.write({"name": "Edited once"})
            self.env.flush_all()
            self.env.invalidate_all()
            self.assertEqual(tag.write_date, first)
            once = self.measure()

            cursor._now = second
            tag.write({"name": "Edited twice"})
            self.env.flush_all()
            self.env.invalidate_all()
            self.assertEqual(tag.write_date, second)
            twice = self.measure()
        finally:
            cursor._now = previous
        # Same second: a second-precision timestamp could not tell these apart.
        self.assertEqual(first.replace(microsecond=0), second.replace(microsecond=0))
        self.assertNotEqual(self.entry(once)["data"], self.entry(twice)["data"])

    def test_a_record_without_write_tracking_is_measured_by_its_stored_content(self):
        import_data_module(self.env)
        profiles = self.env["ir.profile"]
        self.assertFalse(profiles._log_access)
        self.assertNotIn("write_date", profiles._fields)
        profile = profiles.create({"name": "Agent untracked record", "session": "agent-test"})
        self.env["ir.model.data"].create(
            {
                "module": DATA_MODULE,
                "name": "untracked_profile",
                "model": "ir.profile",
                "res_id": profile.id,
            }
        )
        before = self.measure()
        profile.write({"name": "Agent untracked record, edited"})
        self.env.flush_all()
        after = self.measure()
        self.assertEqual(self.entry(after)["records"], self.entry(before)["records"])
        self.assertNotEqual(self.entry(after)["data"], self.entry(before)["data"])

    def test_a_tracked_row_without_a_write_date_is_measured_by_content_not_accepted(self):
        import_data_module(self.env)
        tag = self.env.ref(f"{DATA_MODULE}.agent_data_only_tag")
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE res_partner_category SET write_date = NULL WHERE id = %s", [tag.id]
        )
        self.env.invalidate_all()
        before = self.measure()
        # Changed without touching write_date: only the stored content shows it.
        self.env.cr.execute(
            "UPDATE res_partner_category SET name = %s WHERE id = %s",
            ['{"en_US": "Changed behind the ORM"}', tag.id],
        )
        self.env.invalidate_all()
        after = self.measure()
        self.assertNotEqual(self.entry(after)["data"], self.entry(before)["data"])

    def test_the_measurement_user_measures_and_the_restricted_read_user_is_refused(self):
        import_data_module(self.env)
        self.assertIn(DATA_MODULE, self.data_modules(self.measure()))
        with self.assertRaisesRegex(AccessError, "requires-the-measurement-group"):
            self.measure(user=self.service)


@tagged("post_install", "-at_install")
class TestMeasurementRpcDispatch(HttpCase, MeasurementCase):
    """Over the real ``/json/2/`` dispatcher with real API keys, as the signer calls it."""

    def api_key_for(self, user):
        return (
            self.env["res.users.apikeys"]
            .with_user(user)
            ._generate(None, f"itest-{user.login}", datetime.now() + timedelta(days=1))
        )

    def post(self, path, key, body):
        return self.url_open(
            path,
            data=json.dumps(body),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-Odoo-Database": self.env.cr.dbname,
            },
        )

    def call(self, user, **body):
        payload = {"service_uid": self.service.id, "surface_fields": SURFACE}
        payload.update(body)
        return self.post(f"/json/2/{MODEL}/{METHOD}", self.api_key_for(user), payload)

    def test_the_measurement_user_is_answered_with_the_whole_document(self):
        response = self.call(self.measurer)
        self.assertEqual(response.status_code, 200)
        document = response.json()
        self.assertEqual(
            sorted(document),
            [
                "access",
                "data_modules",
                "database",
                "fields",
                "methods",
                "modules",
                "odoo",
                "schema_version",
                "service",
                "source",
                "studio",
            ],
        )
        self.assertEqual(document["schema_version"], "2")
        self.assertEqual(document["service"]["login"], "measured.service")

    def test_with_a_data_only_module_the_measurer_is_answered_and_the_read_user_refused(self):
        import_data_module(self.env)
        measured = self.call(self.measurer)
        self.assertEqual(measured.status_code, 200)
        self.assertIn(DATA_MODULE, [m["name"] for m in measured.json()["data_modules"]])
        refused = self.call(self.service)
        self.assertEqual(refused.status_code, 403)
        self.assertIn("requires-the-measurement-group", refused.json()["message"])

    def test_an_ordinary_user_is_refused_over_rpc(self):
        response = self.call(self.ordinary)
        self.assertEqual(response.status_code, 403)
        self.assertIn("requires-the-measurement-group", response.json()["message"])

    def test_the_service_identity_cannot_measure_itself_over_rpc(self):
        service_with_group = self.service
        service_with_group.group_ids = [(4, self.env.ref(MEASUREMENT_GROUP).id)]
        response = self.call(service_with_group)
        self.assertEqual(response.status_code, 422)
        self.assertIn("measurer-is-the-service-identity", response.json()["message"])

    def test_a_revoked_or_unknown_key_is_401(self):
        response = self.post(
            f"/json/2/{MODEL}/{METHOD}",
            "not-a-real-key",
            {"service_uid": self.service.id, "surface_fields": SURFACE},
        )
        self.assertEqual(response.status_code, 401)

    def test_the_service_credential_can_name_itself_with_standard_calls_only(self):
        """The two calls the signer makes with the *service* key, and nothing more."""

        key = self.api_key_for(self.service)
        context = self.post("/json/2/res.users/context_get", key, {})
        self.assertEqual(context.status_code, 200)
        self.assertEqual(context.json()["uid"], self.service.id)
        read = self.post(
            "/json/2/res.users/read", key, {"ids": [self.service.id], "fields": ["login"]}
        )
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json(), [{"id": self.service.id, "login": "measured.service"}])
