{
    'name': 'Sales Replenishment (Riassortimento di Vendita)',
    'version': '19.0.1.3.0',
    'summary': 'Restock every shop with what it actually sold, from the two '
               'source warehouses',
    'description': """
Sales Replenishment (Riassortimento di Vendita)
===============================================
The daily job, as the shop floor actually does it: take yesterday's till
receipts, shop by shop, and put the same pieces back on the shelf out of
ING - CONTESSE and MAG - CENTRALE PISTUNINA.

On a Monday the period covers Saturday and Sunday instead - the shops sell on
both and nobody restocks on the Sunday - and the default is worked out from
the day the document is created, so the worker normally changes nothing.

What it does
------------
* Reads the net quantity sold per variant per point of sale over the period.
  Net: a return is a negative line at the till, whether it came through
  Odoo's refund flow or was typed as -1 the way ESC did it, and a returned
  piece is back on the shelf so it does not need replacing.
* Puts the sources' free stock beside it and proposes what to send, capped by
  what is actually there - five sold with three in the warehouse sends three.
* Groups every shop's table by Posizionamento (SCA, then APP, then the rest)
  and then by product category (ABBI first), showing the child category where
  there is one and the category itself where there is not.
* Lets the workers drop a whole Stagione they do not want to move, without
  losing the quantities they have already adjusted.
* Prints the cartellini one pile per source warehouse, in the order that
  warehouse is walked, one label per piece, so the warehouse worker can pick
  straight off the pile.

Confirming generates one internal transfer per source and shop and leaves it
ready rather than validated: the goods have not moved yet when the paperwork
is printed, so the warehouse validates as it picks.
""",
    'author': 'Dimensione Immagine',
    'category': 'Inventory/Inventory',
    'depends': ['stock', 'product', 'point_of_sale', 'mail',
                'di_stock_arrival', 'di_stock_replenish', 'di_tag_export'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'report/pos_replenish_report.xml',
        'report/pos_replenish_report_templates.xml',
        'views/pos_replenish_views.xml',
        'views/pos_replenish_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'di_pos_replenish/static/src/**/*',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
