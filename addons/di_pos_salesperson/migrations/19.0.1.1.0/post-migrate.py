"""Carry the old order-level attribution down onto the lines.

Before this version a whole receipt carried one salesperson. Reporting now
reads the line, so without this the orders tagged under the old scheme would
vanish from every per-salesperson figure.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE pos_order_line line
           SET salesperson_id = ord.salesperson_id
          FROM pos_order ord
         WHERE line.order_id = ord.id
           AND ord.salesperson_id IS NOT NULL
           AND line.salesperson_id IS NULL
    """)
    # The summary column is filled by the server on every save; seed it for
    # orders that will not be saved again.
    cr.execute("""
        UPDATE pos_order ord
           SET di_salesperson_label = emp.name
          FROM hr_employee emp
         WHERE ord.salesperson_id = emp.id
           AND ord.di_salesperson_label IS NULL
    """)
