{
    "name": "POS Customer Account (Settle Due)",
    "version": "19.0.1.0.0",
    "summary": "Customer balances at the till: settle debts and invoices, take deposits",
    "description": """
Community counterpart of the Enterprise pos_settle_due, for the shops that sell
on "Customer Account" (pay later):

- the till's customer list shows what each customer owes (or has in credit),
  refreshed every time it opens and after every sale that moves a balance;
- Settle debt: the customer pays what they owe from earlier Customer Account sales;
- Settle invoices: the customer pays open invoices at the till;
- Deposit money: the customer pays in advance and spends it later with
  Customer Account.

A settlement is an ordinary POS order: one information line at 0, the money
taken in (cash/card) and the same amount as a NEGATIVE Customer Account
payment. Closing the session therefore books the money in and credits the
customer's receivable with Odoo's own accounting; this module then reconciles
that credit with the debts or invoices it pays.

The live balance refresh that di_pos_due_sync adds on top of pos_settle_due
is built in, so di_pos_due_sync is not needed with this module.
""",
    "author": "Dimensione Immagine",
    "category": "Point of Sale",
    "depends": ["point_of_sale", "account"],
    "data": [
        "data/product_data.xml",
        "views/pos_order_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "di_pos_customer_account/static/src/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
