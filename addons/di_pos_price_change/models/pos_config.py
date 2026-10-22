from odoo import fields, models


class PosConfig(models.Model):
    _inherit = 'pos.config'

    # pos.config has no _load_pos_data_fields override, so every field -
    # this one included - reaches the till without listing it anywhere.
    di_allow_price_change = fields.Boolean(
        string="Allow Price Changes",
        help="When on, a cashier or an administrator may change a product's "
             "price or apply a discount at this till, and must give a reason "
             "for each change. When off, the Price and % keys are greyed out "
             "here for everyone.")
