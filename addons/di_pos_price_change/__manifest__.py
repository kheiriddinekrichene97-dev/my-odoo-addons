{
    'name': 'POS Price Changes with a Reason',
    'version': '19.0.1.0.0',
    'summary': 'Allow price and discount changes per shop, and record why each one was made',
    'description': """
POS Price Changes with a Reason
===============================
A per-shop switch, *Allow Price Changes*. When it is off, the Price and %
keys are greyed out at that till for everyone. When it is on, a cashier or
an administrator may change a product's price or apply a discount - and
every change asks for a reason before it is applied. Cancelling the reason
leaves the line exactly as it was.

The salesperson (Minimal rights) never gets either key: Odoo already greys
them out for that role.

Each changed line records its original price and discount, the reason, and
the employee who made the change. Point of Sale > Orders > Price Changes
lists them, by shop, by cashier and by day. Nothing is printed on the
customer's receipt.

A price changed by hand also counts as a discount for bonus points - see
di_bonus_points (isLineBonusEligible / get_max_points_for_line).
""",
    'author': 'Dimensione Immagine',
    'category': 'Point of Sale',
    'depends': ['point_of_sale', 'pos_hr'],
    'data': [
        'views/res_config_settings_views.xml',
        'views/pos_order_line_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'assets': {
        'point_of_sale._assets_pos': [
            'di_pos_price_change/static/src/**/*',
        ],
    },
    'license': 'LGPL-3',
}
