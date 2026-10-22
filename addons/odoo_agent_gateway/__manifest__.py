{
    "name": "Odoo agent gateway",
    "version": "19.0.1.0.0",
    "summary": "Checked read-only projections for the autonomous agent control plane",
    "author": "odoo-autonomous-agent",
    "category": "Technical",
    "license": "LGPL-3",
    "depends": ["stock"],
    "data": [
        # The group must load before the ACL that references it.
        "security/agent_gateway_groups.xml",
        "security/ir.model.access.csv",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
