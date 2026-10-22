from odoo import api, models


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    @api.model
    def _load_pos_data_fields(self, config):
        """Tell the register which warehouse it is standing in.

        get_product_info_pos already sorts this register's own warehouse to
        the front of its `warehouses` list, but sends nothing that says which
        one it is - and once the popup re-sorts that list by quantity, being
        first no longer identifies it. The id is enough to match against the
        `id` core puts on every warehouse row.

        `stock.warehouse` is not one of the POS's loaded models, so this
        arrives on the frontend as a bare id rather than a linked record
        (relations to unloaded models degrade to their raw value); that is
        exactly what the comparison needs, so the model is deliberately not
        loaded just for this.
        """
        fields = super()._load_pos_data_fields(config)
        return fields + ['warehouse_id']
