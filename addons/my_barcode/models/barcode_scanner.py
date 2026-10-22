from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError

OPEN_PICKING_STATES = ("confirmed", "assigned")


class BarcodeScanner(models.AbstractModel):
    """Server API used by the Barcode Scanner client action.

    Every method runs with the current user's access rights, so the scanner
    can never do more than the user could do from the regular Inventory app.
    """

    _name = "my_barcode.scanner"
    _description = "Barcode Scanner API"

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @api.model
    def _find_product(self, barcode):
        Product = self.env["product.product"]
        return (
            Product.search([("barcode", "=", barcode)], limit=1)
            or Product.search([("default_code", "=", barcode)], limit=1)
        )

    @api.model
    def _browse_product(self, product_id):
        product = self.env["product.product"].browse(product_id).exists()
        if not product:
            raise UserError(_("This product no longer exists."))
        return product

    @api.model
    def _check_scannable(self, product, for_count=False):
        if for_count and not product.is_storable:
            raise UserError(_(
                "%s doesn't track inventory, so it can't be counted.",
                product.display_name,
            ))
        if product.tracking != "none":
            raise UserError(_(
                "%s is tracked by lots/serial numbers, which the scanner doesn't "
                "support yet. Please process it from the Inventory app.",
                product.display_name,
            ))

    @api.model
    def _get_open_picking(self, picking_id):
        picking = self.env["stock.picking"].browse(picking_id).exists()
        if not picking:
            raise UserError(_("This transfer no longer exists."))
        if picking.state not in OPEN_PICKING_STATES:
            raise UserError(_("%s is not ready to be processed.", picking.name))
        return picking

    @api.model
    def _selection_label(self, record, field_name):
        return dict(record._fields[field_name]._description_selection(self.env)).get(record[field_name], "")

    # -------------------------------------------------------------------------
    # Generic scan & product lookup
    # -------------------------------------------------------------------------

    @api.model
    def scan(self, barcode):
        """Tell what a scanned barcode refers to: product, location or transfer."""
        barcode = (barcode or "").strip()
        product = self._find_product(barcode)
        if product:
            return {"type": "product", "id": product.id}
        location = self.env["stock.location"].search(
            [("barcode", "=", barcode), ("usage", "=", "internal")], limit=1)
        if location:
            return {"type": "location", "id": location.id}
        picking = self.env["stock.picking"].search([("name", "=", barcode)], limit=1)
        if picking:
            return {"type": "picking", "id": picking.id}
        return {"type": "unknown", "barcode": barcode}

    @api.model
    def product_info(self, product_id):
        product = self._browse_product(product_id)
        quants = self.env["stock.quant"].search([
            ("product_id", "=", product.id),
            ("location_id.usage", "=", "internal"),
        ])
        qty_by_location = defaultdict(float)
        for quant in quants:
            qty_by_location[quant.location_id] += quant.quantity
        locations = [
            {"id": location.id, "name": location.complete_name, "quantity": qty}
            for location, qty in sorted(qty_by_location.items(), key=lambda item: item[0].complete_name)
            if not product.uom_id.is_zero(qty)
        ]
        return {
            "id": product.id,
            "display_name": product.display_name,
            "default_code": product.default_code or "",
            "barcode": product.barcode or "",
            "list_price": product.lst_price,
            "currency_id": product.currency_id.id,
            "uom": product.uom_id.name,
            "is_storable": product.is_storable,
            "tracking": product.tracking,
            "qty_available": product.qty_available,
            "virtual_available": product.virtual_available,
            "locations": locations,
        }

    # -------------------------------------------------------------------------
    # Inventory count (uses the standard "counted quantity" of quants, so a
    # count in progress is also visible in Inventory > Physical Inventory)
    # -------------------------------------------------------------------------

    @api.model
    def count_locations(self):
        locations = self.env["stock.location"].search([("usage", "=", "internal")], order="complete_name")
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        return {
            "locations": [{"id": loc.id, "name": loc.complete_name} for loc in locations],
            "default_id": warehouse.lot_stock_id.id or locations[:1].id,
        }

    @api.model
    def _count_quants(self, location_id):
        return self.env["stock.quant"].search([
            ("location_id", "=", location_id),
            ("inventory_quantity_set", "=", True),
            ("user_id", "=", self.env.uid),
        ], order="write_date desc, id desc")

    @api.model
    def count_lines(self, location_id):
        return [{
            "quant_id": quant.id,
            "product_id": quant.product_id.id,
            "product_name": quant.product_id.display_name,
            "default_code": quant.product_id.default_code or "",
            "counted": quant.inventory_quantity,
            "on_hand": quant.quantity,
            "uom": quant.product_uom_id.name,
        } for quant in self._count_quants(location_id)]

    @api.model
    def count_add(self, location_id, product_id, quantity=1.0, absolute=False):
        """Add ``quantity`` to the counted quantity (or set it if ``absolute``)."""
        product = self._browse_product(product_id)
        self._check_scannable(product, for_count=True)
        location = self.env["stock.location"].browse(location_id)
        Quant = self.env["stock.quant"].with_context(inventory_mode=True)
        quant = Quant._gather(product, location, strict=True).filtered(
            lambda q: not q.lot_id and not q.package_id and not q.owner_id)[:1]
        if quant:
            current = quant.inventory_quantity if quant.inventory_quantity_set else 0.0
            new_quantity = quantity if absolute else current + quantity
            quant.write({
                "inventory_quantity": max(new_quantity, 0.0),
                "inventory_quantity_set": True,
                "user_id": self.env.uid,
            })
        else:
            quant = Quant.create({
                "product_id": product.id,
                "location_id": location.id,
                "inventory_quantity": max(quantity, 0.0),
            })
            quant.write({"inventory_quantity_set": True, "user_id": self.env.uid})
        return self.count_lines(location_id)

    @api.model
    def count_remove(self, location_id, quant_id):
        quant = self._count_quants(location_id).filtered(lambda q: q.id == quant_id)
        quant.with_context(inventory_mode=True).action_clear_inventory_quantity()
        return self.count_lines(location_id)

    @api.model
    def count_discard(self, location_id):
        self._count_quants(location_id).with_context(inventory_mode=True).action_clear_inventory_quantity()
        return self.count_lines(location_id)

    @api.model
    def count_apply(self, location_id):
        quants = self._count_quants(location_id)
        if not quants:
            raise UserError(_("Nothing has been counted in this location yet."))
        result = quants.with_context(inventory_mode=True).action_apply_inventory()
        return result if isinstance(result, dict) else True

    # -------------------------------------------------------------------------
    # Transfers (receipts, deliveries, internal)
    # -------------------------------------------------------------------------

    @api.model
    def picking_list(self, picking_type_code=False):
        domain = [("state", "in", OPEN_PICKING_STATES)]
        if picking_type_code:
            domain.append(("picking_type_code", "=", picking_type_code))
        pickings = self.env["stock.picking"].search(domain, order="scheduled_date asc, id asc", limit=80)
        return [{
            "id": picking.id,
            "name": picking.name,
            "partner": picking.partner_id.display_name or "",
            "origin": picking.origin or "",
            "type_name": picking.picking_type_id.display_name,
            "scheduled_date": fields.Datetime.to_string(picking.scheduled_date),
            "state": picking.state,
            "state_label": self._selection_label(picking, "state"),
        } for picking in pickings]

    @api.model
    def picking_data(self, picking_id):
        picking = self.env["stock.picking"].browse(picking_id).exists()
        if not picking:
            raise UserError(_("This transfer no longer exists."))
        is_open = picking.state in OPEN_PICKING_STATES
        moves = picking.move_ids.filtered(lambda m: m.state != "cancel")
        return {
            "id": picking.id,
            "name": picking.name,
            "state": picking.state,
            "state_label": self._selection_label(picking, "state"),
            "is_open": is_open,
            "type_code": picking.picking_type_code,
            "type_name": picking.picking_type_id.display_name,
            "partner": picking.partner_id.display_name or "",
            "origin": picking.origin or "",
            "location_src": picking.location_id.complete_name,
            "location_dest": picking.location_dest_id.complete_name,
            "lines": [{
                "move_id": move.id,
                "product_id": move.product_id.id,
                "product_name": move.product_id.display_name,
                "default_code": move.product_id.default_code or "",
                "barcode": move.product_id.barcode or "",
                "uom": move.product_uom.name,
                "demand": move.product_uom_qty,
                "scanned": move.barcode_qty_scanned if is_open else move.quantity,
                "tracking": move.product_id.tracking,
            } for move in moves],
        }

    @api.model
    def picking_scan(self, picking_id, barcode):
        picking = self._get_open_picking(picking_id)
        barcode = (barcode or "").strip()
        product = self._find_product(barcode)
        if not product:
            other = self.scan(barcode)
            if other["type"] == "picking":
                return {"switch_picking": other["id"]}
            raise UserError(_("No product found for barcode %s.", barcode))
        self._check_scannable(product)

        moves = picking.move_ids.filtered(
            lambda m: m.product_id == product and m.state not in ("done", "cancel"))
        move = next(
            (m for m in moves if m.product_uom.compare(m.barcode_qty_scanned, m.product_uom_qty) < 0),
            moves[:1],
        )
        extra = not move
        if extra:
            move = self.env["stock.move"].create({
                "picking_id": picking.id,
                "product_id": product.id,
                "product_uom_qty": 1.0,
                "product_uom": product.uom_id.id,
                "location_id": picking.location_id.id,
                "location_dest_id": picking.location_dest_id.id,
                "picking_type_id": picking.picking_type_id.id,
                "company_id": picking.company_id.id,
            })
            move = move._action_confirm()[:1] or move
        move.barcode_qty_scanned += 1.0
        return {
            "picking": self.picking_data(picking.id),
            "move_id": move.id,
            "product_name": product.display_name,
            "extra": extra,
            "over": move.product_uom.compare(move.barcode_qty_scanned, move.product_uom_qty) > 0,
        }

    @api.model
    def picking_set_qty(self, move_id, quantity):
        move = self.env["stock.move"].browse(move_id).exists()
        if not move:
            raise UserError(_("This line no longer exists."))
        self._get_open_picking(move.picking_id.id)
        move.barcode_qty_scanned = max(quantity, 0.0)
        return self.picking_data(move.picking_id.id)

    @api.model
    def picking_validate(self, picking_id):
        picking = self._get_open_picking(picking_id)
        moves = picking.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        if all(move.product_uom.is_zero(move.barcode_qty_scanned) for move in moves):
            raise UserError(_("Scan at least one product before validating."))
        for move in moves:
            move.quantity = move.barcode_qty_scanned
        moves.picked = True
        result = picking.button_validate()
        return result if isinstance(result, dict) else True

    # -------------------------------------------------------------------------
    # Barcode assignment
    # -------------------------------------------------------------------------

    @api.model
    def assign_candidates(self, search="", only_missing=True, limit=40):
        domain = [("type", "=", "consu")]
        if only_missing:
            domain.append(("barcode", "=", False))
        if search:
            domain += ["|", "|",
                       ("name", "ilike", search),
                       ("default_code", "ilike", search),
                       ("barcode", "ilike", search)]
        products = self.env["product.product"].search(domain, limit=limit, order="name, id")
        return [{
            "id": product.id,
            "display_name": product.display_name,
            "default_code": product.default_code or "",
            "barcode": product.barcode or "",
        } for product in products]

    @api.model
    def assign_barcode(self, product_id, barcode):
        barcode = (barcode or "").strip()
        if not barcode:
            raise UserError(_("The barcode is empty."))
        product = self._browse_product(product_id)
        other = self.env["product.product"].with_context(active_test=False).search(
            [("barcode", "=", barcode), ("id", "!=", product.id)], limit=1)
        if other:
            raise UserError(_(
                "Barcode %(barcode)s is already used by %(product)s.",
                barcode=barcode, product=other.display_name,
            ))
        product.barcode = barcode
        return {"id": product.id, "display_name": product.display_name, "barcode": barcode}
