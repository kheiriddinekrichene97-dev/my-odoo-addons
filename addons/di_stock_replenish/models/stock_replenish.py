import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# The season lives on product.template as a Studio many2one. It is referenced
# by name rather than declared as a relation: a Many2one('x_stagioni') would
# stop this module installing on any database without that Studio model, and
# the taxonomy fields are hand-made - see di_stock_arrival's importer, which
# looks them up the same defensive way.
SEASON_FIELD = 'x_studio_prodotti2stagione'
SEASON_MODEL = 'x_stagioni'
BRAND_FIELD = 'x_studio_Prodotti2Marca'
BRAND_MODEL = 'x_marche'
# Posizionamento: no x_studio_ prefix, unlike the other two - it was made by
# hand. Same defensive lookup as the season.
POSITION_FIELD = 'x_prodotti2posizionamento'
POSITION_MODEL = 'x_posizionamenti'
# The only two posizionamenti the buyers work with. x_posizionamenti also
# carries three dozen leftovers from the old software's import ("5-5 2BA",
# "DOC N 20 RFD" ...) that nobody filters on, so the picker offers exactly
# these, in this order, and nothing else.
POSITION_ORDER = ('APP', 'SCA')

# The promo price shown beside each row. Matched by name because the
# pricelists are customer records, not module data - the same lookup, and the
# same "Promo (EUR)" fallback, that di_stock_arrival's importer writes with.
# "Promo Franchising" must not answer to "Promo", hence the exact comparison.
PROMO_PRICELIST = 'Promo'

# The two axes the variant breakdown is read along - the same real
# product.attribute records di_tag_export prints and di_stock_locations grids.
COLOUR_ATTRIBUTE = 'Colore'
SIZE_ATTRIBUTE = 'Taglia'

# The cartellino report lives in di_tag_export, which owns the layout, the
# paper format and the pricelists it prints. This module only decides which
# variants go into it.
TAG_REPORT = 'di_tag_export.action_report_product_tag'

# A tag per piece multiplies quickly. Keep the PDF small enough that Odoo and
# the thermal printer can render it rather than silently timing out.
TAG_PRINT_LIMIT = 1000

# Reading a shop's shelf is cheap; reading the whole catalogue is not. Past
# this many rows the grid is unusable anyway, so the report says so instead of
# locking up the browser.
ROW_LIMIT = 500


class StockReplenish(models.Model):
    _name = 'di.stock.replenish'
    # mail.thread is what makes tracking=True on state mean anything: without
    # it Odoo logs "unknown parameter 'tracking'" and silently records nothing.
    # A document that moves stock deserves the audit trail anyway.
    _inherit = ['mail.thread']
    _description = "Stock Replenishment Comparison"
    _order = 'create_date desc, id desc'
    _rec_name = 'display_name'

    name = fields.Char(
        string="Reference", required=True, copy=False, readonly=True,
        default=lambda self: _("New"))
    date = fields.Datetime(
        string="Date", required=True, default=fields.Datetime.now)
    # 'done' is kept as the key so nothing stored changes; its meaning is
    # "the transfers exist", not "the stock has moved" - see _process_picking.
    state = fields.Selection(
        [('draft', "Draft"),
         ('done', "Transfers Generated"),
         ('cancel', "Cancelled")],
        string="Status", default='draft', required=True,
        copy=False, tracking=True)

    company_id = fields.Many2one(
        'res.company', string="Company", required=True, index=True,
        default=lambda self: self.env.company)
    # Several sources, so one comparison can read both stockrooms at once
    # instead of the worker running it twice. A variant sits in one of them
    # on nearly the whole catalogue (di_pos_replenish counted 90 of some nine
    # thousand in both), so the destination cells stay one number each and
    # confirming fills them from the sources in this field's order - see
    # _allocate_sources.
    source_warehouse_ids = fields.Many2many(
        'stock.warehouse', relation='di_stock_replenish_source_rel',
        column1='replenish_id', column2='warehouse_id',
        string="Source", required=True, check_company=True,
        default=lambda self: self._default_source_warehouse(),
        help="Warehouses the stock would be taken from. Defaults to the "
             "arrival warehouse configured in Inventory settings. With more "
             "than one, each quantity is taken from the first that has it, "
             "then the next.")
    destination_warehouse_ids = fields.Many2many(
        'stock.warehouse', string="Destinations",
        help="Shops and stockrooms to compare against the source. Pick as "
             "many as you want to look at together.")

    # Seasons are chosen with the chip picker in the grid, not a relation:
    # x_stagioni is a Studio model, and a Many2many to it would stop this
    # module installing on any database without the taxonomy. The ids are
    # kept as text and resolved when the comparison runs.
    season_keys = fields.Char(
        string="Seasons",
        help="The seasons picked in the grid, as ids. Edited through the "
             "season chips rather than typed.")
    # Posizionamenti: chips over ids like the seasons, for the same reason -
    # x_posizionamenti is a Studio model, and a relation to it would stop
    # this module installing where it does not exist.
    position_keys = fields.Char(
        string="Positioning",
        help="The posizionamenti picked in the grid, as ids. Edited through "
             "the chips rather than typed.")
    # Brands, unlike seasons, ARE a relation: it buys the same autocomplete
    # widget Product Categories has, which is what 379 brands need. The cost
    # is deliberate and worth naming - x_marche is a Studio model, so this
    # module now requires it to install at all, where before it degraded to
    # hiding the picker. pricelist_extend already binds the same model the
    # same way. See [[studio-taxonomy-fields]] before adding a third.
    brand_ids = fields.Many2many(
        comodel_name=BRAND_MODEL,
        relation='di_stock_replenish_x_marche_rel',
        column1='replenish_id', column2='x_marche_id',
        string="Marca",
        help="Narrow the comparison to these brands.")
    # Suppliers are a plain res.partner relation, so unlike the brands this
    # one costs nothing in portability - res.partner is always there. The
    # domain keeps the autocomplete to the 277 partners actually flagged as
    # vendors rather than the whole address book.
    supplier_ids = fields.Many2many(
        'res.partner', relation='di_stock_replenish_supplier_rel',
        column1='replenish_id', column2='partner_id',
        string="Supplier", domain=[('supplier_rank', '>', 0)],
        help="Narrow the comparison to products bought from these "
             "suppliers. A product matches on any of its suppliers, while "
             "the Supplier column shows the preferred one - the two differ "
             "on the handful of products bought from more than one vendor.")
    categ_ids = fields.Many2many(
        'product.category', string="Product Categories",
        help="Narrow the comparison to these product categories. Child "
             "categories are included.")
    # Four ways to read the same comparison, not a tick: "none left", "gone
    # below zero" and "getting low" are different questions. A destination at
    # 0 has run out; one below 0 has sold stock the system did not know it
    # had, which is a counting error as much as a gap; one at 2 of a size
    # that sells is worth a top-up before it becomes either. They are all the
    # same test with the line drawn in a different place - see is_gap in
    # get_comparison_matrix - so they share one selector rather than becoming
    # three filters a worker could combine into nonsense. They do NOT share a
    # quantifier: the first three ask whether ANY destination is under the
    # line, 'below' whether EVERY one is. The reasoning is at the filter
    # itself. 'missing' is the default because it is the question the screen
    # exists to answer.
    missing_mode = fields.Selection(
        [('all', "All"),
         ('missing', "Only Missing"),
         ('negative', "Only Negative"),
         ('below', "Below a Quantity")],
        string="Show", default='missing', required=True,
        help="Which rows to keep. Only Missing keeps the products at least "
             "one destination has none of. Only Negative keeps just the "
             "destinations that have gone below zero - stock sold that was "
             "never counted in. Below a Quantity keeps only what every "
             "destination is thin on. All leaves the comparison whole.")
    # Whole pieces, so an integer: the worker types "5" and means five
    # garments, and a Float on 'Product Unit' would show them 5.00 to type
    # over. The quantities it is compared against are floats all the same,
    # and are rounded to the unit's precision before the comparison.
    below_quantity = fields.Integer(
        string="Below", default=5,
        help="With Show set to Below a Quantity, keep the products EVERY "
             "destination has fewer than this many of - one well stocked "
             "shop is enough to drop the row. The number itself is not "
             "included: 5 keeps 4, 3, 2, 1, 0 and anything negative.")
    # Picked in the grid, like the season chips - the option list itself has
    # to be computed fresh on every call (it depends on the Promo pricelist,
    # which can change independently of this document), so a stored relation
    # or Selection with a fixed list of values would go stale the same way a
    # hardcoded one would. Stored as the bucket's own number, as text: '' is
    # "Tutti" (no filter), '0' is "Nessuno" (on the Promo pricelist at no
    # actual discount), anything else is that rounded percentage.
    discount_bucket = fields.Char(
        string="Discount %",
        help="Narrow the comparison to products discounted by roughly this "
             "much off Sales Price, on the Promo pricelist. Rounded to the "
             "nearest 10% - real promo prices land close to a round number "
             "but rarely exactly on one.")
    # Same shape as discount_bucket: picked in the grid, options computed
    # fresh from what the comparison currently holds, stored as text. '' is
    # "Tutti", anything else is the Promo price to narrow to, as '9.95'.
    price_bucket = fields.Char(
        string="Promo Price",
        help="Narrow the comparison to products at exactly this price on "
             "the Promo pricelist - 4.95, 9.95, 19.95 and so on. A product "
             "with no Promo rule has no promo price and is left out once a "
             "price is picked.")

    line_ids = fields.One2many(
        'di.stock.replenish.line', 'replenish_id', string="To Transfer",
        copy=False)
    line_count = fields.Integer(compute='_compute_totals', store=True)
    total_quantity = fields.Float(
        compute='_compute_totals', store=True, string="Total Quantity",
        digits='Product Unit')
    picking_ids = fields.One2many(
        'stock.picking', 'di_replenish_id', string="Transfers", copy=False)
    picking_count = fields.Integer(compute='_compute_picking_count')

    # Anchor for the comparison grid. It stores nothing: the grid reads
    # through get_comparison_matrix() so a report of several hundred rows is
    # never written to the database.
    comparison = fields.Char(string="Comparison", readonly=True)

    # ------------------------------------------------------------------
    # Defaults and naming
    # ------------------------------------------------------------------

    @api.model
    def _default_source_warehouse(self):
        # di_stock_arrival owns this setting, and this module depends on it.
        return self.env.company._di_get_arrival_warehouse()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _("New"):
                vals['name'] = self._next_free_reference(
                    vals.get('company_id', self.env.company.id))
        return super().create(vals_list)

    @api.model
    def _next_free_reference(self, company_id):
        """The next reference the sequence gives that no document holds yet.

        Same guard as di.stock.arrival's, for the same reason: a database
        rebuilt from a backup keeps the documents but can carry an older
        sequence counter, and this model has no unique constraint to stop
        it - the next save would quietly repeat a reference that is already
        on another document. Draw again until the number is free; bounded.
        """
        Sequence = self.env['ir.sequence'].with_company(company_id)
        taken = self.sudo().with_context(active_test=False)
        for _attempt in range(1000):
            name = Sequence.next_by_code('di.stock.replenish')
            if not name:
                return _("New")
            if not taken.search_count([
                    ('name', '=', name), ('company_id', '=', company_id)]):
                return name
        raise UserError(_(
            "Could not find a free reference: every number the sequence "
            "gives is already used. Check the replenishment sequence under "
            "Settings > Technical > Sequences."))

    @api.depends('line_ids.quantity')
    def _compute_totals(self):
        for sheet in self:
            sheet.line_count = len(sheet.line_ids)
            sheet.total_quantity = sum(sheet.line_ids.mapped('quantity'))

    def _compute_picking_count(self):
        for sheet in self:
            sheet.picking_count = len(sheet.picking_ids)

    @api.constrains('source_warehouse_ids', 'destination_warehouse_ids')
    def _check_source_not_destination(self):
        for sheet in self:
            both = sheet.source_warehouse_ids & sheet.destination_warehouse_ids
            if both:
                raise ValidationError(_(
                    "%s cannot be a source and a destination at once.",
                    ', '.join(both.mapped('display_name'))))

    @api.ondelete(at_uninstall=False)
    def _unlink_only_draft(self):
        for sheet in self:
            if sheet.state == 'done':
                raise UserError(_(
                    "%s has already generated transfers, so it cannot be "
                    "deleted. Cancel it instead.", sheet.name))

    # ------------------------------------------------------------------
    # Season, looked up rather than related
    # ------------------------------------------------------------------
    # The season picker stays chips over ids: there are 77 seasons and they
    # are picked constantly, so they belong above the grid rather than in the
    # form. Brands went the other way - see brand_ids.

    @api.model
    def _season_model(self):
        """The Studio season model, or None where it was never created."""
        if SEASON_FIELD not in self.env['product.template']._fields:
            return None
        if SEASON_MODEL not in self.env:
            return None
        return self.env[SEASON_MODEL].sudo()

    def _available_seasons(self):
        """Every season the picker can offer, in name order."""
        model = self._season_model()
        if model is None:
            return []
        return [
            {'id': record.id, 'name': record.x_name}
            for record in model.search([], order='x_name')
            if record.x_name
        ]

    def _selected_seasons(self):
        """The picked season ids, tolerant of anything stale in the field."""
        ids = []
        for chunk in (self.season_keys or '').split(','):
            chunk = chunk.strip()
            if chunk.isdigit():
                ids.append(int(chunk))
        return ids

    # ------------------------------------------------------------------
    # Posizionamento, looked up the same way
    # ------------------------------------------------------------------

    @api.model
    def _position_model(self):
        """The Studio posizionamento model, or None where it never existed."""
        if POSITION_FIELD not in self.env['product.template']._fields:
            return None
        if POSITION_MODEL not in self.env:
            return None
        return self.env[POSITION_MODEL].sudo()

    def _available_positions(self):
        """The posizionamenti the picker offers: APP and SCA, in that order,
        and only those that actually exist on this database."""
        model = self._position_model()
        if model is None:
            return []
        by_name = {
            (r.x_name or '').strip().upper(): r
            for r in model.search([]) if r.x_name
        }
        return [
            {'id': by_name[name].id, 'name': by_name[name].x_name}
            for name in POSITION_ORDER if name in by_name
        ]

    def _selected_positions(self):
        """The picked posizionamento ids, tolerant of anything stale."""
        ids = []
        for chunk in (self.position_keys or '').split(','):
            chunk = chunk.strip()
            if chunk.isdigit():
                ids.append(int(chunk))
        return ids

    # ------------------------------------------------------------------
    # Product detail shown beside each row
    # ------------------------------------------------------------------

    def _promo_pricelist(self):
        Pricelist = self.env['product.pricelist']
        company = [('company_id', 'in', (False, self.company_id.id))]
        found = Pricelist.search(
            [('name', '=', PROMO_PRICELIST)] + company, limit=1)
        if not found:
            candidates = Pricelist.search(
                [('name', '=like', f'{PROMO_PRICELIST}%')] + company)
            found = candidates.filtered(
                lambda p: p.name.split('(')[0].strip().lower()
                == PROMO_PRICELIST.lower())[:1]
        if not found:
            _logger.info(
                "Pricelist %r not found; the promo column stays empty.",
                PROMO_PRICELIST)
        return found

    def _promo_prices(self, products):
        """``{product id: promo price}``, template rules then variant rules.

        Two queries for the whole page rather than a price computation per
        row: this is a report, and five hundred rows of pricelist resolution
        would be five hundred round trips.
        """
        pricelist = self._promo_pricelist()
        if not pricelist:
            return {}
        Item = self.env['product.pricelist.item']
        base = [('pricelist_id', '=', pricelist.id),
                ('compute_price', '=', 'fixed')]

        by_template = {
            item.product_tmpl_id.id: item.fixed_price
            for item in Item.search(base + [
                ('applied_on', '=', '1_product'),
                ('product_tmpl_id', 'in', products.product_tmpl_id.ids),
            ])
        }
        # A rule on the variant itself is more specific and wins.
        by_variant = {
            item.product_id.id: item.fixed_price
            for item in Item.search(base + [
                ('applied_on', '=', '0_product_variant'),
                ('product_id', 'in', products.ids),
            ])
        }
        return {
            product.id: by_variant.get(
                product.id, by_template.get(product.product_tmpl_id.id, 0.0))
            for product in products
        }

    def _discount_buckets(self, products):
        """``{product id: rounded discount %}``, for products with a Promo
        price at all. `_promo_prices` returns 0.0 for a product with no rule
        on the Promo pricelist at all - the same convention the grid's own
        Promo column already relies on to decide whether to show a price
        (see ``priceDisplay`` in replenish_matrix.js) - so 0.0 here means
        "not on the list", not "on it at no discount", and is excluded
        rather than miscounted as a 100% discount (which a naive
        1 - 0/list_price calculation would otherwise produce).

        Rounded to the nearest 10%: real promo prices land close to a round
        number (a 245-product cluster sits within a point of exactly 30% off,
        for one) but very rarely exactly on it, so an unrounded comparison
        would scatter one intended discount tier across a dozen decimal
        values nobody picked on purpose.
        """
        promo = self._promo_prices(products)
        buckets = {}
        for product in products:
            promo_price = promo.get(product.id) or 0.0
            list_price = product.lst_price or 0.0
            if not promo_price or not list_price:
                continue
            pct = (1 - promo_price / list_price) * 100
            buckets[product.id] = int(round(pct / 10) * 10)
        return buckets

    def _available_discount_buckets(self, products):
        """The discount picker's own options: every bucket actually present
        among ``products``, each labelled - not a fixed list, so it never
        offers a tier nothing in the current comparison is at."""
        buckets = self._discount_buckets(products)
        return [
            {'value': str(value), 'label': self._discount_bucket_label(value)}
            for value in sorted(set(buckets.values()))
        ]

    @api.model
    def _discount_bucket_label(self, value):
        return _("None") if value == 0 else "%d%%" % value

    def _selected_discount_bucket(self):
        """The picked bucket as an int, or None for "Tutti" (no filter)."""
        stored = (self.discount_bucket or '').strip()
        if stored.lstrip('-').isdigit():
            return int(stored)
        return None

    @staticmethod
    def _price_key(value):
        """A price as the picker's own value: '19.95', never 19.949999."""
        return '%.2f' % (value or 0.0)

    def _available_prices(self, promo):
        """The price picker's options: every Promo price actually present in
        ``promo`` (``{product id: promo price}``, as _promo_prices returns
        it), ascending - not a fixed list, for the same reason the discount
        picker's is not. 0.0 there means "no rule on the Promo list", the
        convention the grid's own Promo column relies on, so it is not a
        price to offer."""
        prices = sorted({
            round(price, 2) for price in promo.values() if price})
        return [
            {'value': self._price_key(price), 'label': self._price_key(price)}
            for price in prices
        ]

    def _selected_price(self):
        """The picked price as the picker value, or None for "Tutti"."""
        stored = (self.price_bucket or '').strip()
        try:
            return self._price_key(float(stored)) if stored else None
        except ValueError:
            return None

    def _seasons(self, products):
        """``{product id: season name}``, empty where Studio never made it."""
        if SEASON_FIELD not in self.env['product.template']._fields:
            return {}
        seasons = {}
        for product in products:
            record = product.product_tmpl_id[SEASON_FIELD]
            seasons[product.id] = record.x_name if record else ''
        return seasons

    def _positions(self, products):
        """``{product id: posizionamento}``, empty where Studio never made it."""
        if POSITION_FIELD not in self.env['product.template']._fields:
            return {}
        positions = {}
        for product in products:
            record = product.product_tmpl_id[POSITION_FIELD]
            positions[product.id] = record.x_name if record else ''
        return positions

    def _brands(self, products):
        """``{product id: brand name}``, empty where Studio never made it."""
        if BRAND_FIELD not in self.env['product.template']._fields:
            return {}
        brands = {}
        for product in products:
            record = product.product_tmpl_id[BRAND_FIELD]
            brands[product.id] = record.x_name if record else ''
        return brands

    @staticmethod
    def _supplier(product):
        """The first supplier on the product, which is the preferred one.

        ``seller_ids`` is ordered by sequence, so the first is the one the
        buyer actually uses.
        """
        seller = product.seller_ids[:1]
        return seller.partner_id.display_name if seller else ''

    # ------------------------------------------------------------------
    # Stock reading
    # ------------------------------------------------------------------

    def _product_domain(self):
        """Filters to apply to the products, expressed from stock.quant."""
        domain = []
        if self.categ_ids:
            domain.append(
                ('product_id.categ_id', 'child_of', self.categ_ids.ids))
        seasons = self._selected_seasons()
        if seasons and self._season_model() is not None:
            domain.append((
                f'product_id.product_tmpl_id.{SEASON_FIELD}', 'in', seasons))
        # Brands narrow on top of the seasons rather than beside them: several
        # values of one kind widen the search, but the two kinds together are
        # read as "this brand AND this season", which is how a worker asks it.
        if self.brand_ids and BRAND_FIELD in self.env['product.template']._fields:
            domain.append((
                f'product_id.product_tmpl_id.{BRAND_FIELD}', 'in',
                self.brand_ids.ids))
        positions = self._selected_positions()
        if positions and self._position_model() is not None:
            domain.append((
                f'product_id.product_tmpl_id.{POSITION_FIELD}', 'in',
                positions))
        # Suppliers narrow alongside the rest. seller_ids is reachable from
        # product.product through the _inherits delegation to the template,
        # so this is one join rather than a pre-pass over the catalogue.
        if self.supplier_ids:
            domain.append((
                'product_id.seller_ids.partner_id', 'in',
                self.supplier_ids.ids))
        return domain

    def _warehouse_stock(self, warehouse, product_ids=None):
        """``{product id: (on hand, reserved)}`` for one warehouse.

        One grouped query per warehouse. ``available_quantity`` is computed
        and not stored, so it cannot be aggregated - the free quantity is
        derived from the two stored columns instead.
        """
        domain = [
            ('location_id', 'child_of', warehouse.view_location_id.id),
            ('location_id.usage', '=', 'internal'),
            ('company_id', '=', self.company_id.id),
        ]
        if product_ids is None:
            domain += self._product_domain()
        else:
            if not product_ids:
                return {}
            domain.append(('product_id', 'in', list(product_ids)))

        rows = self.env['stock.quant']._read_group(
            domain,
            groupby=['product_id'],
            aggregates=['quantity:sum', 'reserved_quantity:sum'],
        )
        return {
            product.id: (quantity or 0.0, reserved or 0.0)
            for product, quantity, reserved in rows
        }

    def _source_free(self, product_ids=None):
        """``{warehouse id: {product id: free quantity}}``, one entry per
        source and only the products it actually has free.

        Free, not on hand, at every source: stock already reserved for
        another transfer cannot be sent anywhere. ``product_ids`` is passed
        straight to _warehouse_stock, so ``None`` means "apply the filters".
        """
        self.ensure_one()
        result = {}
        for warehouse in self.source_warehouse_ids:
            result[warehouse.id] = {
                product_id: quantity - reserved
                for product_id, (quantity, reserved)
                in self._warehouse_stock(warehouse, product_ids).items()
                if quantity - reserved > 0
            }
        return result

    @staticmethod
    def _total_free(by_source):
        """What every source can give together, per product."""
        total = {}
        for stock in by_source.values():
            for product_id, quantity in stock.items():
                total[product_id] = total.get(product_id, 0.0) + quantity
        return total

    def _source_names(self):
        return ', '.join(self.source_warehouse_ids.mapped('display_name'))

    # ------------------------------------------------------------------
    # The comparison
    # ------------------------------------------------------------------

    def _core_label(self, model, field_name, default):
        """Borrow Odoo's own translated label for a field this column shows."""
        model_obj = self.env.get(model)
        if model_obj is not None and field_name in model_obj._fields:
            described = model_obj.fields_get([field_name], ['string'])
            label = (described.get(field_name) or {}).get('string')
            if label:
                return label
        return default

    def _tag_report(self):
        return self.env.ref(TAG_REPORT, raise_if_not_found=False)

    def action_print_tags(self, product_ids):
        """Print the cartellino for the variants ticked in the grid.

        The grid holds the ticks, so the ids arrive from the client rather
        than from a selection on the record. They are re-browsed and filtered
        through ``exists`` before anything is printed.
        """
        self.ensure_one()
        products = self.env['product.product'].browse(
            [int(pid) for pid in (product_ids or [])]).exists()
        if not products:
            raise UserError(_("Tick at least one product to print its tag."))
        report = self._tag_report()
        if not report:
            raise UserError(_(
                "The Cartellino report is not available on this database."))
        # One pile in di_tag_export's shared order: the source's own, when
        # the document has just one; the general order when it has several,
        # since a tick says nothing about which source the label is for.
        sources = self.source_warehouse_ids
        return report.report_action(products, data={
            'tag_lines': self.env['product.product']._di_tag_lines([(
                False, sources if len(sources) == 1 else False,
                [(product, 1) for product in products],
            )]),
        })

    @api.model
    def _matrix_labels(self):
        """Column headings, taken from the fields the columns actually show.

        Every one of these columns *is* a core field, so borrowing its label
        keeps the grid speaking the same words as the rest of Inventory, in
        whatever language the reader has - a field label is a database
        translation and is never at the mercy of this module's own catalogue.

        _() stays as the fallback for a database where a field is absent. It
        does work: the earlier note here blamed _() for headings that came
        out English, but the real cause was i18n/it.po carrying no
        "#. odoo-python" / "#. odoo-javascript" comments, which is exactly
        what CodeTranslations filters code translations on - so every one of
        this module's Python and JS strings was silently dropped. The
        catalogue now carries them.
        """
        product_model = self.env['ir.model']._get('product.product')
        return {
            'product': product_model.name or _("Product"),
            'barcode': self._core_label('product.product', 'barcode', _("Barcode")),
            'brand': self._core_label('product.template', BRAND_FIELD, _("Brand")),
            'season': self._core_label(
                'product.template', SEASON_FIELD, _("Season")),
            'position': self._core_label(
                'product.template', POSITION_FIELD, _("Positioning")),
            'category': self._core_label(
                'product.template', 'categ_id', _("Product Category")),
            'supplier': self._core_label(
                'product.supplierinfo', 'partner_id', _("Supplier")),
            # The price before any campaign, beside the promo one. lst_price
            # rather than the template's list_price: it carries the variant's
            # own attribute price extra, and the grid is one row per variant.
            'list_price': self._core_label(
                'product.product', 'lst_price', _("Sales Price")),
            # A pricelist name, not a word: the same in either language.
            'promo': PROMO_PRICELIST,
            # This one has no core field to borrow a label from - "how much
            # discount" is not an Odoo concept, it is this grid's own, so it
            # falls back to this module's own catalogue.
            'discount': self._core_label(
                'di.stock.replenish', 'discount_bucket', _("Discount %")),
            # Likewise the report's own name, so the button says what the
            # printout is called everywhere else in Odoo.
            'print': (self._tag_report().name
                      if self._tag_report() else _("Print Tags")),
            'source_hint': self._core_label(
                'stock.quant', 'available_quantity', _("free to use")),
            'dest_hint': self._core_label(
                'stock.quant', 'quantity', _("on hand")),
        }

    def _row_limit(self):
        """The quantity a destination has to be under for the mode to take an
        interest in it.

        "Missing" is one piece, not "at most zero". The two are the same thing
        here - every product on this database is in Units and not one quant
        carries a fraction (checked directly) - and stating it as a limit lets
        every mode share a single comparison.
        """
        self.ensure_one()
        if self.missing_mode == 'negative':
            return 0
        if self.missing_mode == 'below':
            return self.below_quantity
        return 1

    def _gap_limit(self):
        """What a destination has to be under for its *cell* to read as a gap.

        Deliberately not _row_limit. Below a Quantity keeps a row only when
        every destination is under the threshold, so every cell on screen is
        already under it and painting them all amber would say nothing at all.
        The highlight therefore keeps its standing meaning - an empty shelf -
        which is precisely the signal the eye still needs once the rows have
        been narrowed to the thin ones.

        Only Negative is the one mode that moves it, because there an empty
        shelf is not the point: a shelf below zero is.
        """
        self.ensure_one()
        return 0 if self.missing_mode == 'negative' else 1

    def _nothing_kept_message(self):
        """Why the comparison came back empty, in the words of the mode that
        emptied it - "nothing is missing" and "nothing is under 5" send a
        worker to different places."""
        self.ensure_one()
        if self.missing_mode == 'negative':
            return _("No destination is below zero on any of these products.")
        if self.missing_mode == 'below':
            return _(
                "No product is under %s at every one of these destinations. "
                "One shop holding more than that is enough to drop a row, so "
                "a longer destination list needs a higher number.",
                self.below_quantity)
        return _("Every destination already holds all of these products.")

    @api.constrains('below_quantity')
    def _check_below_quantity(self):
        for sheet in self:
            if sheet.below_quantity < 0:
                raise ValidationError(_(
                    "The quantity to stay below cannot be negative. Use "
                    "Only Negative to look for destinations under zero."))

    @api.model
    def _ordered_product_ids(self, product_ids):
        """The comparison's row order: ascending barcode, without ever
        splitting a product code across the page.

        Two rules that pull against each other, so both are stated here.
        Variants of one product code belong under each other - the code is
        what a worker reads down the Product Variant column - but their
        barcodes are not always consecutive, so ordering on barcode alone
        scatters a code across the page. Each code therefore travels as a
        block, placed at its own lowest barcode: the page still reads lowest
        to highest, and a code is never broken up.

        Barcodes are compared as numbers, not as text. They are 6, 8 and 13
        digits long on this database, and as text a 13-digit one sorts in
        among the 8-digit ones - the same trap di_tag_export sorts printed
        cartellini around. Anything without a usable barcode sorts last
        rather than heading the page, again as the cartellini do.

        Sorted here rather than in SQL: neither "as a number" nor "at the
        group's lowest barcode" can be written as an ORM order. Only the two
        columns it sorts on are read, and the result is a plain list of ids,
        so this stays one small query however big the catalogue is.
        """
        rows = self.env['product.product'].search_read(
            [('id', 'in', list(product_ids))], ['default_code', 'barcode'])

        def barcode_key(row):
            barcode = (row['barcode'] or '').strip()
            return (0, int(barcode)) if barcode.isdigit() else (1, 0)

        # Where each code's block sits: at the lowest barcode it contains.
        block = {}
        for row in rows:
            code = row['default_code'] or ''
            key = barcode_key(row)
            if code not in block or key < block[code]:
                block[code] = key

        # id last, so a page boundary always falls in the same place even
        # where two rows agree on everything else.
        return [row['id'] for row in sorted(
            rows,
            key=lambda row: (
                block[row['default_code'] or ''],
                row['default_code'] or '',
                barcode_key(row),
                row['id'],
            ),
        )]

    def get_comparison_matrix(self, offset=0, seasons=None, discount=None,
                              price=None, positions=None):
        """Everything the grid draws, in one round trip.

        ``offset`` pages through the result: the comparison itself is computed
        in full every time, since the gap filter depends on all of it, and
        only the ordered page of products is sent.

        ``seasons`` is the picker's current selection. It is written back to
        the record here rather than through a separate call, so choosing a
        season is one round trip and the choice survives reopening the
        document. Brands and categories are ordinary form fields instead, so
        they arrive by being saved before Refresh reads them.

        ``discount`` is the discount-bucket picker's current selection, as a
        string: ``None`` means "leave it as it was" (a page turn, a season
        change - anything that is not the worker touching this picker),
        ``''`` means "Tutti" (clear the filter), anything else is the bucket
        to narrow to. Same one-round-trip reasoning as seasons.

        ``price`` is the price picker's selection, with the same three-way
        meaning as ``discount``: ``None`` leaves it alone, ``''`` clears it,
        anything else is a Promo price to narrow to.

        ``positions`` is the posizionamento chips' selection, handled
        exactly as ``seasons``.
        """
        self.ensure_one()
        offset = max(0, int(offset or 0))
        if seasons is not None:
            self.season_keys = ','.join(
                str(int(season)) for season in seasons)
        if discount is not None:
            self.discount_bucket = discount
        if price is not None:
            self.price_bucket = price
        if positions is not None:
            self.position_keys = ','.join(
                str(int(position)) for position in positions)
        sources = self.source_warehouse_ids
        destinations = self.destination_warehouse_ids
        if not sources:
            raise UserError(_("Choose the source warehouse first."))
        if not destinations:
            return self._empty_matrix(_("Choose at least one destination."))

        # Only what can actually be moved is worth listing: stock already
        # reserved for another transfer cannot be sent anywhere. A product is
        # listed when any source has some, and `free` is what they can give
        # together - the figure the over-source guard measures a row against.
        free_by_source = self._source_free()
        free = self._total_free(free_by_source)
        if not free:
            return self._empty_matrix(
                _("%s holds no free stock matching these filters.",
                  self._source_names()))

        destination_stock = {
            warehouse.id: self._warehouse_stock(warehouse, free.keys())
            for warehouse in destinations
        }

        # Every mode is the same test with the line drawn somewhere else:
        # 'negative' under 0, 'missing' under 1, 'below' under whatever the
        # worker typed. What differs is how many destinations have to be under
        # it, and the two answers are two different questions rather than an
        # inconsistency:
        #
        #   missing / negative - ANY destination. "Who has run out?" One empty
        #     shelf is the whole reason to send a transfer, and it must not be
        #     hidden because the shop next door is well stocked.
        #   below - EVERY destination. "What are we thin on everywhere?" Asked
        #     with ANY it answers nothing useful: one habitually empty shop
        #     satisfies it for the entire catalogue, and on the buyers' own
        #     four destinations that kept 3540 rows out of 3540. It also put
        #     quantities at or above the threshold on screen, which is what
        #     the number was typed in to exclude.
        keep = set(free)
        limit = self._row_limit()
        if self.missing_mode != 'all':
            precision = self.env['decimal.precision'].precision_get(
                'Product Unit')
            # `destinations` is non-empty - the guard above returned already -
            # so all() cannot be vacuously true here.
            enough = all if self.missing_mode == 'below' else any

            def is_gap(quantity):
                return round(quantity, precision) < limit

            keep = {
                product_id for product_id in free
                if enough(
                    is_gap(destination_stock[warehouse.id].get(
                        product_id, (0.0, 0.0))[0])
                    for warehouse in destinations
                )
            }
        if not keep:
            return self._empty_matrix(self._nothing_kept_message())

        # The discount picker's own options come from what is left after the
        # season, brand, supplier, category and Show filters narrowed things -
        # not the whole catalogue - so it never offers a bucket nothing in
        # the current comparison is actually at. Computed before the bucket
        # filter below is applied, or picking one would immediately shrink
        # the list to just itself.
        available_discount_buckets = self._available_discount_buckets(
            self.env['product.product'].browse(keep))

        selected_bucket = self._selected_discount_bucket()
        if selected_bucket is not None:
            bucket_by_product = self._discount_buckets(
                self.env['product.product'].browse(keep))
            keep = {
                product_id for product_id in keep
                if bucket_by_product.get(product_id) == selected_bucket
            }
        if not keep:
            return self._empty_matrix(
                _("No product in this comparison is at that discount."),
                discount_buckets=available_discount_buckets)

        # The price picker's options, like the discount picker's, come from
        # what survived every other filter - computed before this one is
        # applied so picking a price does not shrink the list to itself.
        promo_by_product = self._promo_prices(
            self.env['product.product'].browse(keep))
        available_prices = self._available_prices(promo_by_product)

        selected_price = self._selected_price()
        if selected_price is not None:
            keep = {
                product_id for product_id in keep
                if promo_by_product.get(product_id)
                and self._price_key(promo_by_product[product_id])
                == selected_price
            }
        if not keep:
            return self._empty_matrix(
                _("No product in this comparison is at that price."),
                discount_buckets=available_discount_buckets,
                prices=available_prices)

        # Barcode ascending, with each product code kept together - see
        # _ordered_product_ids. The order has to be total and stable for the
        # row cap below to mean anything.
        ordered_ids = self._ordered_product_ids(keep)
        total = len(ordered_ids)
        if offset >= total:
            offset = max(0, (total - 1) // ROW_LIMIT * ROW_LIMIT)
        products = self.env['product.product'].browse(
            ordered_ids[offset:offset + ROW_LIMIT])

        promo = self._promo_prices(products)
        brands = self._brands(products)
        seasons_by_product = self._seasons(products)
        positions_by_product = self._positions(products)
        # What is already planned for the products on this page, so the grid
        # opens on the work in progress rather than on empty cells.
        planned = {}
        page_ids = set(products.ids)
        for line in self.line_ids:
            if line.product_id.id in page_ids:
                planned.setdefault(line.product_id.id, {})[
                    str(line.warehouse_id.id)] = line.quantity

        return {
            'sources': self._source_columns(),
            'warehouses': [{
                'id': warehouse.id,
                'code': warehouse.code or warehouse.name,
                'name': warehouse.name,
            } for warehouse in destinations],
            'lines': [{
                'id': product.id,
                'name': product.display_name,
                'code': product.default_code or '',
                'barcode': product.barcode or '',
                'brand': brands.get(product.id, ''),
                'season': seasons_by_product.get(product.id, ''),
                'position': positions_by_product.get(product.id, ''),
                'category': product.categ_id.name or '',
                'supplier': self._supplier(product),
                'list_price': product.lst_price or 0.0,
                'promo': promo.get(product.id, 0.0),
                'source_qty': free.get(product.id, 0.0),
                'source_cells': {
                    str(warehouse.id): free_by_source[warehouse.id].get(
                        product.id, 0.0)
                    for warehouse in sources
                },
                'planned': planned.get(product.id, {}),
                'cells': {
                    str(warehouse.id): destination_stock[warehouse.id].get(
                        product.id, (0.0, 0.0))[0]
                    for warehouse in destinations
                },
            } for product in products],
            'labels': self._matrix_labels(),
            'seasons': self._available_seasons(),
            'selected_seasons': self._selected_seasons(),
            'positions': self._available_positions(),
            'selected_positions': self._selected_positions(),
            'discount_buckets': available_discount_buckets,
            'selected_discount_bucket': self.discount_bucket or '',
            'prices': available_prices,
            'selected_price': selected_price or '',
            # What the grid paints a cell amber for, and the line the rows
            # were chosen at. Both sent rather than derived in the browser, so
            # the policy lives in one place - see _gap_limit and _row_limit.
            'gap_limit': self._gap_limit(),
            'row_limit': limit,
            'can_print': bool(self._tag_report()),
            'editable': self.state == 'draft',
            'total': total,
            'offset': offset,
            'limit': ROW_LIMIT,
            'range_label': '%s-%s / %s' % (
                offset + 1, min(offset + ROW_LIMIT, total), total),
            'message': '',
        }

    def _source_columns(self):
        return [{
            'id': warehouse.id,
            'code': warehouse.code or warehouse.name,
            'name': warehouse.display_name,
        } for warehouse in self.source_warehouse_ids]

    def _empty_matrix(self, message, discount_buckets=None, prices=None):
        return {
            'sources': [],
            'warehouses': [],
            'lines': [],
            'labels': self._matrix_labels(),
            'seasons': self._available_seasons(),
            'selected_seasons': self._selected_seasons(),
            'positions': self._available_positions(),
            'selected_positions': self._selected_positions(),
            'discount_buckets': (
                discount_buckets if discount_buckets is not None else []),
            'selected_discount_bucket': self.discount_bucket or '',
            'prices': prices if prices is not None else [],
            'selected_price': self._selected_price() or '',
            'gap_limit': self._gap_limit(),
            'row_limit': self._row_limit(),
            'can_print': bool(self._tag_report()),
            'editable': self.state == 'draft',
            'total': 0,
            'offset': 0,
            'limit': ROW_LIMIT,
            'range_label': '',
            'message': message,
        }

    # ------------------------------------------------------------------
    # One product's colours and sizes, under its row
    # ------------------------------------------------------------------

    @staticmethod
    def _variant_axis(variant, attribute_name):
        """This variant's value on one attribute, or ''. Read from
        product_template_attribute_value_ids, never from
        product_template_variant_value_ids: the latter drops any attribute
        with a single value on the template, so a one-colour style would
        come back with no colour at all."""
        for ptav in variant.product_template_attribute_value_ids:
            if ptav.attribute_id.name == attribute_name:
                return ptav.name or ''
        return ''

    @staticmethod
    def _size_sort_key(value):
        """Numeric sizes in numeric order, the rest alphabetically after."""
        try:
            return (0, float(value), '')
        except ValueError:
            return (1, 0.0, value)

    def get_variant_breakdown(self, product_id):
        """Every colour and size of the style one row belongs to, with the
        same stock columns the row itself shows.

        Read on demand when the worker opens a row, not sent with the page:
        a page is five hundred rows and a style has a dozen variants, and
        almost none of them get opened. The filters do not apply here on
        purpose - the question is "what else of this style is there", and a
        sibling that a filter hid is still an answer.
        """
        self.ensure_one()
        product = self.env['product.product'].browse(int(product_id)).exists()
        if not product:
            raise UserError(_("That product no longer exists."))
        variants = product.product_tmpl_id.product_variant_ids
        sources = self.source_warehouse_ids
        destinations = self.destination_warehouse_ids

        free_by_source = self._source_free(variants.ids)
        destination_stock = {
            warehouse.id: self._warehouse_stock(warehouse, variants.ids)
            for warehouse in destinations
        }

        rows = []
        for variant in variants:
            rows.append({
                'id': variant.id,
                'colour': self._variant_axis(variant, COLOUR_ATTRIBUTE),
                'size': self._variant_axis(variant, SIZE_ATTRIBUTE),
                'barcode': variant.barcode or '',
                'source_cells': {
                    str(warehouse.id): free_by_source[warehouse.id].get(
                        variant.id, 0.0)
                    for warehouse in sources
                },
                'cells': {
                    str(warehouse.id): destination_stock[warehouse.id].get(
                        variant.id, (0.0, 0.0))[0]
                    for warehouse in destinations
                },
            })
        rows.sort(key=lambda row: (
            row['colour'], self._size_sort_key(row['size']), row['id']))
        return {
            'product': product.product_tmpl_id.display_name,
            'rows': rows,
        }

    # ------------------------------------------------------------------
    # Editing the plan from the grid
    # ------------------------------------------------------------------

    def set_replenish_cells(self, cells):
        """Apply the quantities a worker typed into the grid.

        ``cells`` maps ``"<product id>-<warehouse id>"`` to a quantity and
        carries **only what changed**. It is deliberately not authoritative
        for the whole document the way the arrival's distribution grid is:
        the comparison is paged, so a payload can never describe the rows the
        worker cannot currently see. A cell cleared to zero deletes its line;
        a key that is absent is left alone.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "%s is no longer a draft, so its quantities cannot be "
                "changed.", self.name))

        Line = self.env['di.stock.replenish.line']
        existing = {
            (line.product_id.id, line.warehouse_id.id): line
            for line in self.line_ids
        }
        warehouse_scope = set(self.destination_warehouse_ids.ids)

        to_delete = Line.browse()
        to_create = []
        for key, value in (cells or {}).items():
            try:
                product_id, warehouse_id = (int(p) for p in key.split('-'))
            except ValueError as exc:
                raise UserError(_("Malformed cell %r.", key)) from exc
            if warehouse_id not in warehouse_scope:
                raise UserError(_(
                    "That destination is not one of this document's."))

            quantity = float(value or 0.0)
            if quantity < 0:
                raise UserError(_("A quantity cannot be negative."))

            line = existing.get((product_id, warehouse_id))
            if not quantity:
                if line:
                    to_delete |= line
            elif line:
                line.quantity = quantity
            else:
                to_create.append({
                    'replenish_id': self.id,
                    'product_id': product_id,
                    'warehouse_id': warehouse_id,
                    'quantity': quantity,
                })
        if to_delete:
            to_delete.unlink()
        if to_create:
            Line.create(to_create)

        self.env.flush_all()
        self._check_against_source()
        return self.get_comparison_matrix(offset=self.env.context.get(
            'replenish_offset', 0))

    def _check_against_source(self):
        """Refuse to plan more of a product than the sources can give.

        Checked across the whole document rather than per cell: three shops
        asking for four each is only a problem once they are added up. And
        against every source together, since confirming fills a line from
        the next source once the first runs dry - see _allocate_sources.
        """
        self.ensure_one()
        if not self.line_ids:
            return
        products = self.line_ids.product_id
        stock = self._total_free(self._source_free(products.ids))
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        for product in products:
            planned = sum(self.line_ids.filtered(
                lambda l, p=product: l.product_id == p).mapped('quantity'))
            free = stock.get(product.id, 0.0)
            if round(planned - free, precision) > 0:
                raise UserError(_(
                    "%(product)s: %(planned)s planned but only %(free)s free "
                    "at %(source)s.",
                    product=product.display_name,
                    planned=planned,
                    free=free,
                    source=self._source_names()))

    def _allocate_sources(self):
        """``[(line, source warehouse, quantity)]`` - where each planned
        piece is taken from.

        Each line is filled from the sources in the order the field lists
        them, moving to the next only once the first has nothing left. What a
        line takes is taken off for the lines after it, so two shops can
        never both be handed the last piece. On nearly the whole catalogue a
        variant is in one source only, so this has nothing to choose; the
        order only matters for the few held in both.

        Lines are walked in destination order then the grid's row order, so
        the result does not depend on the order the cells were typed in.
        A line the sources cannot cover keeps what they could give: the
        guard in _check_against_source runs before anything is confirmed.
        """
        self.ensure_one()
        remaining = self._source_free(self.line_ids.product_id.ids)
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        warehouse_order = {
            warehouse.id: index
            for index, warehouse in enumerate(self.destination_warehouse_ids)
        }
        product_order = {
            product_id: index
            for index, product_id in enumerate(
                self._ordered_product_ids(self.line_ids.product_id.ids))
        }
        lines = self.line_ids.sorted(key=lambda line: (
            warehouse_order.get(line.warehouse_id.id, len(warehouse_order)),
            product_order.get(line.product_id.id, len(product_order)),
            line.id,
        ))
        allocations = []
        for line in lines:
            left = line.quantity
            for source in self.source_warehouse_ids:
                if round(left, precision) <= 0:
                    break
                stock = remaining[source.id]
                take = min(left, stock.get(line.product_id.id, 0.0))
                if round(take, precision) <= 0:
                    continue
                stock[line.product_id.id] -= take
                left -= take
                allocations.append((line, source, take))
        return allocations

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    def action_confirm(self):
        """Generate one internal transfer per destination, ready to pick.

        Deliberately not validated here. The sheet and the cartellini are
        printed before anything moves, the worker picks the pile and scans it
        against the transfer in Inventory (or the Barcode app), and validates
        it there once the quantities agree. Stock is reserved in the
        meantime, which is what stops a second comparison promising the
        same piece.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Only a draft can be confirmed."))
        if not self.line_ids:
            raise UserError(_(
                "Add at least one quantity before confirming."))
        self._check_against_source()
        self._create_transfers()
        self.state = 'done'
        return True

    def action_cancel(self):
        """Cancel the document, once nothing it generated is still open.

        A confirmed document is not a dead end: cancel its transfers in
        Inventory and it can then be cancelled here, and from there reset to
        draft. What is refused is pulling the paperwork out from under a
        transfer somebody is picking - or one already validated, whose
        stock has moved and needs a transfer of its own to come back.
        """
        self.ensure_one()
        if self.state == 'done':
            open_pickings = self.picking_ids.filtered(
                lambda picking: picking.state != 'cancel')
            if open_pickings:
                raise UserError(_(
                    "%(name)s has transfers that are not cancelled: "
                    "%(pickings)s. Cancel those in Inventory first - or, if "
                    "one is already validated, reverse it with a transfer "
                    "of its own so the movement stays traceable.",
                    name=self.name,
                    pickings=', '.join(open_pickings.mapped('name'))))
        self.state = 'cancel'
        return True

    def action_reset_to_draft(self):
        self.ensure_one()
        if self.state != 'cancel':
            raise UserError(_(
                "Only a cancelled document can be set back to draft."))
        self.state = 'draft'
        return True

    def _get_picking_type(self, warehouse):
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('warehouse_id', '=', warehouse.id),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not picking_type:
            raise UserError(_(
                "Warehouse %s has no internal operation type configured.",
                warehouse.display_name))
        return picking_type

    def _create_transfers(self):
        """One internal transfer per source and destination.

        A transfer leaves from a single location, so a shop served from two
        sources gets two: each is picked in its own building and validated
        on its own. With one source this is one transfer per destination.
        """
        self.ensure_one()
        groups = {}
        for line, source, quantity in self._allocate_sources():
            groups.setdefault((source, line.warehouse_id), []).append(
                (line, quantity))
        pickings = self.env['stock.picking']
        for (source, warehouse), entries in groups.items():
            picking_type = self._get_picking_type(source)
            source_location = source.lot_stock_id
            destination = warehouse.lot_stock_id
            moves = [
                (0, 0, {
                    'product_id': line.product_id.id,
                    'product_uom_qty': quantity,
                    'product_uom': line.product_id.uom_id.id,
                    'location_id': source_location.id,
                    'location_dest_id': destination.id,
                    'company_id': self.company_id.id,
                    'date': self.date,
                })
                for line, quantity in entries
            ]
            picking = self.env['stock.picking'].create({
                'picking_type_id': picking_type.id,
                'origin': self.name,
                'location_id': source_location.id,
                'location_dest_id': destination.id,
                'scheduled_date': self.date,
                'company_id': self.company_id.id,
                'di_replenish_id': self.id,
                'move_ids': moves,
            })
            self._process_picking(picking)
            pickings |= picking
        return pickings

    def _process_picking(self, picking):
        """Confirm and reserve, but do not validate.

        Unlike di_stock_arrival, where the goods are already counted when the
        worker confirms, here the confirmation comes BEFORE the pile is
        picked: the sheet and the cartellini come out of this document, the
        worker walks the stockroom with them, and scans what they picked
        against the transfer. Validating here would move the stock on paper
        before anybody touched it, and there would be nothing left to check
        against. The transfer waits in Inventory as Ready, its stock
        reserved, until the worker validates it.
        """
        picking.action_confirm()
        picking.action_assign()
        return picking

    # ------------------------------------------------------------------
    # Printing and navigation
    # ------------------------------------------------------------------

    def report_destinations(self):
        """One entry per destination on the document, for the printout -
        each becomes a document of its own, on its own page(s), because a
        sheet is carried round the stockroom for one shop at a time and
        handed over with that shop's pile. Public rather than underscored
        so QWeb may call it.

        Ordered as the destination field lists them, with any warehouse that
        only appears on a line (the field having been narrowed since) after.
        """
        self.ensure_one()
        order = {
            warehouse.id: index
            for index, warehouse in enumerate(self.destination_warehouse_ids)
        }
        warehouses = self.line_ids.warehouse_id.sorted(
            key=lambda w: (order.get(w.id, len(order)), w.name or ''))
        sources = self._line_sources() if len(self.source_warehouse_ids) > 1 else {}
        result = []
        for warehouse in warehouses:
            lines = self.line_ids.filtered(
                lambda l, w=warehouse: l.warehouse_id == w)
            pickings = self.picking_ids.filtered(
                lambda p, w=warehouse: p.location_dest_id.warehouse_id == w)
            result.append({
                'warehouse': warehouse,
                'lines': lines,
                'quantity': sum(lines.mapped('quantity')),
                'pickings': pickings,
                'sources': sources,
            })
        return result

    def _line_sources(self):
        """``{line id: "MCP 2 · ING 1"}`` - which source each line is picked
        from, for a sheet with more than one source."""
        self.ensure_one()

        def piece(source, quantity):
            shown = int(quantity) if float(quantity).is_integer() else quantity
            return '%s %s' % (source.code or source.name, shown)

        return {
            line_id: ' · '.join(piece(source, qty) for source, qty in entries)
            for line_id, entries in self._line_allocations().items()
        }

    def _line_allocations(self):
        """``{line id: [(source warehouse, quantity), ...]}`` - where each
        line's pieces are picked, for the sheet and the cartellini.

        Once confirmed this is read off the transfers, which are what was
        actually reserved: re-running the allocation then would count the
        document's own reservations as stock that is no longer free. Before
        that it is the allocation confirming would make right now.
        """
        self.ensure_one()
        parts = {}
        open_pickings = self.picking_ids.filtered(
            lambda picking: picking.state != 'cancel')
        if self.state == 'done' and open_pickings:
            line_by_key = {
                (line.product_id.id, line.warehouse_id.id): line
                for line in self.line_ids
            }
            for picking in open_pickings:
                source = picking.location_id.warehouse_id
                for move in picking.move_ids:
                    line = line_by_key.get((
                        move.product_id.id,
                        picking.location_dest_id.warehouse_id.id))
                    if line:
                        parts.setdefault(line.id, []).append(
                            (source, move.product_uom_qty))
        else:
            for line, source, quantity in self._allocate_sources():
                parts.setdefault(line.id, []).append((source, quantity))
        return parts

    def report_labels(self):
        """Headings for the printed document.

        Taken from the fields they show, for the same reason the grid's are:
        on this database a field label translates and free text inside a
        template does not. Public rather than underscored so QWeb may call it -
        the template evaluator refuses private attributes.
        """
        self.ensure_one()
        doc = self.fields_get(
            ['source_warehouse_ids', 'destination_warehouse_ids', 'date',
             'total_quantity', 'picking_ids', 'state'],
            ['string', 'selection'])
        line = self.env['di.stock.replenish.line'].fields_get(
            ['product_id', 'default_code', 'barcode', 'quantity'], ['string'])
        states = dict(doc['state'].get('selection') or [])
        return {
            'source': doc['source_warehouse_ids']['string'],
            'source_names': self._source_names(),
            'destination': self.env['stock.picking'].fields_get(
                ['location_dest_id'], ['string'])['location_dest_id']['string'],
            'date': doc['date']['string'],
            'total': doc['total_quantity']['string'],
            'transfers': doc['picking_ids']['string'],
            'state': states.get(self.state, ''),
            'product': line['product_id']['string'],
            'code': line['default_code']['string'],
            'barcode': line['barcode']['string'],
            'quantity': line['quantity']['string'],
        }

    def action_print_document(self):
        self.ensure_one()
        return self.env.ref(
            'di_stock_replenish.action_report_replenish').report_action(self)

    def action_print_document_tags(self):
        """Cartellini for every planned piece on this document.

        ``action_print_tags`` is deliberately different: it is the grid's
        ad-hoc button, where each tick requests one label for that variant.
        The document action follows the saved plan instead. A row sending two
        of a variant needs two labels, and the same variant sent to two
        destinations needs to remain in both destination piles.

        A pile is one destination out of one source - where the pieces are
        actually walked out of - in the destination field's order, then the
        source field's. Inside it the labels follow that source's shelving
        order, and a blank card opens each pile when there is more than one:
        both are di_tag_export's tag_order, shared by every screen that
        prints cartellini.
        """
        self.ensure_one()
        report = self._tag_report()
        if not report:
            raise UserError(_(
                "The Cartellino report is not available on this database."))

        allocations = self._line_allocations()
        piles = {}
        for line in self.line_ids:
            for source, quantity in allocations.get(line.id, []):
                pieces = int(quantity)
                if pieces > 0:
                    piles.setdefault((line.warehouse_id, source), []).append(
                        (line.product_id, pieces))

        warehouse_order = {
            warehouse.id: index
            for index, warehouse in enumerate(self.destination_warehouse_ids)
        }
        source_order = {
            warehouse.id: index
            for index, warehouse in enumerate(self.source_warehouse_ids)
        }
        ordered = sorted(piles.items(), key=lambda item: (
            warehouse_order.get(item[0][0].id, len(warehouse_order)),
            source_order.get(item[0][1].id, len(source_order)),
            item[0][0].name or ''))
        tag_lines = self.env['product.product']._di_tag_lines([
            (destination.name, source, entries)
            for (destination, source), entries in ordered
        ])

        product_lines = [entry for entry in tag_lines if isinstance(entry, list)]
        if not product_lines:
            raise UserError(_("There is nothing to print tags for."))
        total = sum(pieces for _product_id, pieces in product_lines)
        if total > TAG_PRINT_LIMIT:
            raise UserError(_(
                "That would print %(total)s tags. Print them in smaller "
                "batches from the product list, or the printer and the PDF "
                "will both give up.", total=total))

        products = self.env['product.product'].browse(
            list(dict.fromkeys(
                int(product_id) for product_id, _pieces in product_lines)))
        quantities = {}
        for product_id, pieces in product_lines:
            quantities[product_id] = quantities.get(product_id, 0) + pieces
        return report.report_action(products, data={
            'tag_lines': tag_lines,
            # Lets a database with an older tag report still print the right
            # number of labels, although it cannot preserve the piles.
            'tag_quantities': quantities,
        })

    def action_view_transfers(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _("Transfers"),
            'res_model': 'stock.picking',
            'domain': [('id', 'in', self.picking_ids.ids)],
            'view_mode': 'list,form',
            'context': {'create': False},
        }
        if len(self.picking_ids) == 1:
            action.update(view_mode='form', res_id=self.picking_ids.id)
        return action
