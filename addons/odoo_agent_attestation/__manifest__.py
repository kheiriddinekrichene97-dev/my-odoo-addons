{
    "name": "Odoo agent attestation measurement",
    "version": "19.0.1.1.0",
    "summary": "Read-only in-process measurement of this deployment for the attestation signer",
    "author": "odoo-autonomous-agent",
    "category": "Technical",
    "license": "LGPL-3",
    # Deliberately `base` only: the measurement is told which models to measure, and must not pull
    # in -- and so change -- the module set it reports.
    "depends": ["base"],
    "data": [
        # The group must load before the ACL that references it.
        "security/agent_attestation_groups.xml",
        "security/ir.model.access.csv",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
