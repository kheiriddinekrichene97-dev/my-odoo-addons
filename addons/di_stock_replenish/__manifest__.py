{
    'name': 'Stock Replenishment (Riassortimento)',
    'version': '19.0.12.2.0',
    'summary': 'Compare shop stock against the central warehouse to find what has run out',
    'description': """
Stock Replenishment (Riassortimento)
====================================
Answers the one question the legacy ESC comparison screen answered and that
standard Odoo reporting cannot: **which products is a shop out of, that the
central warehouse still holds?**

A product with no stock at a shop has no ``stock.quant`` record there - quants
are created by movement - so the absence cannot be filtered or sorted on. The
Locations report, pivoted by product and warehouse, gets the comparison but
never the gap.

This module puts the source warehouses and the chosen shops side by side, one
row per variant, narrowed by season, brand, supplier, posizionamento, discount
or product category, and a *Show* selector that keeps every row, only what a
shop has run out of, only what has gone below zero, or only what EVERY shop is
down to fewer than a number the worker types in. A shop at -3 sold stock that
was never counted in, which is a counting error as much as a gap; a style every
shop is down to 2 of is worth a top-up before it becomes either.

The first three modes ask whether *any* destination is under the line - one
empty shelf is reason enough to send a transfer. *Below a Quantity* asks
whether *every* one is, because asked the other way a single habitually empty
shop satisfies it for the whole catalogue.

Quantities are typed straight into the grid, and confirming generates one
internal transfer per source and destination, left Ready so the worker can
check the pile against it before validating in Inventory.

Free to use is shown at the source and on hand at the destinations: you care
what can actually be moved, and what is actually on the shop's shelf.
""",
    'author': 'Dimensione Immagine',
    'category': 'Inventory/Inventory',
    'depends': ['stock', 'product', 'mail', 'di_stock_arrival', 'di_tag_export'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'report/replenish_report.xml',
        'report/replenish_report_templates.xml',
        'views/stock_replenish_views.xml',
        'views/stock_replenish_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'di_stock_replenish/static/src/**/*',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
