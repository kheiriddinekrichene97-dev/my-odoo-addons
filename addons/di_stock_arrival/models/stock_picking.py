from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    di_arrival_id = fields.Many2one(
        'di.stock.arrival', string="Arrival", copy=False, index='btree_not_null',
        ondelete='set null',
        help="Arrival document this transfer was generated from.")

    def _get_report_lang(self):
        """Print warehouse paperwork in the language of whoever prints it.

        Core answers with the partner's language, which is right for a
        delivery note that ends up in a customer's hands. A goods receipt
        note - and the transfer note for one of our own shops - is read in
        the warehouse instead; the supplier never sees it. Suppliers
        imported from ESC carry lang 'en_US', so an Italian warehouse was
        getting an English receipt note for every arrival.
        """
        if self.picking_type_id.code in ('incoming', 'internal'):
            return self.env.lang or self.env.user.lang
        return super()._get_report_lang()
