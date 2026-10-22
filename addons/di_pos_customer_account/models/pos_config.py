from odoo import api, models


class PosConfig(models.Model):
    _inherit = "pos.config"

    def _get_special_products(self):
        """Always loaded at the till, and hidden from the product list."""
        product = self.env.ref("di_pos_customer_account.product_customer_account", raise_if_not_found=False)
        return super()._get_special_products() | (product or self.env["product.product"])

    @api.model
    def _load_pos_data_read(self, records, config):
        read_records = super()._load_pos_data_read(records, config)
        if read_records:
            product = self.env.ref("di_pos_customer_account.product_customer_account", raise_if_not_found=False)
            read_records[0]["_di_customer_account_product_id"] = product.id if product else False
        return read_records
