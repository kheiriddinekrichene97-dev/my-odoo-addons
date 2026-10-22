from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_repr


class StockArrivalLine(models.Model):
    _name = 'di.stock.arrival.line'
    _description = "Stock Arrival Line"
    _order = 'arrival_id, sequence, id'
    # Without these a line shows as "di.stock.arrival.line,35" wherever it is
    # referenced - which is what the distribution rows used to display.
    _rec_name = 'product_id'
    _rec_names_search = ['product_id']

    arrival_id = fields.Many2one(
        'di.stock.arrival', string="Arrival", required=True,
        ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='arrival_id.company_id', store=True, index=True)
    state = fields.Selection(related='arrival_id.state', store=True)

    product_id = fields.Many2one(
        'product.product', string="Product", required=True,
        check_company=True, domain="[('is_storable', '=', True)]")
    barcode = fields.Char(related='product_id.barcode', string="Barcode")
    default_code = fields.Char(
        related='product_id.default_code', string="Internal Reference")

    quantity = fields.Float(
        string="Received", required=True, default=1.0,
        digits='Product Unit',
        help="Quantity that physically arrived, for this variant.")
    price_unit = fields.Float(
        string="Unit Cost", digits='Product Price',
        help="Purchase price per unit. Recorded for reporting; product "
             "categories use periodic valuation, so this posts no journal "
             "entry.")
    subtotal = fields.Monetary(compute='_compute_subtotal', store=True)
    currency_id = fields.Many2one(related='arrival_id.currency_id')

    allocation_ids = fields.One2many(
        'di.stock.arrival.allocation', 'line_id', string="Distribution")
    qty_allocated = fields.Float(
        compute='_compute_quantities', store=True, string="Distributed",
        digits='Product Unit')
    qty_remaining = fields.Float(
        compute='_compute_quantities', store=True, string="Staying Here",
        digits='Product Unit',
        help="Quantity left at the arrival warehouse. This is informational: "
             "leaving stock here is normal and never blocks the document.")

    @api.depends('product_id', 'product_id.display_name')
    def _compute_display_name(self):
        """Show the variant, the way the worker sees it on the receipt."""
        for line in self:
            line.display_name = line.product_id.display_name or _("New line")

    @api.depends('quantity', 'price_unit')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.price_unit

    @api.depends('quantity', 'allocation_ids.quantity')
    def _compute_quantities(self):
        for line in self:
            line.qty_allocated = sum(line.allocation_ids.mapped('quantity'))
            line.qty_remaining = line.quantity - line.qty_allocated

    @api.constrains('quantity')
    def _check_quantity(self):
        """Zero is allowed, below zero is not.

        Supplier files list the whole size grid and put a 0 against the sizes
        that did not come. Keeping those rows is the point: the arrival then
        shows the grid the supplier sent, and a size that turns up after all
        is corrected in place rather than typed in from scratch. A zero line
        receives nothing - it is left out of the receipt entirely.
        """
        for line in self:
            if line.quantity < 0:
                raise ValidationError(_(
                    "Received quantity cannot be negative (%s).",
                    line.product_id.display_name))

    @api.constrains('quantity', 'allocation_ids')
    def _check_not_over_allocated(self):
        """Refuse a distribution that adds up to more than what arrived.

        Also called from ``di.stock.arrival.allocation``: a constraint on
        ``allocation_ids`` only fires when the one2many itself is written, so
        editing an allocation row directly used to slip past it entirely.
        """
        precision = self.env['decimal.precision'].precision_get(
            'Product Unit')
        for line in self:
            if round(line.qty_allocated - line.quantity, precision) > 0:
                raise ValidationError(_(
                    "You cannot distribute more of %(product)s than arrived: "
                    "%(received)s received, %(allocated)s distributed.\n"
                    "Reduce the distribution by %(excess)s, or correct the "
                    "received quantity.",
                    product=line.product_id.display_name,
                    received=line.quantity,
                    allocated=line.qty_allocated,
                    excess=round(line.qty_allocated - line.quantity,
                                 precision)))

    # ------------------------------------------------------------------
    # Correcting what arrived, after the goods are already in
    # ------------------------------------------------------------------
    #
    # A worker confirms the arrival, then counts again and finds the pile is
    # eighteen, not twenty. Until the division is generated that correction
    # has to be possible - but confirming validated a receipt, so the stock
    # is already on hand and the number on this line is no longer just a
    # number. Quantities are therefore editable while the arrival is in
    # 'to_divide', and every change is carried through to that receipt.
    #
    # Products themselves are not: adding or removing one after the goods are
    # in means a movement that never happened, or one that has to be undone.
    # Those stay a draft-only edit, and the guards below say so.

    @api.model_create_multi
    def create(self, vals_list):
        arrivals = self.env['di.stock.arrival'].browse({
            vals['arrival_id'] for vals in vals_list if vals.get('arrival_id')
        })
        received = arrivals.filtered(lambda a: a.state != 'draft')
        if received:
            raise UserError(_(
                "Products can only be added while the arrival is a draft, and "
                "%s has already received its goods.\n"
                "Correct the quantity of a line instead, or record what "
                "turned up later as an arrival of its own.",
                ", ".join(received.mapped('name'))))
        return super().create(vals_list)

    def unlink(self):
        received = self.filtered(lambda line: line.arrival_id.state != 'draft')
        if received:
            raise UserError(_(
                "A line cannot be removed once its goods have been received: "
                "the stock is already in the warehouse.\n"
                "Correct the received quantity instead, or send the goods "
                "back with a return from Inventory."))
        return super().unlink()

    def write(self, vals):
        if 'quantity' not in vals:
            return super().write(vals)
        # Read the quantities before the write, for the lines whose goods are
        # already in stock: the receipt has to follow the correction, or the
        # warehouse holds what the paperwork says it does not.
        previous = {
            line.id: line.quantity for line in self
            if line.arrival_id.state == 'to_divide'
        }
        # After the division the goods have left for the shops, so a
        # correction here would match neither the receipt nor the transfers.
        # The view already locks the lines; this is the same rule for anything
        # writing through the ORM.
        divided = self.filtered(
            lambda l: l.arrival_id.state == 'divided'
            and not l.product_id.uom_id.is_zero(vals['quantity'] - l.quantity))
        if divided:
            raise UserError(_(
                "Arrival %s has already been divided, so what it received can "
                "no longer be corrected here. Adjust the transfers from "
                "Inventory instead.",
                ", ".join(divided.mapped('arrival_id.name'))))
        res = super().write(vals)
        corrections = {}
        for line in self.filtered(lambda l: l.id in previous):
            note = line._sync_receipt_quantity(previous[line.id])
            if note:
                corrections.setdefault(line.arrival_id, []).append(note)
        for arrival, notes in corrections.items():
            arrival.message_post(body=Markup("<br/>").join(
                Markup.escape(note) for note in notes))
        return res

    def _receipt_move(self):
        """The validated receipt movement this line's goods came in on.

        Matched on the product rather than kept in a link field: confirming
        the arrival runs ``action_confirm``, which merges identical moves, so
        two lines of the same variant arrive as one movement and a stored
        link would point at a move that no longer exists.
        """
        self.ensure_one()
        return self.arrival_id.receipt_id.move_ids.filtered(
            lambda m: m.product_id == self.product_id and m.state == 'done')

    def _add_receipt_move(self):
        """Receive a line that arrived at zero, onto the receipt as it stands.

        The same four steps ``_process_picking`` runs, for one move on an
        already validated receipt: confirm, assign, pick, done. Nothing merges
        into the movements that are there - ``_merge_moves`` skips candidates
        in state done - so this lands as a movement of its own, dated when the
        goods were actually found.
        """
        self.ensure_one()
        arrival = self.arrival_id
        receipt = arrival.receipt_id
        if not receipt:
            raise UserError(_(
                "Arrival %s has no receipt to add these goods to.",
                arrival.name))
        move = self.env['stock.move'].create(dict(
            arrival._prepare_move_values(
                self.product_id, self.quantity,
                receipt.location_id, arrival.location_id),
            picking_id=receipt.id,
            date=fields.Datetime.now()))
        move = move._action_confirm()
        move._action_assign()
        move.quantity = self.quantity
        move.picked = True
        move._action_done()
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        return _(
            "%(product)s: %(quantity)s received after all, where the file "
            "said none. Added to receipt %(picking)s.",
            product=self.product_id.display_name,
            quantity=float_repr(self.quantity, precision),
            picking=receipt.name)

    def _sync_receipt_quantity(self, previous):
        """Carry a corrected quantity through to the validated receipt.

        Writing the new quantity onto the done move line is the path Odoo
        itself takes when a validated transfer is unlocked and edited: it
        undoes the original quant movement and re-applies it, so the stock on
        hand ends up matching the correction. The difference is applied, not
        the total, because one movement can carry several lines of the same
        variant.

        Returns a line of text for the chatter, or None when nothing moved.
        """
        self.ensure_one()
        uom = self.product_id.uom_id
        delta = self.quantity - previous
        if uom.is_zero(delta):
            return None
        moves = self._receipt_move()
        if not moves:
            # A line imported at zero brought nothing in, so the receipt holds
            # no movement for it. Correcting it upwards is that size turning
            # up after all: record it on the same receipt instead of sending
            # the worker to Inventory for it.
            if uom.is_zero(previous):
                return self._add_receipt_move()
            raise UserError(_(
                "The receipt movement for %(product)s cannot be found on "
                "%(arrival)s, so correcting the quantity here would leave the "
                "stock behind. Adjust it from Inventory instead.",
                product=self.product_id.display_name,
                arrival=self.arrival_id.name))
        move = moves[0]
        if len(move.move_line_ids) != 1:
            raise UserError(_(
                "The receipt of %(product)s was recorded in several detailed "
                "operations, which this correction cannot rewrite safely. "
                "Adjust it from Inventory instead.",
                product=self.product_id.display_name))
        move_line = move.move_line_ids
        corrected = move_line.quantity + delta
        if uom.compare(corrected, 0) < 0:
            raise UserError(_(
                "Correcting %(product)s to %(quantity)s would take more out "
                "of the receipt than it brought in.",
                product=self.product_id.display_name,
                quantity=self.quantity))
        # The move line moves the stock; the demand follows so the receipt
        # does not read as half delivered afterwards.
        move_line.quantity = corrected
        move.product_uom_qty = move.product_uom_qty + delta
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        return _(
            "%(product)s: received quantity corrected from %(before)s to "
            "%(after)s. Receipt %(picking)s was updated to match.",
            product=self.product_id.display_name,
            before=float_repr(previous, precision),
            after=float_repr(self.quantity, precision),
            picking=move.picking_id.name or self.arrival_id.receipt_id.name)

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id and not self.price_unit:
            self.price_unit = self.product_id.standard_price
