from odoo import _, api, fields, models
from odoo.exceptions import UserError

# A tag per piece multiplies fast. di_tag_export now hands wkhtmltopdf a few
# large documents instead of one per label, so a whole arrival of several
# thousand pieces prints in one go; the cap only stops a typo in a quantity
# from asking for a stack no printer is going to get through.
TAG_PRINT_LIMIT = 10000


class StockArrival(models.Model):
    _name = 'di.stock.arrival'
    _description = "Stock Arrival"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(
        string="Reference", required=True, copy=False, readonly=True,
        index=True, default=lambda self: _("New"))
    partner_id = fields.Many2one(
        'res.partner', string="Supplier", required=True, tracking=True,
        domain="[('supplier_rank', '>', 0)]",
        # Must be a dict: the web client splats it with ** when reading the
        # field, so a string raises TypeError. A string ``domain`` is fine -
        # that one is evaluated client-side.
        context={
            'res_partner_search_mode': 'supplier',
            'default_is_company': True,
            'default_supplier_rank': 1,
        },
        help="Supplier the goods came from. A new supplier can be created inline.")
    date = fields.Datetime(
        string="Arrival Date", required=True, tracking=True,
        default=fields.Datetime.now)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    warehouse_id = fields.Many2one(
        'stock.warehouse', string="Received At", required=True,
        check_company=True, tracking=True,
        default=lambda self: self.env.company._di_get_arrival_warehouse(),
        help="Warehouse the goods physically arrived at. Configured in "
             "Inventory settings; goods always land here before division.")
    location_id = fields.Many2one(
        'stock.location', string="Arrival Location",
        related='warehouse_id.lot_stock_id', store=True)
    state = fields.Selection(
        [('draft', "Draft"),
         ('to_divide', "Not Divided"),
         ('divided', "Divided"),
         ('cancel', "Cancelled")],
        string="Status", default='draft', required=True, copy=False,
        tracking=True, index=True)
    note = fields.Html(string="Notes")
    # Written by the import: the variants the catalogue already held and
    # where they were in stock, as the grid the import dialog showed. Kept
    # here so it is still there once the dialog is closed.
    existing_html = fields.Html(
        string="Already in the Catalogue", sanitize=False, readonly=True,
        copy=False)

    line_ids = fields.One2many(
        'di.stock.arrival.line', 'arrival_id', string="Lines", copy=True)
    allocation_ids = fields.One2many(
        'di.stock.arrival.allocation', 'arrival_id', string="Distribution")

    receipt_id = fields.Many2one(
        'stock.picking', string="Receipt", copy=False, readonly=True,
        help="Transfer that brought the goods into the arrival warehouse.")
    # No domain here on purpose: ``picking_type_code`` is not stored in v19
    # and cannot be used in a One2many domain. The receipt and the internal
    # transfers are kept together and split in Python where needed.
    picking_ids = fields.One2many(
        'stock.picking', 'di_arrival_id', string="Transfers")
    transfer_count = fields.Integer(compute='_compute_transfer_count')

    # Stored like the other four totals: they share _compute_totals, and a
    # compute method that writes both stored and non-stored fields makes
    # reading this one silently recompute and save the rest. Storing it also
    # keeps 'compute_sudo' uniform across the group.
    line_count = fields.Integer(compute='_compute_totals', store=True)
    total_quantity = fields.Float(
        compute='_compute_totals', string="Received", store=True,
        digits='Product Unit')
    total_allocated = fields.Float(
        compute='_compute_totals', string="Distributed", store=True,
        digits='Product Unit')
    total_remaining = fields.Float(
        compute='_compute_totals', string="Staying Here", store=True,
        digits='Product Unit')
    total_value = fields.Monetary(
        compute='_compute_totals', string="Total Value", store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')

    _name_company_uniq = models.Constraint(
        'unique(name, company_id)',
        "An arrival with this reference already exists.",
    )

    @api.depends('line_ids.quantity', 'line_ids.qty_allocated',
                 'line_ids.price_unit')
    def _compute_totals(self):
        for arrival in self:
            lines = arrival.line_ids
            arrival.line_count = len(lines)
            arrival.total_quantity = sum(lines.mapped('quantity'))
            arrival.total_allocated = sum(lines.mapped('qty_allocated'))
            arrival.total_remaining = (
                arrival.total_quantity - arrival.total_allocated)
            arrival.total_value = sum(
                line.quantity * line.price_unit for line in lines)

    @api.depends('picking_ids')
    def _compute_transfer_count(self):
        for arrival in self:
            arrival.transfer_count = len(arrival.picking_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _("New")) == _("New"):
                company_id = vals.get('company_id', self.env.company.id)
                vals['name'] = self._next_free_reference(company_id)
        return super().create(vals_list)

    @api.model
    def _next_free_reference(self, company_id):
        """The next reference the sequence gives that no arrival holds yet.

        The counter can fall behind the records: a database rebuilt from a
        backup carries the arrivals but a sequence from an earlier day, and
        the next save then draws a number that is already taken and dies on
        the unique constraint - for every arrival, until somebody moves the
        counter by hand. Drawing again until the number is free costs one
        query per skipped reference and puts the counter back in front by
        itself. Bounded, so a broken sequence cannot spin forever.
        """
        Sequence = self.env['ir.sequence'].with_company(company_id)
        taken = self.sudo().with_context(active_test=False)
        for _attempt in range(1000):
            name = Sequence.next_by_code('di.stock.arrival')
            if not name:
                return _("New")
            if not taken.search_count([
                    ('name', '=', name), ('company_id', '=', company_id)]):
                return name
        raise UserError(_(
            "Could not find a free arrival reference: every number the "
            "sequence gives is already used. Check the 'Stock Arrival' "
            "sequence under Settings > Technical > Sequences."))

    def unlink(self):
        if any(arrival.state not in ('draft', 'cancel') for arrival in self):
            raise UserError(_(
                "Only draft or cancelled arrivals can be deleted. An arrival "
                "that has been confirmed has already moved stock."))
        return super().unlink()

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    def action_confirm(self):
        """Receive the goods into the arrival warehouse.

        Generates and validates one receipt so the stock is real and
        countable before the worker starts dividing it.
        """
        for arrival in self:
            if arrival.state != 'draft':
                raise UserError(_(
                    "Only a draft arrival can be confirmed."))
            if not arrival.line_ids:
                raise UserError(_(
                    "Add at least one line before confirming this arrival."))
            # Lines at zero are kept on the document and left out of the
            # receipt; an arrival made of nothing but those received nothing.
            if not arrival.line_ids.filtered(lambda l: l.quantity > 0):
                raise UserError(_(
                    "At least one line must have a quantity greater than "
                    "zero: this arrival received nothing."))
            arrival.receipt_id = arrival._create_receipt()
            arrival.state = 'to_divide'
        return True

    def action_validate_division(self):
        """Generate one internal transfer per destination warehouse.

        Whatever has not been allocated stays at the arrival warehouse and
        needs no transfer, so this is deliberately never gated on the lines
        being fully allocated.
        """
        for arrival in self:
            if arrival.state != 'to_divide':
                raise UserError(_(
                    "Only an arrival that has been confirmed and not yet "
                    "divided can be validated."))
            arrival._create_transfers()
            arrival.state = 'divided'
        return True

    def action_cancel(self):
        for arrival in self:
            if arrival.state not in ('draft',):
                raise UserError(_(
                    "Arrival %s has already received stock, so it cannot be "
                    "cancelled. Reverse it with a return or an inventory "
                    "adjustment instead, so the stock movement stays "
                    "traceable.", arrival.name))
            arrival.state = 'cancel'
        return True

    def action_reset_to_draft(self):
        for arrival in self:
            if arrival.state != 'cancel':
                raise UserError(_(
                    "Only a cancelled arrival can be set back to draft."))
            arrival.state = 'draft'
        return True

    # ------------------------------------------------------------------
    # Stock moves
    # ------------------------------------------------------------------

    def _get_picking_type(self, code, warehouse):
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', code),
            ('warehouse_id', '=', warehouse.id),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not picking_type:
            raise UserError(_(
                "Warehouse %(warehouse)s has no %(code)s operation type "
                "configured.",
                warehouse=warehouse.display_name, code=code))
        return picking_type

    def _prepare_move_values(self, product, quantity, location, location_dest):
        return {
            'product_id': product.id,
            'product_uom_qty': quantity,
            'product_uom': product.uom_id.id,
            'location_id': location.id,
            'location_dest_id': location_dest.id,
            'company_id': self.company_id.id,
            'date': self.date,
        }

    def _create_receipt(self):
        """Create and validate the supplier receipt for every line."""
        self.ensure_one()
        picking_type = self._get_picking_type('incoming', self.warehouse_id)
        supplier_location = (
            picking_type.default_location_src_id
            or self.env.ref('stock.stock_location_suppliers'))
        moves = [
            (0, 0, self._prepare_move_values(
                line.product_id, line.quantity,
                supplier_location, self.location_id))
            for line in self.line_ids.filtered(lambda l: l.quantity > 0)
        ]
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': self.partner_id.id,
            'origin': self.name,
            'location_id': supplier_location.id,
            'location_dest_id': self.location_id.id,
            'scheduled_date': self.date,
            'company_id': self.company_id.id,
            'di_arrival_id': self.id,
            'move_ids': moves,
        })
        self._process_picking(picking)
        return picking

    def _create_transfers(self):
        """Create and validate one internal transfer per destination."""
        self.ensure_one()
        allocations = self.allocation_ids.filtered(lambda a: a.quantity > 0)
        if not allocations:
            # Nothing was divided; everything stays at the arrival warehouse.
            return self.env['stock.picking']

        picking_type = self._get_picking_type('internal', self.warehouse_id)
        pickings = self.env['stock.picking']
        for warehouse in allocations.mapped('warehouse_id'):
            destination = warehouse.lot_stock_id
            moves = []
            for allocation in allocations.filtered(
                    lambda a, w=warehouse: a.warehouse_id == w):
                moves.append((0, 0, self._prepare_move_values(
                    allocation.product_id, allocation.quantity,
                    self.location_id, destination)))
            picking = self.env['stock.picking'].create({
                'picking_type_id': picking_type.id,
                'origin': self.name,
                'location_id': self.location_id.id,
                'location_dest_id': destination.id,
                'scheduled_date': self.date,
                'company_id': self.company_id.id,
                'di_arrival_id': self.id,
                'move_ids': moves,
            })
            self._process_picking(picking)
            pickings |= picking
        return pickings

    def _process_picking(self, picking):
        """Confirm, reserve and validate a picking in one step.

        The goods are physically present when the worker records them, so
        there is nothing to wait for.
        """
        picking.action_confirm()
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        result = picking.button_validate()
        # ``button_validate`` returns an action when it wants to raise a
        # wizard (backorder, immediate transfer). Quantities always match the
        # demand here, so anything coming back is unexpected and worth
        # surfacing rather than swallowing.
        if isinstance(result, dict) and result.get('res_model'):
            raise UserError(_(
                "Transfer %(picking)s could not be validated automatically "
                "(Odoo asked for '%(model)s'). Validate it manually from "
                "Inventory, then reopen this arrival.",
                picking=picking.name, model=result['res_model']))
        return picking

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_open_import_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Import Arrival Lines"),
            'res_model': 'di.stock.arrival.import',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_arrival_id': self.id},
        }

    def action_open_divide_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Divide Across Warehouses"),
            'res_model': 'di.stock.arrival.divide',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_arrival_id': self.id},
        }

    def action_print_tags(self):
        """Print one cartellino per piece received, for the whole arrival.

        Quantities are passed to the tag report rather than the products
        alone: three of the same size and colour need three identical labels.
        The stack comes out in the order the receiving warehouse is shelved
        in - di_tag_export's tag_order, the same order every screen prints
        cartellini in - and a single pile, so no separator card.
        """
        self.ensure_one()
        if self.state not in ('to_divide', 'divided'):
            raise UserError(_(
                "Confirm the arrival before printing its tags: the goods have "
                "to be in before they can be labelled."))

        report = self.env.ref(
            'di_tag_export.action_report_product_tag', raise_if_not_found=False)
        if not report:
            raise UserError(_(
                "The Cartellino report is not available on this database."))

        quantities = {}
        for line in self.line_ids:
            pieces = int(line.quantity)
            if pieces > 0:
                quantities[line.product_id.id] = (
                    quantities.get(line.product_id.id, 0) + pieces)
        if not quantities:
            raise UserError(_("There is nothing to print tags for."))

        total = sum(quantities.values())
        if total > TAG_PRINT_LIMIT:
            raise UserError(_(
                "That would print %(total)s tags. Print them in smaller "
                "batches from the product list, or the printer and the PDF "
                "will both give up.", total=total))

        Product = self.env['product.product']
        products = Product.browse(list(quantities))
        tag_lines = Product._di_tag_lines([(
            False, self.warehouse_id,
            [(product, quantities[product.id]) for product in products],
        )])
        return report.report_action(products, data={
            'tag_lines': tag_lines,
            # Kept beside it for a di_tag_export that predates tag_lines.
            'tag_quantities': {str(pid): qty for pid, qty in quantities.items()},
        })

    def action_view_transfers(self):
        self.ensure_one()
        pickings = self.picking_ids | self.receipt_id
        action = {
            'type': 'ir.actions.act_window',
            'name': _("Transfers"),
            'res_model': 'stock.picking',
            'domain': [('id', 'in', pickings.ids)],
            'view_mode': 'list,form',
            'context': {'create': False},
        }
        if len(pickings) == 1:
            action.update(view_mode='form', res_id=pickings.id)
        return action

    # ------------------------------------------------------------------
    # Distribution matrix
    #
    # Backs the grid in the Distribution tab: lines down, destination
    # warehouses across. The client holds no business rules of its own - it
    # reads and writes through these two methods, so the allocation
    # constraints stay the single source of truth.
    # ------------------------------------------------------------------

    EDITABLE_STATES = ('draft', 'to_divide')

    def _split_quantity_changes(self, lines, quantities):
        """Sort the pending received quantities into raises and drops.

        A quantity that goes up has to be written before the cells that need
        the room, and one that goes down after the cells have given it back.
        Anything already at the value asked for is left alone, so a grid saved
        without touching that tab writes nothing here at all.
        """
        raises, drops = [], []
        for key, value in (quantities or {}).items():
            try:
                line_id = int(key)
                quantity = float(value)
            except (TypeError, ValueError) as exc:
                raise UserError(_(
                    "Malformed received quantity for line %r.", key)) from exc
            line = lines.get(line_id)
            if not line:
                raise UserError(_(
                    "Line %s does not belong to this arrival.", line_id))
            if quantity < 0:
                raise UserError(_(
                    "A received quantity cannot be negative (%(product)s).",
                    product=line.product_id.display_name))
            if line.product_id.uom_id.compare(quantity, line.quantity) == 0:
                continue
            (raises if quantity > line.quantity else drops).append(
                (line, quantity))
        return raises, drops

    def get_distribution_matrix(self):
        """Everything the grid needs, in one round trip."""
        self.ensure_one()
        warehouses = self.env['stock.warehouse'].search([
            ('company_id', '=', self.company_id.id),
            ('id', '!=', self.warehouse_id.id),
        ])
        cells = {}
        for allocation in self.allocation_ids:
            key = f'{allocation.line_id.id}-{allocation.warehouse_id.id}'
            cells[key] = allocation.quantity
        return {
            'editable': self.state in self.EDITABLE_STATES,
            'arrival_warehouse': self.warehouse_id.display_name,
            'lines': [{
                'id': line.id,
                'name': line.product_id.display_name,
                'code': line.product_id.default_code or '',
                'received': line.quantity,
            } for line in self.line_ids],
            'warehouses': [{
                'id': warehouse.id,
                'code': warehouse.code or warehouse.name,
                'name': warehouse.name,
            } for warehouse in warehouses],
            'cells': cells,
        }

    def set_distribution_matrix(self, cells, quantities=None):
        """Write the grid back.

        ``cells`` maps ``"<line id>-<warehouse id>"`` to a quantity and is
        authoritative for everything the grid can show: a cell the client
        cleared is simply absent, so any allocation of this arrival to a
        listed warehouse that is missing from the payload is removed.
        Allocations to a warehouse the grid never listed - an archived one,
        say - are outside that scope and left untouched.

        ``quantities`` carries the received quantities the worker changed in
        the Products Received tab and has not saved yet, as ``{line id: qty}``.
        They are written here, in this transaction, because the two edits are
        only legal together: a line corrected to zero with its cells cleared
        passes, while saving either half on its own leaves allocated above
        received and trips the constraint. Raises come first and drops last,
        for the same reason the allocations shrink before they grow.
        """
        self.ensure_one()
        if self.state not in self.EDITABLE_STATES:
            raise UserError(_(
                "The distribution of this arrival can no longer be changed."))

        Allocation = self.env['di.stock.arrival.allocation']
        lines = {line.id: line for line in self.line_ids}
        raises, drops = self._split_quantity_changes(lines, quantities)
        for line, quantity in raises:
            line.quantity = quantity
        warehouse_scope = set(self.env['stock.warehouse'].search([
            ('company_id', '=', self.company_id.id),
            ('id', '!=', self.warehouse_id.id),
        ]).ids)

        targets = {}
        for key, value in (cells or {}).items():
            try:
                line_id, warehouse_id = (int(part) for part in key.split('-'))
            except ValueError as exc:
                raise UserError(_("Malformed distribution cell %r.", key)) from exc
            if line_id not in lines:
                raise UserError(_(
                    "Line %s does not belong to this arrival.", line_id))
            quantity = float(value or 0.0)
            if quantity < 0:
                raise UserError(_(
                    "A distributed quantity cannot be negative (%(product)s).",
                    product=lines[line_id].product_id.display_name))
            if quantity:
                targets[(line_id, warehouse_id)] = quantity

        existing = {
            (a.line_id.id, a.warehouse_id.id): a for a in self.allocation_ids
        }

        # Shrink before growing. Moving ten pieces from one shop to another
        # would otherwise stand at twenty halfway through the write and trip
        # the over-allocation constraint on a distribution that is fine.
        to_delete = Allocation.browse()
        for pair, allocation in existing.items():
            if pair[1] not in warehouse_scope:
                continue
            target = targets.get(pair, 0.0)
            if not target:
                to_delete |= allocation
            elif target < allocation.quantity:
                allocation.quantity = target
        if to_delete:
            to_delete.unlink()

        to_create = []
        for pair, quantity in targets.items():
            allocation = existing.get(pair)
            if allocation and allocation not in to_delete:
                if allocation.quantity != quantity:
                    allocation.quantity = quantity
            else:
                to_create.append({
                    'line_id': pair[0],
                    'warehouse_id': pair[1],
                    'quantity': quantity,
                })
        if to_create:
            Allocation.create(to_create)

        # Last, now that nothing is allocated above them any more.
        for line, quantity in drops:
            line.quantity = quantity

        # Deleting rows does not re-run the line constraint, so the finished
        # grid is validated once as a whole.
        self.env.flush_all()
        self.line_ids._check_not_over_allocated()
        return self.get_distribution_matrix()
