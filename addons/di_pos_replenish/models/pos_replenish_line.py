from odoo import api, fields, models


class PosReplenishLine(models.Model):
    _name = 'di.pos.replenish.line'
    _description = "Sales Replenishment Line"
    # sequence carries the reading order the run was built in - posizionamento,
    # then category, then barcode with each product code kept together. None of
    # that can be expressed as an ORM order (a barcode has to be compared as a
    # number, and a code's block sits at its own lowest barcode), so it is
    # worked out once in Python when the run is built and stored here.
    _order = 'config_id, sequence, id'
    _rec_name = 'product_id'
    _rec_names_search = ['product_id']

    replenish_id = fields.Many2one(
        'di.pos.replenish', string="Replenishment", required=True,
        ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='replenish_id.company_id', store=True, index=True)
    state = fields.Selection(related='replenish_id.state', store=True)
    sequence = fields.Integer(string="Sequence", default=0, index=True)

    config_id = fields.Many2one(
        'pos.config', string="Point of Sale", required=True, index=True,
        ondelete='cascade')
    warehouse_id = fields.Many2one(
        related='config_id.warehouse_id', store=True,
        string="Destination Warehouse")
    product_id = fields.Many2one(
        'product.product', string="Product", required=True, index=True,
        ondelete='cascade')
    default_code = fields.Char(
        related='product_id.default_code', string="Internal Reference")
    barcode = fields.Char(related='product_id.barcode', string="Barcode")

    # What the till did. Sold and returned are kept apart rather than stored
    # as one signed number so a worker can see why a row asks for less than
    # they remember selling.
    sold_qty = fields.Float(
        string="Sold", digits='Product Unit', readonly=True)
    returned_qty = fields.Float(
        string="Returned", digits='Product Unit', readonly=True,
        help="Pieces that came back over the period, as a positive count. A "
             "return is a negative line at the till, whether it went through "
             "Odoo's refund flow or was typed as -1.")
    net_sold_qty = fields.Float(
        string="To Replace", digits='Product Unit', readonly=True,
        help="Sold less returned: a returned piece is back on the shop's own "
             "shelf, so it does not need replacing.")

    allocation_ids = fields.One2many(
        'di.pos.replenish.allocation', 'line_id', string="Sources")
    qty_to_send = fields.Float(
        string="To Send", compute='_compute_qty_to_send', store=True,
        digits='Product Unit')
    shortfall = fields.Float(
        string="Short", compute='_compute_qty_to_send', store=True,
        digits='Product Unit',
        help="Sold more than the source warehouses can replace. The shop "
             "stays short by this much.")

    # Taxonomy snapshots. Stored as plain values rather than relations to the
    # Studio models: this module has to install on a database where Studio
    # never made them, and a run is a record of what was true that morning
    # anyway - reclassifying a product later must not rewrite last week's
    # paperwork. See [[studio-taxonomy-fields]].
    season_key = fields.Integer(string="Season Id", readonly=True, index=True)
    season_name = fields.Char(string="Season", readonly=True)
    position_name = fields.Char(string="Positioning", readonly=True)
    category_root = fields.Char(
        string="Main Category", readonly=True,
        help="The top of the category tree the product sits under - ABBI, "
             "ACCE and so on. The table is grouped by this first, then by "
             "posizionamento inside it.")
    category_label = fields.Char(
        string="Category", readonly=True,
        help="The child category where the product has one, the category "
             "itself where it does not.")

    is_excluded = fields.Boolean(
        string="Dropped", compute='_compute_is_excluded',
        help="Its season was dropped from this run, so it is not printed and "
             "not transferred.")

    _product_config_uniq = models.Constraint(
        'unique(replenish_id, config_id, product_id)',
        "This product is already on this run for that point of sale.",
    )

    @api.depends('allocation_ids.quantity', 'net_sold_qty')
    def _compute_qty_to_send(self):
        for line in self:
            line.qty_to_send = sum(line.allocation_ids.mapped('quantity'))
            line.shortfall = max(line.net_sold_qty - line.qty_to_send, 0.0)

    @api.depends('season_key', 'replenish_id.excluded_season_keys')
    def _compute_is_excluded(self):
        # Grouped by document so the key set is parsed once per run rather
        # than once per row.
        for sheet, lines in self.grouped('replenish_id').items():
            excluded = sheet._excluded_seasons() if sheet else set()
            for line in lines:
                line.is_excluded = line.season_key in excluded

    @api.depends('config_id', 'product_id')
    def _compute_display_name(self):
        for line in self:
            line.display_name = '%s - %s' % (
                line.config_id.name or '', line.product_id.display_name or '')
