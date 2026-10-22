{
    'name': 'Buoni Acquisto',
    'version': '19.0.1.4.0',
    'summary': 'Buoni acquisto per articolo, con fasce di prezzo e regole di idoneità configurabili',
    'description': """
Buoni Acquisto
===============
Custom loyalty-points engine for Dimensione Immagine, built from scratch
rather than on top of the native ``loyalty`` module because points are:

- earned on the order total, but spent **per item**, capped by a
  configurable price bracket table;
- gated by an eligibility rule comparing each item's current discount %
  to a configurable per-category (Reparto) maximum;
- tracked as independent, non-merging batches that each carry their own
  earn date and expiry date (default 30 days), consumed FIFO.

The business logic lives entirely in a POS-agnostic service layer
(``bonus.point.service``) so it can be reused by Sales/eCommerce later
without duplicating rules in the POS frontend.

All thresholds, rates, brackets and durations are configurable records,
never hardcoded constants.
""",
    'author': 'Dimensione Immagine',
    'category': 'Point of Sale',
    'depends': ['point_of_sale', 'product', 'stock'],
    'data': [
        'security/bonus_point_security.xml',
        'security/ir.model.access.csv',
        'data/bonus_point_data.xml',
        'views/bonus_point_settings_views.xml',
        'views/bonus_point_bracket_views.xml',
        'views/bonus_point_category_rule_views.xml',
        'views/bonus_point_batch_views.xml',
        'views/bonus_point_movement_views.xml',
        'views/res_partner_views.xml',
        'views/product_template_views.xml',
        'views/product_product_views.xml',
        'views/bonus_point_menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'assets': {
        'point_of_sale._assets_pos': [
            'di_bonus_points/static/src/**/*',
        ],
    },
    'license': 'LGPL-3',
}
