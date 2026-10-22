{
    "name": "DI Studio Fields",
    "version": "19.0.1.0.0",
    "summary": "Recreates the production Studio customizations (Marche, Stagioni, Reparto, Posizionamenti) on Community",
    "description": """
The Dimensione Immagine modules (di_*, pricelist_extend) use product
classification models and fields that were created with Odoo Studio in
production. Studio is Enterprise-only, so this module declares the same
models and fields, with the same technical names, as data — exactly like
Studio stores them — so the modules can be installed unchanged.
""",
    "category": "Hidden",
    "depends": ["product", "stock"],
    "data": [
        "data/studio_models.xml",
        "security/ir.model.access.csv",
        "views/studio_views.xml",
    ],
    "installable": True,
    "license": "LGPL-3",
}
