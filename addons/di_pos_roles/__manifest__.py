{
    'name': 'POS Roles: Administrator, Cashier, Salesperson',
    'version': '19.0.1.0.0',
    'summary': 'Who may open and close the till and which orders each role sees',
    'description': """
POS Roles
=========
Maps the shop's three roles onto the three tiers Odoo's "Log in with
Employees" already provides, and fills the gaps between what Odoo allows
and how the shops work:

=================  ==============  ======================================
Role               Odoo tier       What changes
=================  ==============  ======================================
Administrator      Advanced        nothing - sees every order, opens and
                                   closes the till
Cashier            Basic           may now CLOSE the till, even when the
                                   cash is off (the difference is
                                   recorded); the Orders list shows TODAY
                                   only
Salesperson        Minimal         may no longer OPEN the till; the Orders
                                   list is hidden entirely
=================  ==============  ======================================

Returns keep working for the cashier: scanning a receipt's QR code loads
that exact order whatever its date, and so does searching by its receipt
or invoice number. Browsing older history - by date, customer or an empty
search - stays limited to today.

Enforcement is at the till, the same level as every role rule Odoo itself
applies: all employees at a register share one Odoo user, so the server
cannot tell a cashier from a salesperson.
""",
    'author': 'Dimensione Immagine',
    'category': 'Point of Sale',
    'depends': ['point_of_sale', 'pos_hr'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'assets': {
        'point_of_sale._assets_pos': [
            'di_pos_roles/static/src/**/*',
        ],
    },
    'license': 'LGPL-3',
}
