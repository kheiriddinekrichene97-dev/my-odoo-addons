{
    'name': 'Rimborso con Buono',
    'version': '19.0.1.0.0',
    'summary': 'Un reso si salda emettendo un buono acquisto, non restituendo denaro',
    'description': """
Rimborso con Buono
==================
Adds a way of settling a refund that costs the shop nothing: instead of
handing money back, the till issues a gift card for the same amount.

It works by adding a **payment method**, not a button. The point of sale
draws its payment list from pos.payment.method records, so marking a
method "issues a gift card" makes it appear beside Cassa and Carta with
no interface code at all - and the cashier's flow is the stock one:

    Refund -> pick the products -> payment screen -> Rimborso con Buono

Tapping it books the negative amount against whichever journal that
method uses, so the order balances and can be validated. On validation
the order issues a loyalty.card worth the refunded amount to the
customer on the order.

Which journal, and therefore which account the liability lands in, is a
configuration choice - a gift card is money owed to a customer, and the
accountant should choose where that sits.

On a refund, the other payment methods refuse to be used: money is not
given back, so the only way to close the order is the voucher.
""",
    'author': 'Dimensione Immagine',
    'category': 'Point of Sale',
    'depends': ['point_of_sale', 'pos_loyalty'],
    'data': [
        'views/pos_payment_method_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'assets': {
        'point_of_sale._assets_pos': [
            'di_pos_refund_voucher/static/src/**/*',
        ],
    },
    'license': 'LGPL-3',
}
