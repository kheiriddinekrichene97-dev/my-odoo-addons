{
    'name': 'POS Stock by Shop',
    'version': '19.0.1.0.0',
    'summary': "Read a product's stock per shop at the register without "
               "counting past the shops that have none",
    'description': """
POS Stock by Shop
=================
The register already knows where every product sits: core's product info
popup lists **all 22 warehouses** with their quantities. The problem is that
it lists all 22 whatever the answer is - a typical garment is in 4 shops, so
the cashier reads 18 lines of "0 Units available, 0 forecasted" to find the
4 that matter, with a customer waiting.

This module re-shapes that one section:

- the shops that actually hold the item, **this register's own shop first**,
  then by quantity;
- a one-line summary above them ("In stock at 4 of 22 shops");
- the empty shops folded behind a toggle rather than deleted - the question
  "does anybody have it" and the question "which shops carry it at all" are
  both real, and a row that was never drawn cannot answer the second.

It also puts a visible info badge on each product card. The gesture itself is
**not** new: core already opens this popup on a 500ms long press
(`useLongPress` in product_screen.js), for mouse and touch alike. Nobody
discovers a hidden gesture on a shop floor, so the badge is a label for
something the register could already do.

Read-only throughout: no cart, pricing, tax or payment code is touched.
""",
    'author': 'Dimensione Immagine',
    'category': 'Point of Sale',
    'depends': ['point_of_sale', 'stock'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'assets': {
        'point_of_sale._assets_pos': [
            'di_pos_stock_info/static/src/**/*',
        ],
    },
    'license': 'LGPL-3',
}
