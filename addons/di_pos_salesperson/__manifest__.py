{
    'name': 'POS Salesperson Attribution',
    'version': '19.0.1.1.1',
    'summary': 'Optionally attribute a POS order to the employee who assisted the customer, separate from the cashier',
    'description': """
POS Salesperson Attribution
============================
Credits the employee who helped the customer pick a product, separately
from the cashier operating the register. Attribution is recorded **per
line**, because one receipt is often shared: a scarf sold by one assistant
and a dress by another must each count for the right person.

At the till the name is *sticky*. The cashier picks a salesperson once and
everything scanned afterwards is credited to them, until someone changes
it - which is how a shop floor works, an assistant serving a customer for
a stretch and then handing over. Correcting a single product is done by
selecting its line and ticking "only this line" in the same popup, so the
common case stays one tap and the correction is explicit rather than
hidden behind a gesture.

Attribution never blocks a sale: a line may stay untagged. A returned line
is credited back to whoever made the original sale, so commission follows
the goods. The order keeps a read-only summary - one name, or "Mixed".

Eligibility is controlled by a per-employee "Available as Salesperson in
POS" toggle. Which employees show up for a given register still follows
Point of Sale > Employees' existing per-shop employee lists when those
are set, same as the cashier login feature - it does not require "Log in
with employees" to be enabled on that register.

Point of Sale > Sales by Salesperson lists the sold lines grouped by
employee.
""",
    'author': 'Dimensione Immagine',
    'category': 'Point of Sale',
    'depends': ['point_of_sale', 'pos_hr'],
    'data': [
        'views/hr_employee_views.xml',
        'views/pos_order_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'assets': {
        'point_of_sale._assets_pos': [
            'di_pos_salesperson/static/src/**/*',
        ],
    },
    'license': 'LGPL-3',
}
