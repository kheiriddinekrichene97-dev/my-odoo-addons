import logging
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Studio taxonomy, looked up by name rather than declared as a relation: a
# Many2one('x_posizionamenti') would stop this module installing on any
# database where Studio never made the model. di_stock_replenish reads the
# season the same defensive way, and for the same reason.
SEASON_FIELD = 'x_studio_prodotti2stagione'
SEASON_MODEL = 'x_stagioni'
POSITION_FIELD = 'x_prodotti2posizionamento'

# The order the shop floor reads a comparison in. Everything named here comes
# first, in this order; every other posizionamento follows alphabetically, and
# products with none at all come last. Written as names because that is what
# the workers asked for and what the records are keyed on - ids would silently
# reorder the page if the taxonomy were ever reimported.
POSITION_ORDER = ('SCA', 'APP')

# Same idea one level down: this category heads the page, the rest follow
# alphabetically.
CATEGORY_ORDER = ('ABBI',)

# The two warehouses the shops are restocked from. Defaults only - the field
# is editable, and they are resolved by code first and name second so a rename
# in Odoo does not silently empty the document.
SOURCE_WAREHOUSES = (('ICONT', 'ING - CONTESSE'),
                     ('MCP', 'MAG - CENTRALE PISTUNINA'))

# Till receipts that count as a sale. A draft order is still on the screen at
# the till, and a cancelled one never happened. There is no 'invoiced' state to
# add to these: an invoiced order is posted as 'done' - read off this
# database's own selection rather than assumed.
SOLD_STATES = ('paid', 'done')

# Points of sale a new run leaves out by default: they are restocked on a
# different rhythm. Still pickable by hand - this is only the default. Matched
# by name, case-insensitively, since tills are customer records with no
# external id.
DEFAULT_EXCLUDED_CONFIGS = ('Torregrotta', 'Silvi')

TAG_REPORT = 'di_tag_export.action_report_product_tag'

# A day's selling across eight shops is a few hundred pieces. Past this the
# PDF and the printer both give up, so say so rather than hang.
TAG_PRINT_LIMIT = 1000


class PosReplenish(models.Model):
    _name = 'di.pos.replenish'
    _inherit = ['mail.thread']
    _description = "Sales Replenishment"
    _order = 'date_to desc, id desc'

    name = fields.Char(
        string="Reference", required=True, copy=False, readonly=True,
        default=lambda self: _("New"))
    date = fields.Datetime(
        string="Created", required=True, default=fields.Datetime.now)
    state = fields.Selection(
        [('draft', "Draft"),
         ('confirmed', "Transfers Created"),
         ('cancel', "Cancelled")],
        string="Status", default='draft', required=True,
        copy=False, tracking=True)

    company_id = fields.Many2one(
        'res.company', string="Company", required=True, index=True,
        default=lambda self: self.env.company)

    # Dates, not datetimes: the workers think in days, and the period always
    # runs from the first minute of one day to the last of another. The
    # conversion to UTC happens once, in _period_bounds, against the user's
    # timezone - a shop's midnight is not the database's.
    date_from = fields.Date(
        string="From", required=True,
        default=lambda self: self._default_period()[0])
    date_to = fields.Date(
        string="To", required=True,
        default=lambda self: self._default_period()[1])

    config_ids = fields.Many2many(
        'pos.config', string="Points of Sale",
        default=lambda self: self._default_configs(),
        help="The shops to restock. Every till that sold something in the "
             "period gets its own table.")
    source_warehouse_ids = fields.Many2many(
        'stock.warehouse', string="Source Warehouses",
        default=lambda self: self._default_sources(),
        help="Where the replacement stock is taken from. A variant normally "
             "sits in only one of them, so nothing has to be prioritised; "
             "where both hold it, both are offered and you choose.")

    # The seasons the workers have dropped from this run, as ids in a Char -
    # the same defensive treatment SEASON_FIELD gets, and for the same reason.
    # Excluding is a filter over lines that already exist rather than a
    # recompute, so dropping a season never discards a quantity somebody has
    # already adjusted by hand.
    excluded_season_keys = fields.Char(
        string="Excluded Seasons",
        help="Seasons dropped from this run, as ids. Toggled with the "
             "chips above the table rather than typed.")

    computed_on = fields.Datetime(
        string="Built On", readonly=True, copy=False,
        help="When the rows were last read from the till receipts. An empty "
             "run that has been built means nothing was sold, which is not "
             "the same as a run nobody has built yet.")

    # Returns no sale of the same variant offsets in the period - a piece
    # sold last week and brought back today. They have no row (nothing to
    # replace), but the till counts them against the day, so the shop's
    # total has to as well or it never matches the till receipt. Kept per
    # season, as {config id: {season key: pieces}}, so dropping a season
    # drops its returns from the total too.
    unmatched_returns = fields.Json(
        string="Returns Without a Sale", readonly=True, copy=False)

    line_ids = fields.One2many(
        'di.pos.replenish.line', 'replenish_id', string="Lines", copy=False)
    line_count = fields.Integer(compute='_compute_totals', store=True)
    total_sold = fields.Float(
        compute='_compute_totals', store=True, string="Sold",
        digits='Product Unit')
    total_to_send = fields.Float(
        compute='_compute_totals', store=True, string="To Send",
        digits='Product Unit')

    picking_ids = fields.One2many(
        'stock.picking', 'di_pos_replenish_id', string="Transfers", copy=False)
    picking_count = fields.Integer(compute='_compute_picking_count')

    # Anchor for the grid. Stores nothing: the component reads through
    # get_sales_matrix(), so a run of several thousand rows is never written
    # into a field.
    matrix = fields.Char(string="Comparison", readonly=True)

    # ------------------------------------------------------------------
    # Defaults
    # ------------------------------------------------------------------

    @api.model
    def _default_period(self):
        """Yesterday, or Saturday-to-Sunday when today is a Monday.

        The shops sell on Saturday and on Sunday and nobody restocks in
        between, so Monday's run has two days of selling to catch up on.
        Every other day covers the single day before.
        """
        today = fields.Date.context_today(self)
        if today.weekday() == 0:  # Monday
            return today - timedelta(days=2), today - timedelta(days=1)
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday

    @api.model
    def _default_configs(self):
        excluded = {name.lower() for name in DEFAULT_EXCLUDED_CONFIGS}
        return self.env['pos.config'].search([
            ('company_id', 'in', (False, self.env.company.id)),
        ]).filtered(lambda config: (config.name or '').strip().lower()
                    not in excluded).ids

    @api.model
    def _default_sources(self):
        """The two source warehouses, matched on code then on name.

        Neither is module data - they are the customer's own records - so
        there is no external id to point at. Missing ones are simply left
        out; the field is editable and the grid says when it is empty.
        """
        Warehouse = self.env['stock.warehouse']
        company = [('company_id', 'in', (False, self.env.company.id))]
        found = Warehouse.browse()
        for code, name in SOURCE_WAREHOUSES:
            match = Warehouse.search(
                [('code', '=', code)] + company, limit=1)
            if not match:
                match = Warehouse.search(
                    [('name', '=', name)] + company, limit=1)
            if match:
                found |= match
            else:
                _logger.info(
                    "Source warehouse %r/%r not found on this database.",
                    code, name)
        return found.ids

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _("New"):
                vals['name'] = self._next_free_reference(
                    vals.get('company_id', self.env.company.id))
        return super().create(vals_list)

    @api.model
    def _next_free_reference(self, company_id):
        """The next reference the sequence gives that no run holds yet.

        Same guard as di.stock.arrival's, for the same reason: a database
        rebuilt from a backup keeps the runs but can carry an older sequence
        counter, and this model has no unique constraint to stop it - the
        next save would quietly repeat a reference that is already on
        another run. Draw again until the number is free; bounded.
        """
        Sequence = self.env['ir.sequence'].with_company(company_id)
        taken = self.sudo().with_context(active_test=False)
        for _attempt in range(1000):
            name = Sequence.next_by_code('di.pos.replenish')
            if not name:
                return _("New")
            if not taken.search_count([
                    ('name', '=', name), ('company_id', '=', company_id)]):
                return name
        raise UserError(_(
            "Could not find a free reference: every number the sequence "
            "gives is already used. Check the sales replenishment sequence "
            "under Settings > Technical > Sequences."))

    @api.depends('line_ids.net_sold_qty', 'line_ids.qty_to_send',
                 'line_ids.season_key', 'excluded_season_keys')
    def _compute_totals(self):
        # Filtered on the season here rather than through line.is_excluded:
        # that field is computed and not stored, and a stored total must not
        # depend on one - the totals would go stale the moment a season was
        # dropped without a line itself being written.
        for sheet in self:
            excluded = sheet._excluded_seasons()
            kept = sheet.line_ids.filtered(
                lambda line: line.season_key not in excluded)
            sheet.line_count = len(kept)
            sheet.total_sold = sum(kept.mapped('net_sold_qty'))
            sheet.total_to_send = sum(kept.mapped('qty_to_send'))

    def _compute_picking_count(self):
        for sheet in self:
            sheet.picking_count = len(sheet.picking_ids)

    @api.constrains('date_from', 'date_to')
    def _check_period(self):
        for sheet in self:
            if sheet.date_from > sheet.date_to:
                raise ValidationError(_(
                    "The period ends before it starts."))

    @api.ondelete(at_uninstall=False)
    def _unlink_only_unconfirmed(self):
        for sheet in self:
            if sheet.state == 'confirmed':
                raise UserError(_(
                    "%s has already generated transfers, so it cannot be "
                    "deleted. Cancel those in Inventory first, then cancel "
                    "the run.", sheet.name))

    # ------------------------------------------------------------------
    # The period, in the timezone the shops keep
    # ------------------------------------------------------------------

    def _period_bounds(self):
        """The period as the pair of UTC datetimes ``pos.order`` is stored in.

        ``date_order`` is a UTC datetime; the period is a pair of local days.
        Comparing the two without converting would shift the window by the
        offset - two hours of a summer evening's selling would fall into the
        wrong day, which is exactly the kind of error nobody notices until a
        shop is short.
        """
        self.ensure_one()
        tz = pytz.timezone(self.env.user.tz or 'Europe/Rome')
        start = tz.localize(datetime.combine(self.date_from, time.min))
        end = tz.localize(datetime.combine(self.date_to, time.max))
        return (start.astimezone(pytz.UTC).replace(tzinfo=None),
                end.astimezone(pytz.UTC).replace(tzinfo=None))

    # ------------------------------------------------------------------
    # What was sold
    # ------------------------------------------------------------------

    def _sales_by_config(self):
        """``{config id: {product id: (sold, returned)}}`` over the period.

        Sold and returned are read as two separate groupings rather than one
        signed total, so the table can show a worker why a number dropped.
        Netting them is the caller's job.

        One pair of grouped queries per till rather than one for all of them:
        ``pos.order.line`` carries no ``config_id`` of its own, and a grouping
        cannot walk ``order_id.config_id`` - checked against this database,
        which rejects the dotted path outright. Eight tills is sixteen
        grouped queries for a daily run, which is nothing.

        Only stocked products: there is nothing to send back for a gift
        card, a service or the "Articolo ESC non migrato" stand-in that the
        ESC till's rounding lines (ARRO. DL N. 50/2017) land on.
        """
        self.ensure_one()
        start, end = self._period_bounds()
        Line = self.env['pos.order.line']
        sales = {}
        for config in self.config_ids:
            base = [
                ('order_id.config_id', '=', config.id),
                ('order_id.date_order', '>=', start),
                ('order_id.date_order', '<=', end),
                ('order_id.state', 'in', SOLD_STATES),
                ('product_id.is_storable', '=', True),
            ]
            per_product = {}
            for sign, extra in ((1, ('qty', '>', 0)), (-1, ('qty', '<', 0))):
                rows = Line._read_group(
                    base + [extra], groupby=['product_id'],
                    aggregates=['qty:sum'])
                for product, quantity in rows:
                    sold, returned = per_product.get(product.id, (0.0, 0.0))
                    if sign > 0:
                        sold += quantity or 0.0
                    else:
                        # Stored negative; reported as a positive count of
                        # pieces that came back.
                        returned += abs(quantity or 0.0)
                    per_product[product.id] = (sold, returned)
            if per_product:
                sales[config.id] = per_product
        return sales

    # ------------------------------------------------------------------
    # What the sources hold
    # ------------------------------------------------------------------

    def _source_stock(self, product_ids):
        """``{warehouse id: {product id: free quantity}}`` for the sources.

        Free, not on hand: stock already reserved for another transfer cannot
        be sent anywhere, and proposing it would produce a picking that can
        never be filled. ``available_quantity`` is computed rather than
        stored, so it cannot be aggregated - it is derived from the two
        stored columns instead.
        """
        self.ensure_one()
        if not product_ids:
            return {warehouse.id: {} for warehouse in self.source_warehouse_ids}
        stock = {}
        for warehouse in self.source_warehouse_ids:
            rows = self.env['stock.quant']._read_group(
                [('location_id', 'child_of', warehouse.view_location_id.id),
                 ('location_id.usage', '=', 'internal'),
                 ('company_id', '=', self.company_id.id),
                 ('product_id', 'in', list(product_ids))],
                groupby=['product_id'],
                aggregates=['quantity:sum', 'reserved_quantity:sum'])
            stock[warehouse.id] = {
                product.id: (quantity or 0.0) - (reserved or 0.0)
                for product, quantity, reserved in rows
                if (quantity or 0.0) - (reserved or 0.0) > 0
            }
        return stock

    def _unstocked_line_ids(self, lines, stock=None):
        """The rows not worth showing: sold, but with nothing planned and no
        free stock at any source, so there is nothing a worker could send.

        Only hidden, never deleted. The line still counts towards what the
        till sold - the tab's total has to keep matching the till receipt -
        and it comes back on its own once stock reaches a source, with a
        cell to type into. A row with something planned always shows, so a
        confirmed run, whose own transfers have reserved the stock it needed,
        never loses its rows.

        ``stock`` is _source_stock's result, when the caller already has it.
        """
        self.ensure_one()
        if stock is None:
            stock = self._source_stock(lines.product_id.ids)
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        return {
            line.id for line in lines
            if round(line.qty_to_send, precision) <= 0
            and not any(
                round(per_product.get(line.product_id.id, 0.0), precision) > 0
                for per_product in stock.values())
        }

    # ------------------------------------------------------------------
    # Taxonomy, read defensively
    # ------------------------------------------------------------------

    @api.model
    def _has_studio_field(self, field_name):
        return field_name in self.env['product.template']._fields

    @api.model
    def _season_model(self):
        if not self._has_studio_field(SEASON_FIELD) or SEASON_MODEL not in self.env:
            return None
        return self.env[SEASON_MODEL].sudo()

    def _excluded_seasons(self):
        """The dropped season ids, tolerant of anything stale in the field."""
        self.ensure_one()
        return {
            int(chunk) for chunk in (self.excluded_season_keys or '').split(',')
            if chunk.strip().lstrip('-').isdigit()
        }

    @api.model
    def _position_rank(self, name):
        """Where a posizionamento sorts: the named ones first, in the order
        they are named, then everything else alphabetically, then nothing.

        Returned as a tuple so the name breaks the tie inside the trailing
        group - two unnamed posizionamenti still sort predictably against
        each other rather than by whichever the database returned first.
        """
        name = (name or '').strip()
        if not name:
            return (2, '')
        if name.upper() in POSITION_ORDER:
            return (0, '%02d' % POSITION_ORDER.index(name.upper()))
        return (1, name.upper())

    @api.model
    def _category_rank(self, category):
        """Where a category's root sorts, the root's name, and what the table
        calls the row.

        Ranking is on the root, so every ABBI child stays inside the ABBI
        block rather than scattering alphabetically among the others. The row
        is still labelled by the child - the workers read down a column of
        CAMI, GIAC, PANT, not of "ABBI / CAMI" repeated - and a category with
        no parent is labelled as it is.
        """
        if not category:
            return (2, ''), '', ''
        root = category
        while root.parent_id:
            root = root.parent_id
        root_name = root.name or ''
        if root_name.upper() in CATEGORY_ORDER:
            rank = (0, '%02d' % CATEGORY_ORDER.index(root_name.upper()))
        else:
            rank = (1, root_name.upper())
        return rank, root_name, category.name or ''

    # ------------------------------------------------------------------
    # Row order inside a group
    # ------------------------------------------------------------------

    @api.model
    def _barcode_key(self, barcode):
        """Barcodes compared as numbers, with the unusable ones sorted last.

        They are 6, 8 and 13 digits long on this database, so as text a
        13-digit one sorts in among the 8-digit ones and a printed pile stops
        matching the order goods are counted in. di_tag_export sorts the
        cartellini around the same trap, which is why the printed labels and
        the table on screen agree.
        """
        barcode = (barcode or '').strip()
        return (0, int(barcode)) if barcode.isdigit() else (1, 0)

    def _row_sort_key(self, product, info, block):
        """The reading order of a shop's table.

        Root category first, then posizionamento inside it, then the child
        category, then barcode: ABBI-SCA, ABBI-APP, ACCE-SCA, ACCE-APP. The
        merchandise class leads because that is how the shop floor walks a
        rail - all the clothing decisions together, then all the accessory
        ones - and SCA before APP inside each, because that is the order the
        buyers read a class in.

        The last two keys pull against each other: variants of one product
        code belong under each other, but their barcodes are not always
        consecutive, so ordering on barcode alone scatters a code across the
        page. Each code therefore travels as a block placed at its own lowest
        barcode, exactly as di_stock_replenish orders its comparison. id sorts
        last so the order is total, and a run is reproducible.
        """
        code = product.default_code or ''
        return (info['category_rank'],
                info['position_rank'],
                (info['category_label'] or '').upper(),
                block.get(code, (1, 0)), code,
                self._barcode_key(product.barcode), product.id)

    # ------------------------------------------------------------------
    # Building the run
    # ------------------------------------------------------------------

    def action_compute(self):
        """Read the period's selling and rebuild every line from scratch.

        Destructive by design, and only from a draft: this is the button that
        says "the period changed, start again". Dropping a season is a filter
        rather than a recompute precisely so that it does not come through
        here and throw away hand-adjusted quantities.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "%s is no longer a draft, so it cannot be rebuilt.",
                self.name))
        if not self.config_ids:
            raise UserError(_("Choose at least one point of sale."))
        if not self.source_warehouse_ids:
            raise UserError(_("Choose at least one source warehouse."))

        sales = self._sales_by_config()
        self.line_ids.unlink()
        self.computed_on = fields.Datetime.now()
        self.unmatched_returns = {}
        if not sales:
            return self._reopen()

        product_ids = {
            product_id
            for per_product in sales.values()
            for product_id in per_product
        }
        stock = self._source_stock(product_ids)
        products = self.env['product.product'].browse(sorted(product_ids))
        detail = self._product_detail(products)

        # What every till wants, added up, so the proposal can never promise
        # the same piece to two shops. Whichever shop is served first takes
        # it; the rest see the shortfall on their own row.
        remaining = {
            warehouse_id: dict(per_product)
            for warehouse_id, per_product in stock.items()
        }

        Line = self.env['di.pos.replenish.line']
        unmatched = {}
        for config in self.config_ids:
            per_product = sales.get(config.id)
            if not per_product:
                continue
            rows = self._build_config_rows(
                config, per_product, detail, remaining)
            if rows:
                Line.create(rows)
            per_season = {}
            for product_id, (sold, returned) in per_product.items():
                if returned > sold:
                    key = str(detail[product_id]['season_key'])
                    per_season[key] = per_season.get(key, 0.0) + returned - sold
            if per_season:
                unmatched[str(config.id)] = per_season
        self.unmatched_returns = unmatched
        return self._reopen()

    def _product_detail(self, products):
        """Everything the ordering and the table need, read once per run."""
        has_season = self._has_studio_field(SEASON_FIELD)
        has_position = self._has_studio_field(POSITION_FIELD)
        detail = {}
        for product in products:
            template = product.product_tmpl_id
            season = template[SEASON_FIELD] if has_season else None
            position = template[POSITION_FIELD] if has_position else None
            category_rank, category_root, category_label = self._category_rank(
                product.categ_id)
            position_name = (position.x_name or '') if position else ''
            detail[product.id] = {
                'season_key': season.id if season else 0,
                'season_name': (season.x_name or '') if season else '',
                'position_name': position_name,
                'position_rank': self._position_rank(position_name),
                'category_rank': category_rank,
                'category_root': category_root,
                'category_label': category_label,
            }
        return detail

    def _build_config_rows(self, config, per_product, detail, remaining):
        """The create values for one shop's table, already in reading order."""
        rows = []
        products = self.env['product.product'].browse(sorted(per_product))

        # Where each product code's block sits: at the lowest barcode it
        # contains, so a code is never broken across the page.
        block = {}
        for product in products:
            code = product.default_code or ''
            key = self._barcode_key(product.barcode)
            if code not in block or key < block[code]:
                block[code] = key

        ordered = sorted(
            products,
            key=lambda product: self._row_sort_key(
                product, detail[product.id], block))

        for sequence, product in enumerate(ordered, start=1):
            sold, returned = per_product[product.id]
            # A return puts the piece back on the shop's own shelf, so it
            # needs no replacement. Clamped at zero: a window that catches a
            # return whose sale happened before it must not ask a shop to
            # send stock back to the warehouse.
            net = max(sold - returned, 0.0)
            if net <= 0:
                continue
            info = detail[product.id]
            allocations = self._propose_allocation(product.id, net, remaining)
            rows.append({
                'replenish_id': self.id,
                'config_id': config.id,
                'product_id': product.id,
                'sequence': sequence,
                'sold_qty': sold,
                'returned_qty': returned,
                'net_sold_qty': net,
                'season_key': info['season_key'],
                'season_name': info['season_name'],
                'position_name': info['position_name'],
                'category_root': info['category_root'],
                'category_label': info['category_label'],
                'allocation_ids': [
                    (0, 0, {'warehouse_id': warehouse_id,
                            'quantity': quantity})
                    for warehouse_id, quantity in allocations
                ],
            })
        return rows

    def _propose_allocation(self, product_id, needed, remaining):
        """Fill a row from the sources, and take what is proposed off them.

        A variant sits in one source or the other on almost this whole
        catalogue - a direct count put it at 90 variants in both out of some
        nine thousand - so there is nothing to prioritise on nearly every
        row and the proposal simply fills from the first source that has it.
        The worker can move a quantity between sources afterwards; this only
        saves them typing the obvious case eight times a morning.

        ``remaining`` is carried across the whole run and decremented here,
        so two shops that sold the last piece cannot both be told to take it.

        A source with nothing left is still written as a zero row, so the
        grid always has a cell to type into rather than a gap where a
        warehouse ought to be.
        """
        allocations = []
        left = needed
        for warehouse in self.source_warehouse_ids:
            free = remaining.get(warehouse.id, {}).get(product_id, 0.0)
            take = min(left, free) if left > 0 else 0.0
            if take > 0:
                remaining.setdefault(warehouse.id, {})[product_id] = free - take
                left -= take
            allocations.append((warehouse.id, take))
        return allocations

    def _reopen(self):
        """Reopen this record, so the grid remounts and draws the new rows.

        The grid reads through get_sales_matrix() when it mounts, not from a
        field, so a plain record reload leaves it showing the run as it was
        before Compute. Returning the form action is the one thing that is
        certain to remount it on every version.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Everything the grid draws
    # ------------------------------------------------------------------

    @api.model
    def _core_label(self, model, field_name, default):
        """Borrow Odoo's own translated label for a column that shows a core
        field.

        Odoo translates its field labels on every database, where a custom
        module's catalogue has to be loaded to reach _(). Every column that
        *is* a core field therefore takes its heading from the field, which
        is both certain and keeps the grid speaking the same words as the
        rest of Inventory. _() stays as the fallback.
        """
        model_obj = self.env.get(model)
        if model_obj is not None and field_name in model_obj._fields:
            described = model_obj.fields_get([field_name], ['string'])
            label = (described.get(field_name) or {}).get('string')
            if label:
                return label
        return default

    def _matrix_labels(self):
        line = self.env['di.pos.replenish.line']
        described = line.fields_get(
            ['sold_qty', 'returned_qty', 'net_sold_qty', 'qty_to_send'],
            ['string'])
        return {
            'product': self.env['ir.model']._get('product.product').name
                       or _("Product"),
            'barcode': self._core_label(
                'product.product', 'barcode', _("Barcode")),
            'season': self._core_label(
                'product.template', SEASON_FIELD, _("Season")),
            'position': self._core_label(
                'product.template', POSITION_FIELD, _("Positioning")),
            'category': self._core_label(
                'product.template', 'categ_id', _("Product Category")),
            'sold': described['sold_qty']['string'],
            'returned': described['returned_qty']['string'],
            'net': described['net_sold_qty']['string'],
            'to_send': described['qty_to_send']['string'],
            'available': self._core_label(
                'stock.quant', 'available_quantity', _("free to use")),
            'print': (self._tag_report().name
                      if self._tag_report() else _("Print Tags")),
        }

    def _tag_report(self):
        return self.env.ref(TAG_REPORT, raise_if_not_found=False)

    def get_sales_matrix(self, config_id=None, excluded_seasons=None):
        """One round trip for the whole screen.

        ``excluded_seasons`` is written back here rather than through a call
        of its own, so dropping a season is a single round trip and the
        choice survives reopening the document.

        The whole run is sent, not a page of it: a day's selling across eight
        shops is hundreds of rows, not the tens of thousands the catalogue-
        wide comparison in di_stock_replenish has to page through, and the
        tables are read shop by shop anyway.
        """
        self.ensure_one()
        if excluded_seasons is not None:
            self.excluded_season_keys = ','.join(
                str(int(key)) for key in excluded_seasons)

        excluded = self._excluded_seasons()
        warehouses = [{
            'id': warehouse.id,
            'code': warehouse.code or warehouse.name,
            'name': warehouse.name,
        } for warehouse in self.source_warehouse_ids]

        # Every till that has rows, with its own totals, so the tabs can show
        # what is waiting behind each one without loading it.
        #
        # Driven by the tills the rows actually belong to rather than by
        # config_ids alone: a run built last week and reopened after somebody
        # narrowed the scope would otherwise lose the tab for a shop whose
        # rows - and whose transfers - are still sitting on the document.
        # config_ids leads, so the usual case keeps the order of the field.
        #
        # 'sold' is the till's own net total, the figure ESC prints: the rows,
        # less the returns that have no row of their own.
        unmatched = self.unmatched_returns or {}
        configs = []
        for config in self.config_ids | self.line_ids.config_id:
            lines = self.line_ids.filtered(
                lambda line, c=config: line.config_id == c)
            if not lines:
                continue
            kept = lines.filtered(lambda line: line.season_key not in excluded)
            returned = sum(
                pieces for key, pieces in unmatched.get(str(config.id), {}).items()
                if int(key) not in excluded)
            configs.append({
                'id': config.id,
                'name': config.name,
                'warehouse': config.warehouse_id.display_name or '',
                'sold': sum(kept.mapped('net_sold_qty')) - returned,
                'to_send': sum(kept.mapped('qty_to_send')),
            })

        if not configs:
            return self._empty_matrix(
                _("Nothing was sold in that period at the chosen points of "
                  "sale.")
                if self.computed_on else
                _("Nothing to show yet. Set the period and press Compute."),
                warehouses)

        active = int(config_id or 0)
        if active not in {entry['id'] for entry in configs}:
            active = configs[0]['id']

        lines = self.line_ids.filtered(
            lambda line: line.config_id.id == active)
        stock = self._source_stock(lines.product_id.ids)
        # Sold with nothing at either source: nothing to send, so no row -
        # see _unstocked_line_ids. Counted, so the screen can say so.
        unstocked = self._unstocked_line_ids(lines, stock)
        hidden = 0

        groups = []
        current = None
        for line in lines.sorted(key=lambda line: line.sequence):
            if line.season_key in excluded:
                continue
            if line.id in unstocked:
                hidden += 1
                continue
            heading = (line.category_root, line.position_name,
                       line.category_label)
            if current is None or current['key'] != heading:
                current = {
                    'key': heading,
                    'root': line.category_root or _("Uncategorised"),
                    'position': line.position_name or _("Unclassified"),
                    # A root category is its own label, so repeating it in the
                    # heading would read "ACCE / SCA / ACCE". Blank here means
                    # the heading has nothing more to add.
                    'category': (line.category_label
                                 if line.category_label != line.category_root
                                 else ''),
                    'rows': [],
                }
                groups.append(current)
            take = {
                str(allocation.warehouse_id.id): allocation.quantity
                for allocation in line.allocation_ids
            }
            available = {
                str(warehouse['id']): stock.get(
                    warehouse['id'], {}).get(line.product_id.id, 0.0)
                for warehouse in warehouses
            }
            current['rows'].append({
                'id': line.id,
                'product_id': line.product_id.id,
                'name': line.product_id.display_name,
                'code': line.product_id.default_code or '',
                'barcode': line.product_id.barcode or '',
                'season': line.season_name,
                'sold': line.sold_qty,
                'returned': line.returned_qty,
                'net': line.net_sold_qty,
                'available': available,
                'take': take,
                'total': line.qty_to_send,
                # The row the workers need to see: sold more than the
                # warehouses can replace, so the shop stays short.
                'short': line.qty_to_send < line.net_sold_qty,
            })

        # Groups the exclusion emptied are not worth a heading.
        groups = [group for group in groups if group['rows']]

        return {
            'configs': configs,
            'active_config': active,
            'warehouses': warehouses,
            'groups': groups,
            'seasons': self._run_seasons(),
            'excluded_seasons': sorted(excluded),
            'labels': self._matrix_labels(),
            'can_print': bool(self._tag_report()),
            'editable': self.state == 'draft',
            'hidden_unstocked': hidden,
            'message': '',
        }

    def _run_seasons(self):
        """The seasons this run actually touched, with what each carries.

        Built from the lines rather than from the season model: a picker
        offering all 77 seasons when the day's selling covered four would be
        a list to search rather than a row of chips to press.
        """
        self.ensure_one()
        totals = {}
        for line in self.line_ids:
            key = line.season_key
            name, sold = totals.get(key, (line.season_name, 0.0))
            totals[key] = (name or line.season_name, sold + line.net_sold_qty)
        return [
            {'id': key, 'name': name or _("No season"), 'sold': sold}
            for key, (name, sold) in sorted(
                totals.items(), key=lambda item: (item[1][0] or '').upper())
        ]

    def _empty_matrix(self, message, warehouses=None):
        return {
            'configs': [],
            'active_config': 0,
            'warehouses': warehouses or [],
            'groups': [],
            'seasons': [],
            'excluded_seasons': [],
            'labels': self._matrix_labels(),
            'can_print': bool(self._tag_report()),
            'editable': self.state == 'draft',
            'hidden_unstocked': 0,
            'message': message,
        }

    # ------------------------------------------------------------------
    # Editing from the grid
    # ------------------------------------------------------------------

    def set_take_cells(self, cells, config_id=None):
        """Apply the quantities a worker typed.

        ``cells`` maps ``"<line id>-<warehouse id>"`` to a quantity and
        carries only what changed - a key that is absent is left alone.
        Every line named has to belong to this document: the ids come from
        the browser, so they are re-resolved here rather than trusted.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "%s is no longer a draft, so its quantities cannot be "
                "changed.", self.name))

        by_id = {line.id: line for line in self.line_ids}
        sources = set(self.source_warehouse_ids.ids)
        Allocation = self.env['di.pos.replenish.allocation']

        for key, value in (cells or {}).items():
            try:
                line_id, warehouse_id = (int(part) for part in key.split('-'))
            except ValueError as exc:
                raise UserError(_("Malformed cell %r.", key)) from exc
            line = by_id.get(line_id)
            if not line:
                raise UserError(_(
                    "That row is not part of this document."))
            if warehouse_id not in sources:
                raise UserError(_(
                    "That warehouse is not one of this document's sources."))

            quantity = float(value or 0.0)
            if quantity < 0:
                raise UserError(_("A quantity cannot be negative."))

            allocation = line.allocation_ids.filtered(
                lambda a, w=warehouse_id: a.warehouse_id.id == w)
            if allocation:
                allocation.quantity = quantity
            elif quantity:
                Allocation.create({
                    'line_id': line.id,
                    'warehouse_id': warehouse_id,
                    'quantity': quantity,
                })

        self.env.flush_all()
        self._check_against_sources()
        return self.get_sales_matrix(config_id=config_id)

    def _check_against_sources(self):
        """Refuse to send more of a variant than a source can give.

        Totalled across the whole document rather than per row: three shops
        each asking for the last two pieces is only a problem once they are
        added up, and that is exactly the case the proposal's running
        ``remaining`` prevents but a hand edit can reintroduce.
        """
        self.ensure_one()
        planned = {}
        for line in self.line_ids:
            if line.is_excluded:
                continue
            for allocation in line.allocation_ids:
                if allocation.quantity <= 0:
                    continue
                key = (allocation.warehouse_id.id, line.product_id.id)
                planned[key] = planned.get(key, 0.0) + allocation.quantity
        if not planned:
            return

        stock = self._source_stock({product_id for _wh, product_id in planned})
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        for (warehouse_id, product_id), quantity in planned.items():
            free = stock.get(warehouse_id, {}).get(product_id, 0.0)
            if round(quantity - free, precision) > 0:
                warehouse = self.env['stock.warehouse'].browse(warehouse_id)
                product = self.env['product.product'].browse(product_id)
                raise UserError(_(
                    "%(product)s: %(planned)s asked for across the shops but "
                    "only %(free)s free at %(warehouse)s.",
                    product=product.display_name,
                    planned=quantity,
                    free=free,
                    warehouse=warehouse.display_name))

    def set_excluded_seasons(self, season_keys, config_id=None):
        self.ensure_one()
        return self.get_sales_matrix(
            config_id=config_id, excluded_seasons=season_keys or [])

    # ------------------------------------------------------------------
    # Printing
    # ------------------------------------------------------------------

    def _tag_quantities(self, lines):
        """``{"product id": label count}`` - one cartellino per piece sent.

        Two of a variant going to a shop need two labels, not one: the
        warehouse worker picks against the pile. Quantities are summed per
        variant, so a variant split across both sources is one entry with
        the total, which is what gets picked anyway.

        Keyed by the id as a string, which is what di_tag_export reads first:
        this dict travels inside a report action, and a JSON round trip to
        the browser turns integer keys into strings anyway. Writing them that
        way from the start means the same call also survives XML-RPC, which
        refuses to marshal a non-string key outright.
        """
        quantities = {}
        for line in lines:
            pieces = int(line.qty_to_send)
            if pieces > 0:
                key = str(line.product_id.id)
                quantities[key] = quantities.get(key, 0) + pieces
        return quantities

    def _print_lines(self, lines):
        """The labels in the order they come off the printer, as di_tag_export
        takes them: ``[product id, label count]`` per entry, and a blank
        ``{'separator': ...}`` card in front of each pile when the print
        covers more than one.

        A pile is one shop out of one source warehouse, walked by one person:
        interleaving ING - CONTESSE and MAG - CENTRALE PISTUNINA labels would
        send them back and forth between two buildings. Shops follow the
        document's order, warehouses the source field's. Inside a pile the
        order is that warehouse's own - ABBI SCA, BORS, ABBI APP, ACCE SCA at
        ING - CONTESSE, Donna then Uomo at PISTUNINA, ascending barcode in
        each step - which lives in di_tag_export's tag_order, shared by every
        screen that prints cartellini.

        A variant split across both warehouses therefore appears in both
        piles - three labels in one and one in the other - which is why this
        is a list rather than the flat product-to-count map
        ``_tag_quantities`` returns.
        """
        self.ensure_one()
        piles = {}
        for line in lines:
            for allocation in line.allocation_ids:
                pieces = int(allocation.quantity)
                if pieces > 0:
                    piles.setdefault(
                        (line.config_id, allocation.warehouse_id), []
                    ).append((line.product_id, pieces))

        config_order = {
            config.id: index
            for index, config in enumerate(self.config_ids)
        }
        warehouse_order = {
            warehouse.id: index
            for index, warehouse in enumerate(self.source_warehouse_ids)
        }
        ordered = sorted(piles.items(), key=lambda item: (
            config_order.get(item[0][0].id, len(config_order)),
            warehouse_order.get(item[0][1].id, len(warehouse_order)),
            item[0][0].name or ''))
        return self.env['product.product']._di_tag_lines([
            (config.name, warehouse, entries)
            for (config, warehouse), entries in ordered
        ])

    def _print_tags(self, lines):
        report = self._tag_report()
        if not report:
            raise UserError(_(
                "The Cartellino report is not available on this database."))
        tag_lines = self._print_lines(lines)
        labels = [entry for entry in tag_lines if isinstance(entry, list)]
        if not labels:
            raise UserError(_("There is nothing to print tags for."))
        total = sum(pieces for _product_id, pieces in labels)
        if total > TAG_PRINT_LIMIT:
            raise UserError(_(
                "That would print %(total)s tags. Print one point of sale at "
                "a time, or the printer and the PDF will both give up.",
                total=total))
        products = self.env['product.product'].browse(
            list(dict.fromkeys(int(product_id)
                               for product_id, _pieces in labels)))
        return report.report_action(products, data={
            'tag_lines': tag_lines,
            # Kept beside it so a database whose di_tag_export predates
            # tag_lines still prints the right number of labels - just in its
            # own barcode order rather than in warehouse piles.
            'tag_quantities': self._tag_quantities(lines),
        })

    def action_print_tags(self, config_id=None):
        """Cartellini for one shop, or for the whole run when none is named."""
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: not line.is_excluded)
        if config_id:
            lines = lines.filtered(
                lambda line, c=int(config_id): line.config_id.id == c)
            if not lines:
                raise UserError(_(
                    "That point of sale has nothing left to send."))
        return self._print_tags(lines)

    def action_print_document(self):
        self.ensure_one()
        return self.env.ref(
            'di_pos_replenish.action_report_pos_replenish').report_action(self)

    def report_lines(self):
        """The document's rows for the printout, grouped as on screen.

        Public rather than underscored so QWeb may call it: the template
        evaluator refuses private attributes.
        """
        self.ensure_one()
        # The same rows the screen leaves out: a sheet carried round the
        # warehouse has no use for a line with nothing to pick.
        unstocked = self._unstocked_line_ids(self.line_ids)
        result = []
        for config in self.config_ids:
            sold_lines = self.line_ids.filtered(
                lambda line, c=config: (line.config_id == c
                                        and not line.is_excluded))
            lines = sold_lines.filtered(lambda line: line.id not in unstocked)
            if not lines:
                continue
            groups = []
            current = None
            for line in lines.sorted(key=lambda line: line.sequence):
                heading = (line.category_root, line.position_name,
                           line.category_label)
                if current is None or current['key'] != heading:
                    current = {'key': heading,
                               'root': line.category_root or '-',
                               'position': line.position_name or '-',
                               'category': (line.category_label
                                            if line.category_label
                                            != line.category_root else ''),
                               'lines': []}
                    groups.append(current)
                current['lines'].append(line)
            result.append({
                'config': config,
                'groups': groups,
                # What the shop sold includes the rows left off the sheet,
                # as the shop's tab on screen does.
                'sold': sum(sold_lines.mapped('net_sold_qty')),
                'to_send': sum(lines.mapped('qty_to_send')),
            })
        return result

    def report_labels(self):
        """Headings for the printout, taken from the fields they show - the
        same reasoning as _core_label, and the same reason di_stock_replenish
        builds its printed headings server-side."""
        self.ensure_one()
        doc = self.fields_get(['date_from', 'date_to', 'state'],
                              ['string', 'selection'])
        line = self.env['di.pos.replenish.line'].fields_get(
            ['product_id', 'default_code', 'barcode', 'net_sold_qty',
             'qty_to_send'], ['string'])
        labels = self._matrix_labels()
        labels.update({
            'date_from': doc['date_from']['string'],
            'date_to': doc['date_to']['string'],
            'state': dict(doc['state'].get('selection') or {}).get(
                self.state, ''),
            'code': line['default_code']['string'],
        })
        return labels

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    def action_confirm(self):
        """Generate the internal transfers and leave them ready to validate.

        Deliberately not validated here. The paperwork is printed before the
        goods move - that is the whole point of the cartellini - so the
        warehouse validates each transfer as it actually picks it. Stock
        stays reserved in the meantime, which is what stops a second run
        promising the same piece.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Only a draft can be confirmed."))
        self._check_against_sources()
        pickings = self._create_transfers()
        if not pickings:
            raise UserError(_(
                "Nothing to transfer: every row is at zero, or every season "
                "has been dropped."))
        self.state = 'confirmed'
        return True

    def action_cancel(self):
        """Cancel the run, once nothing it generated is still open.

        A confirmed run is not a dead end: cancel its transfers in Inventory
        and the run itself can then be cancelled, and from there reset to
        draft. What is refused is cancelling the paperwork out from under a
        transfer somebody is still picking - the movement has to stay
        traceable to the run that asked for it.
        """
        self.ensure_one()
        if self.state == 'confirmed':
            open_pickings = self.picking_ids.filtered(
                lambda picking: picking.state != 'cancel')
            if open_pickings:
                raise UserError(_(
                    "%(name)s has transfers that are still open: %(pickings)s. "
                    "Cancel those in Inventory first, so the movement stays "
                    "traceable to the run that asked for it.",
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
        """One internal transfer per source warehouse and shop.

        Split that way because that is how the work is actually done: one
        worker walks one warehouse for one shop's pile, and the transfer is
        the piece of paper that goes with it.
        """
        self.ensure_one()
        moves_by_route = {}
        for line in self.line_ids:
            if line.is_excluded:
                continue
            destination = line.config_id.warehouse_id
            if not destination:
                raise UserError(_(
                    "Point of sale %s has no warehouse configured.",
                    line.config_id.display_name))
            for allocation in line.allocation_ids:
                if allocation.quantity <= 0:
                    continue
                key = (allocation.warehouse_id, destination)
                moves_by_route.setdefault(key, []).append(
                    (line.product_id, allocation.quantity))

        pickings = self.env['stock.picking']
        for (source, destination), moves in moves_by_route.items():
            if source == destination:
                # A shop that is also a source would otherwise get a
                # transfer to itself, which Odoo accepts and nobody can pick.
                continue
            picking_type = self._get_picking_type(source)
            picking = self.env['stock.picking'].create({
                'picking_type_id': picking_type.id,
                'origin': '%s - %s' % (self.name, destination.name),
                'location_id': source.lot_stock_id.id,
                'location_dest_id': destination.lot_stock_id.id,
                'scheduled_date': self.date,
                'company_id': self.company_id.id,
                'di_pos_replenish_id': self.id,
                'move_ids': [
                    (0, 0, {
                        'product_id': product.id,
                        'product_uom_qty': quantity,
                        'product_uom': product.uom_id.id,
                        'location_id': source.lot_stock_id.id,
                        'location_dest_id': destination.lot_stock_id.id,
                        'company_id': self.company_id.id,
                        'date': self.date,
                    })
                    for product, quantity in moves
                ],
            })
            # Confirmed and reserved, not validated: the goods are still on
            # the warehouse shelf when this runs.
            picking.action_confirm()
            picking.action_assign()
            pickings |= picking
        return pickings

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
