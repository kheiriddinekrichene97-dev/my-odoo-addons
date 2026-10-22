{
    'name': 'Stock by Location (Ubicazioni)',
    'version': '19.0.4.5.0',
    'summary': "Where a product's stock physically sits, on the product itself",
    'description': """
Stock by Location (Ubicazioni)
==============================
Answers "where are those pieces?" without leaving the product.

A Locations tab, after Inventory, on the product and on the variant - but
the two are shaped for different questions, not the same list twice.

**On the product** it reproduces the shop's previous management software's
"Esistenza per colori articolo" screen: every colour down the side, every
size across the top, and - under each colour - every warehouse in the
company, including a zero row for a shop that carries none of it. A Filiale
filter narrows to one warehouse. This answers "how does this style break
down", not just "where does stock currently sit".

**On a variant** it stays a flat per-location list. One variant already has
one fixed colour and one fixed size, so a colour/size grid for it would be a
single cell - the flat list is the right shape there, not a cut-down version
of the product's grid.

Read-only throughout: the Quantity On Hand figure elsewhere on the form
already opens the same records in inventory-adjustment mode, where typing a
number corrects real stock.
""",
    'author': 'Dimensione Immagine',
    'category': 'Inventory/Inventory',
    'depends': ['stock', 'product'],
    'data': [
        'views/product_product_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'di_stock_locations/static/src/**/*',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
