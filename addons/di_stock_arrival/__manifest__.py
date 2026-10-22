{
    'name': 'Stock Arrivals (Arrivi Merce)',
    'version': '19.0.2.17.0',
    'summary': 'Record incoming goods without a purchase order and divide them across warehouses',
    'description': """
Stock Arrivals (Arrivi Merce)
=============================
Dimensione Immagine receives goods from suppliers without ever raising a
purchase order, so the native Purchase -> Receipt flow does not apply. This
module provides the document that was missing:

1. **Arrival** - a worker records what physically turned up at the central
   warehouse: supplier, DDT reference, date, and one line per product variant
   with the quantity received. Lines can be typed in, scanned, or imported
   from a CSV or Excel file. Confirming the arrival generates and validates a single
   receipt, so the stock is real and countable straight away.

2. **Division** - the worker then allocates each line across destination
   warehouses. Validating generates one internal transfer per destination.

**Partial division is normal, not an error.** Goods routinely stay at the
central warehouse; whatever is not allocated simply remains there and needs
no paperwork. Closing the document is therefore an explicit user action and
is never gated on full allocation.

This replaces the legacy ESC "packing list" pair (``Fornitore`` for the
arrival, ``Deposito`` for each distribution) and adds the parent-to-children
link the legacy system never kept, so an arrival can always be traced to the
transfers it produced.

The source warehouse is a configurable company setting rather than a
hardcoded constant, and workers validate their own arrivals without a
manager counter-signature.
""",
    'author': 'Dimensione Immagine',
    'category': 'Inventory/Inventory',
    'depends': ['stock', 'product', 'mail', 'di_tag_export'],
    'data': [
        'security/di_stock_arrival_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'views/stock_arrival_views.xml',
        'views/stock_arrival_import_views.xml',
        'views/stock_arrival_divide_views.xml',
        'views/res_config_settings_views.xml',
        'views/stock_arrival_menus.xml',
        'report/report_delivery_document.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'di_stock_arrival/static/src/**/*',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
