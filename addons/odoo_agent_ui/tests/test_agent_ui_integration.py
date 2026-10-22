"""Integration tests for the ``odoo_agent_ui`` addon, on real Odoo 19.

What is claimed, against Odoo itself:

* **Nothing is granted.** The UI group ships unassigned; the menu and both routes refuse every user
  without it, over real HTTP; the addon references no projection group, no gateway model and no
  business model, and writes nothing.
* **The selector is the server's.** The bootstrap is computed per request; a workflow sent back is
  re-authorized, so a demo, unknown or unavailable id gains nothing.
* **Demo data needs both operator signals.** Demo workflows are offered only when
  ``ODOO_AGENT_UI_DEMO=1`` and ``database.is_neutralized`` is truthy; with either missing, no demo
  workflow is offered and a demo id is refused exactly like an unknown one. Both signals are
  operator-controlled, not an attestation that the environment is not production; these tests
  prove the gate, not the environment.
* **The screen is registered and its templates are accessible.** The client action and its assets
  are in ``web.assets_backend``, and the Owl templates pass a structural accessibility audit.

The pinned Odoo image ships no Chrome, so browser-driven tests (tours, Hoot) cannot run in this job;
the browser-side logic is tested with ``node --test`` in the ``quality`` job, and a headless-Chrome
smoke run is documented in ``docs/UI.md``.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

from lxml import etree
from odoo.tests import HttpCase, TransactionCase, tagged

from ..ui_protocol import catalog

UI_GROUP = "odoo_agent_ui.group_agent_ui_user"
ADDON = Path(__file__).resolve().parents[1]
TEMPLATES = ADDON / "static/src/agent/agent_app.xml"
DEMO_ENV = {catalog.DEMO_ENVIRONMENT_VARIABLE: "1"}


class UiCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ui_user = cls._user("agent.ui.user", ui=True)
        cls.plain_user = cls._user("agent.ui.plain", ui=False)

    @classmethod
    def _user(cls, login, *, ui):
        groups = [cls.env.ref("base.group_user").id]
        if ui:
            groups.append(cls.env.ref(UI_GROUP).id)
        return cls.env["res.users"].create(
            {
                "name": login,
                "login": login,
                "password": f"{login}-pw",
                "group_ids": [(6, 0, groups)],
            }
        )

    def neutralize(self, value="True"):
        self.env["ir.config_parameter"].sudo().set_param("database.is_neutralized", value)


@tagged("post_install", "-at_install")
class TestUiInstallGrantsNothing(UiCase):
    def test_the_ui_group_ships_unassigned_and_implies_nothing(self):
        group = self.env.ref(UI_GROUP)
        self.assertEqual(group.user_ids - self.ui_user, self.env["res.users"])
        self.assertFalse(group.implied_ids)
        self.assertFalse(group.privilege_id)
        for login in ("admin", "__system__"):
            user = (
                self.env["res.users"]
                .with_context(active_test=False)
                .search([("login", "=", login)])
            )
            if user:
                self.assertNotIn(group, user.all_group_ids, login)

    def test_the_menu_is_visible_only_with_the_ui_group(self):
        menu = self.env.ref("odoo_agent_ui.menu_agent_ui_root")
        self.assertEqual(menu.group_ids, self.env.ref(UI_GROUP))
        visible = self.env["ir.ui.menu"].with_user(self.ui_user)._visible_menu_ids()
        hidden = self.env["ir.ui.menu"].with_user(self.plain_user)._visible_menu_ids()
        self.assertIn(menu.id, visible)
        self.assertNotIn(menu.id, hidden)

    def test_the_addon_touches_no_projection_group_gateway_or_business_model(self):
        self.assertFalse(
            self.env["ir.model.data"].search(
                [("module", "=", "odoo_agent_ui"), ("model", "=", "ir.model.access")]
            )
        )
        self.assertFalse(
            self.env["ir.model.data"].search(
                [("module", "=", "odoo_agent_ui"), ("model", "in", ["ir.model", "ir.rule"])]
            )
        )
        manifest = (ADDON / "__manifest__.py").read_text(encoding="utf-8")
        self.assertIn('"depends": ["web"]', manifest)
        for path in ADDON.rglob("*"):
            # Runtime code and data only: Odoo never loads `tests/` or `unit_tests/`, and test code
            # may name the projection group precisely to assert it stays unassigned.
            if path.suffix in {".py", ".xml", ".js", ".csv"} and not {
                "tests",
                "unit_tests",
            } & set(path.parts):
                text = path.read_text(encoding="utf-8")
                if path.suffix == ".xml":  # comments may explain what is *not* referenced
                    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
                self.assertNotIn("group_agent_projection_service", text, path.name)
                self.assertNotIn("odoo.addons.odoo_agent_gateway", text, path.name)
                self.assertNotIn('ref="odoo_agent_gateway.', text, path.name)
        source = (ADDON / "controllers/main.py").read_text(encoding="utf-8")
        for forbidden in (".create(", ".write(", ".unlink(", "set_param", "execute("):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertEqual(source.count(".sudo()"), 1)

    def test_the_client_action_and_its_assets_are_registered(self):
        action = self.env.ref("odoo_agent_ui.action_agent_ui")
        self.assertEqual((action.type, action.tag), ("ir.actions.client", "odoo_agent_ui.agent"))
        paths = [
            path for path, *_ in self.env["ir.asset"]._get_asset_paths("web.assets_backend", {})
        ]
        for name in ("agent_app.js", "agent_app.xml", "agent_app.scss", "event_protocol.js"):
            self.assertTrue(
                any(path.endswith(f"odoo_agent_ui/static/src/agent/{name}") for path in paths), name
            )
        self.assertFalse(any("odoo_agent_ui/static/tests" in path for path in paths))


@tagged("post_install", "-at_install")
class TestUiDemoGate(UiCase):
    def demo_mode(self):
        from ..controllers.main import AgentUiController

        request = type("R", (), {"env": self.env(user=self.ui_user)})()
        with patch("odoo.addons.odoo_agent_ui.controllers.main.request", request):
            return AgentUiController._demo_mode()

    def test_demo_needs_both_the_operator_flag_and_a_neutralized_database(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(catalog.DEMO_ENVIRONMENT_VARIABLE, None)
            self.neutralize("True")
            self.assertFalse(self.demo_mode())
        with patch.dict(os.environ, DEMO_ENV):
            self.neutralize("")
            self.assertFalse(self.demo_mode())
            self.neutralize("True")
            self.assertTrue(self.demo_mode())

    def test_a_fresh_database_is_not_neutralized(self):
        self.assertFalse(
            self.env["ir.config_parameter"].sudo().get_param("database.is_neutralized")
        )


@tagged("post_install", "-at_install")
class TestUiRoutes(HttpCase, UiCase):
    def rpc(self, user, route, params=None):
        self.authenticate(user.login, f"{user.login}-pw")
        response = self.url_open(
            route,
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "id": 1, "params": params or {}}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def result(self, user, route, params=None):
        payload = self.rpc(user, route, params)
        self.assertNotIn("error", payload, payload.get("error"))
        return payload["result"]

    def error_name(self, user, route, params=None):
        return self.rpc(user, route, params)["error"]["data"]["name"]

    def run_params(self, workflow_id, message="What needs attention today?", attempt=1):
        return {"workflow_id": workflow_id, "message": message, "attempt": attempt}

    def test_a_user_without_the_ui_group_is_refused_by_both_routes(self):
        for route, params in (
            ("/odoo_agent_ui/bootstrap", {}),
            ("/odoo_agent_ui/run", self.run_params("demo.inventory_exceptions")),
        ):
            with self.subTest(route=route):
                self.assertEqual(
                    self.error_name(self.plain_user, route, params), "odoo.exceptions.AccessError"
                )

    def test_with_the_demo_gate_off_nothing_is_available_and_there_is_no_demo(self):
        """The configuration production must have. The test proves the gate, not the environment."""

        boot = self.result(self.ui_user, "/odoo_agent_ui/bootstrap")
        self.assertEqual(boot, catalog.bootstrap(demo_mode=False))
        self.assertIs(boot["writes_enabled"], False)
        self.assertEqual({w["availability"] for w in boot["workflows"]}, {"unavailable"})
        self.assertFalse([w for w in boot["workflows"] if w["demo"]])

    def test_selecting_a_workflow_grants_nothing(self):
        """A demo id outside demo mode, an unknown id and a real unavailable id are all refused,
        and none of the refusals carries demo data."""

        for workflow_id, code in (
            ("demo.inventory_exceptions", "workflow-not-authorized"),
            ("no.such_workflow", "workflow-not-authorized"),
            ("inventory.exceptions", "control-plane-not-connected"),
        ):
            with self.subTest(workflow_id=workflow_id):
                run = self.result(self.ui_user, "/odoo_agent_ui/run", self.run_params(workflow_id))
                self.assertIs(run["demo"], False)
                self.assertEqual(len(run["events"]), 1)
                failure = run["events"][0]["failure"]
                self.assertEqual((failure["kind"], failure["code"]), ("policy_blocked", code))
                self.assertNotIn("DEMO", json.dumps(run))

    def test_in_demo_mode_the_whole_interaction_is_served_and_marked(self):
        self.neutralize("True")
        with patch.dict(os.environ, DEMO_ENV):
            boot = self.result(self.ui_user, "/odoo_agent_ui/bootstrap")
            self.assertTrue(boot["demo_mode"])
            self.assertEqual(
                [w["id"] for w in boot["workflows"] if w["availability"] == "available"],
                ["demo.inventory_exceptions"],
            )
            outcomes = {}
            for message, attempt in (
                ("What needs attention today?", 1),
                ("Show stock I have no permission for", 1),
                ("Read transfers (policy blocked)", 1),
                ("Check stock while Odoo is offline", 1),
                ("Check stock while Odoo is offline", 2),
            ):
                run = self.result(
                    self.ui_user,
                    "/odoo_agent_ui/run",
                    self.run_params("demo.inventory_exceptions", message, attempt),
                )
                self.assertTrue(run["demo"] and all(event["demo"] for event in run["events"]))
                last = run["events"][-1]
                outcomes[(message, attempt)] = (last["type"], last.get("failure", {}).get("kind"))
                again = self.result(
                    self.ui_user,
                    "/odoo_agent_ui/run",
                    self.run_params("demo.inventory_exceptions", message, attempt),
                )
                self.assertEqual(again, run)  # deterministic
        self.assertEqual(
            list(outcomes.values()),
            [
                ("run.completed", None),
                ("run.failed", "permission"),
                ("run.failed", "policy_blocked"),
                ("run.failed", "temporary"),
                ("run.completed", None),
            ],
        )

    def test_the_run_arguments_are_bounded(self):
        for params in (
            self.run_params("Bad Id"),
            self.run_params("demo.inventory_exceptions", message="   "),
            self.run_params("demo.inventory_exceptions", message="x" * 2001),
            self.run_params("demo.inventory_exceptions", attempt=4),
            self.run_params("demo.inventory_exceptions", attempt=True),
        ):
            with self.subTest(params=str(params)[:60]):
                self.assertEqual(
                    self.error_name(self.ui_user, "/odoo_agent_ui/run", params),
                    "odoo.exceptions.UserError",
                )

    def test_the_web_client_serves_the_backend_bundle_with_the_screen(self):
        """The action loads in the real web client: the backend page renders for a UI user and its
        JavaScript bundle contains the registered client action and templates."""

        self.authenticate(self.ui_user.login, f"{self.ui_user.login}-pw")
        page = self.url_open("/odoo/action-odoo_agent_ui.action_agent_ui")
        self.assertEqual(page.status_code, 200)
        bundle = self.env["ir.qweb"]._get_asset_bundle("web.assets_backend")
        js = bundle.js().raw
        js = js.decode() if isinstance(js, bytes) else js
        self.assertIn('registry.category("actions").add("odoo_agent_ui.agent"', js)
        self.assertIn("odoo_agent_ui.AgentApp", js)
        self.assertIn("/odoo_agent_ui/run", js)


@tagged("post_install", "-at_install")
class TestUiTemplateAccessibility(TransactionCase):
    """A structural audit of the Owl templates: what a screen reader and a keyboard need."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tree = etree.parse(str(TEMPLATES))

    def nodes(self, xpath):
        return self.tree.xpath(xpath)

    def test_every_button_has_an_accessible_name_and_a_type(self):
        for button in self.nodes("//button"):
            name = (
                "".join(button.itertext()).strip()
                or button.get("aria-label")
                or button.get("t-esc")
            )
            self.assertTrue(name, etree.tostring(button)[:120])
            self.assertIn(button.get("type"), ("button", "submit"))

    def test_every_form_control_is_labelled(self):
        labels = {label.get("for") for label in self.nodes("//label")}
        for control in self.nodes("//select | //textarea | //input"):
            self.assertTrue(
                control.get("id") in labels or control.get("aria-label"),
                etree.tostring(control)[:120],
            )

    def test_icons_are_hidden_from_assistive_technology(self):
        for icon in self.nodes("//i[contains(@class, 'fa')]"):
            self.assertEqual(icon.get("aria-hidden"), "true", etree.tostring(icon)[:120])

    def test_live_regions_announce_progress_answers_and_failures(self):
        (log,) = self.nodes("//*[@role='log']")
        self.assertEqual(log.get("aria-live"), "polite")
        self.assertTrue(log.get("aria-label"))
        (card,) = self.nodes("//t[@t-name='odoo_agent_ui.FailureCard']/div")
        self.assertEqual(card.get("role"), "alert")
        self.assertIn("No Odoo action was performed.", "".join(card.itertext()))
        self.assertTrue(self.nodes("//*[@role='status' and contains(@class, 'o_agent_loading')]"))

    def test_failure_kinds_are_distinguished_by_text_not_only_colour(self):
        text = "".join(self.nodes("//t[@t-name='odoo_agent_ui.FailureCard']")[0].itertext())
        for words in ("Permission", "Blocked by policy", "Temporary problem"):
            self.assertIn(words, text)

    def test_demo_data_is_labelled_in_words_and_never_linked(self):
        demo_labels = [
            "".join(node.itertext()).strip()
            for node in self.nodes("//*[contains(@class, 'o_agent_badge_demo')]")
        ]
        self.assertIn("Demo record", demo_labels)
        self.assertIn("Demo data", demo_labels)
        (open_button,) = self.nodes("//button[contains(., 'Open in Odoo')]")
        self.assertEqual(open_button.get("t-if"), "!record.demo")

    def test_structure_headings_regions_and_no_positive_tabindex(self):
        self.assertTrue(self.nodes("//h1[@id='o_agent_title']"))
        self.assertTrue(self.nodes("//*[@aria-labelledby='o_agent_title']"))
        self.assertTrue(self.nodes("//aside[@aria-label]"))
        self.assertTrue(self.nodes("//section[@aria-label]"))
        for node in self.nodes("//*[@tabindex]"):
            self.assertLessEqual(int(node.get("tabindex")), 0)
