from odoo import api, fields, models

class ProductPricelistItem(models.Model):
    _inherit = 'product.pricelist.item'

    def _get_vendor_domain(self):
        if 'supplier_rank' in self.env['res.partner']._fields:
            return [('supplier_rank', '>', 0)]
        return []

    x_vendor_id = fields.Many2many(
        comodel_name='res.partner',
        relation='product_pricelist_item_res_partner_rel',
        column1='pricelist_item_id',
        column2='partner_id',
        string='Fornitore',
        domain=lambda self: self._get_vendor_domain(),
    )
    x_marche_id = fields.Many2many(
        comodel_name='x_marche',
        relation='product_pricelist_item_x_marche_rel',
        column1='pricelist_item_id',
        column2='x_marche_id',
        string='Marca',
    )
    x_stagione_id = fields.Many2many(
        comodel_name='x_stagioni',
        relation='product_pricelist_item_x_stagioni_rel',
        column1='pricelist_item_id',
        column2='x_stagione_id',
        string='Stagione',
    )
    x_reparto_id = fields.Many2many(
        comodel_name='x_reparto',
        relation='product_pricelist_item_x_reparto_rel',
        column1='pricelist_item_id',
        column2='x_reparto_id',
        string='Reparto',
    )

    def _is_applicable_for(self, product, qty_in_product_uom):
        if not super()._is_applicable_for(product, qty_in_product_uom):
            return False
        tmpl = product if product._name == 'product.template' else product.product_tmpl_id
        if self.x_marche_id and tmpl.x_studio_Prodotti2Marca not in self.x_marche_id:
            return False
        if self.x_stagione_id and tmpl.x_studio_prodotti2stagione not in self.x_stagione_id:
            return False
        if self.x_reparto_id and tmpl.x_studio_prodotti2reparto not in self.x_reparto_id:
            return False
        if self.x_vendor_id and not (self.x_vendor_id & tmpl.seller_ids.mapped('partner_id')):
            return False
        return True

    @api.depends('x_vendor_id', 'x_marche_id', 'x_stagione_id', 'x_reparto_id')
    def _compute_name(self):
        super()._compute_name()
        for item in self:
            extras = []
            if item.x_vendor_id:
                extras.append(f"Fornitore: {', '.join(item.x_vendor_id.mapped('display_name'))}")
            if item.x_marche_id:
                extras.append(f"Marca: {', '.join(item.x_marche_id.mapped('display_name'))}")
            if item.x_stagione_id:
                extras.append(f"Stagione: {', '.join(item.x_stagione_id.mapped('display_name'))}")
            if item.x_reparto_id:
                extras.append(f"Reparto: {', '.join(item.x_reparto_id.mapped('display_name'))}")
            if extras:
                item.name = (item.name or '') + ' | ' + ' | '.join(extras)
