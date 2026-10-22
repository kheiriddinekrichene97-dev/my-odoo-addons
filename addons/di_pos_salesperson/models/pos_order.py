from odoo import _, fields, models


class PosOrder(models.Model):
    _inherit = 'pos.order'

    # Deliberately no _load_pos_data_fields override: core pos.order returns
    # [] from that method (never overridden in point_of_sale), and Odoo's
    # ORM treats an empty fields list on .read() as "read everything" - that
    # is how the whole order already round-trips to the POS frontend today.
    # Overriding it here to add just this field would flip that to "read
    # ONLY this field", breaking every other order field's sync. A plain
    # field via _inherit is automatically included in both the initial
    # session load and pos.order.read_pos_data's response, same as any
    # other core field (e.g. partner_id).
    salesperson_id = fields.Many2one(
        'hr.employee', string='Salesperson', index='btree_not_null',
        help="Summary of the line-level salespeople: the employee who sold "
             "this order when a single one did, empty when several shared "
             "it. Attribution itself lives on the lines - see the "
             "Salesperson column there.")
    di_salesperson_label = fields.Char(
        string='Salespeople', readonly=True,
        help="Who sold this order, in words: one name, or \"Mixed\" when "
             "the lines were shared between several employees.")

    # A plain stored field filled in by the server rather than a computed
    # one: the till sends the whole order back on every sync (pos.order has
    # no field allow-list), and writing to a computed field without an
    # inverse raises. Recomputing here keeps the summary truthful while
    # leaving the sync a plain write.
    def _di_apply_salesperson_summary(self):
        for order in self:
            people = order.lines.mapped('salesperson_id')
            single = people if len(people) == 1 else self.env['hr.employee']
            order.salesperson_id = single.id or False
            if single:
                order.di_salesperson_label = single.name
            elif people:
                order.di_salesperson_label = _("Mixed")
            else:
                order.di_salesperson_label = False

    def _process_saved_order(self, draft):
        result = super()._process_saved_order(draft)
        self._di_apply_salesperson_summary()
        return result
