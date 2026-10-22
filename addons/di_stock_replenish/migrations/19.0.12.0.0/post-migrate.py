"""Carry the single source over to the several-source field.

19.0.12 replaced ``source_warehouse_id`` (Many2one) with
``source_warehouse_ids`` (Many2many). Odoo leaves a removed field's column in
the table, so every existing comparison's source is still there to copy into
the new relation - without this, every document made before the upgrade
would open with no source and refuse to run.

The old column was NOT NULL. Odoo normally drops that on a removed field
itself; it is dropped here too so new documents, which no longer write the
column, can never trip over it.
"""


def migrate(cr, version):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'di_stock_replenish'
           AND column_name = 'source_warehouse_id'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        ALTER TABLE di_stock_replenish
        ALTER COLUMN source_warehouse_id DROP NOT NULL
    """)
    cr.execute("""
        INSERT INTO di_stock_replenish_source_rel (replenish_id, warehouse_id)
        SELECT id, source_warehouse_id
          FROM di_stock_replenish
         WHERE source_warehouse_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)
