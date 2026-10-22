{
    'name': 'POS Settle Due - Live Refresh',
    'version': '19.0.1.0.0',
    'summary': "Refresh a customer's due balance in an already-open POS session, without reopening the register",
    'description': """
POS Settle Due - Live Refresh
===============================
``pos_settle_due`` offers "Settle orders" / "Settle invoices" on a customer
based on ``partner.total_due`` / ``.pos_orders_amount_due`` /
``.invoices_amount_due``. Those are not ordinary synced fields - they are
written onto the client-side partner record by a ``get_all_total_due`` RPC,
called only at POS session boot and on a server-side customer search.

So a debt taken on during a session (paying part cash, part "Customer
Account"), or one just paid off, leaves those values stale: the cashier gets
"Deposit money" instead of "Settle orders", or still sees a balance that has
already been settled - until the register is closed and reopened.

``pos_settle_due`` already ships the method that fixes this
(``refreshTotalDueOfPartner``); it just never calls it after a sale. This
module calls it on ``postSyncAllOrders``, right after an order syncs, so the
customer's due totals stay correct for the rest of the session.
""",
    'author': 'Dimensione Immagine',
    'category': 'Point of Sale',
    'depends': ['point_of_sale', 'pos_settle_due'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'assets': {
        'point_of_sale._assets_pos': [
            'di_pos_due_sync/static/src/**/*',
        ],
    },
    'license': 'LGPL-3',
}
