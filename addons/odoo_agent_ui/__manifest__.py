{
    "name": "Odoo agent UI",
    "version": "19.0.1.0.0",
    "summary": "Read-only, Odoo-native shell for the autonomous agent (demo transport only)",
    "author": "odoo-autonomous-agent",
    "category": "Productivity",
    "license": "LGPL-3",
    # Deliberately not `odoo_agent_gateway`: the UI grants nothing and needs none of its models.
    "depends": ["web"],
    "data": [
        # The group must load before the menu that references it.
        "security/agent_ui_groups.xml",
        "views/agent_ui_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "odoo_agent_ui/static/src/agent/**/*",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
