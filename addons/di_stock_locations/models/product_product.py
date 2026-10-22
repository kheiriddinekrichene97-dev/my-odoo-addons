from odoo import fields, models

from .product_template import INTERNAL_ONLY


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # Deliberately shadows the delegated template field of the same name.
    # product.product _inherits product.template, so without this a variant
    # would show every colour and size of its style - the template's answer to
    # a question asked about one variant. Here it is a real One2many, because
    # stock.quant.product_id is a stored column, unlike product_tmpl_id.
    di_internal_quant_ids = fields.One2many(
        'stock.quant', 'product_id',
        string="Stock by Location",
        domain=INTERNAL_ONLY,
        readonly=True,
        help="Where this variant's stock physically sits, one row per "
             "internal location.")
