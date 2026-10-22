from odoo import api, models

# The order cartellini come off the printer, for every screen that prints
# them: the sales run, the stock comparison, an arrival and the product list.
# One copy here, so the screens can never drift apart.
#
# A print is cut into piles, one per source warehouse a pile is walked out of
# (and per shop, where it goes to several). Every pile is sorted on its own -
# a low barcode in one never pulls a label forward in another. Inside a pile
# each step below is matched in turn and the first that fits wins; inside a
# step, and among whatever fits none, labels run by ascending barcode.
#
# By category, as (root, child, posizionamento), None matching anything.
# ACCE / BORS comes before ACCE SCA, which would otherwise take the bags -
# every BORS at ING - CONTESSE is SCA. This is the general rule: any
# warehouse not named below, and a print with no warehouse at all (the
# product list), uses it.
TAG_STEPS = (
    ('ABBI', None, 'SCA'),
    ('ACCE', 'BORS', None),
    ('ABBI', None, 'APP'),
    ('ACCE', None, 'SCA'),
)

# Warehouses walked by department instead, keyed by warehouse code. MAG -
# CENTRALE PISTUNINA is almost all ABBI APP, shelved Donna then Uomo. Reparto
# values as they are on this database; B/O, UNI and the rest follow.
TAG_STEPS_BY_REPARTO = {
    'MCP': (('D', 'DN', 'D+', 'DO'), ('U', 'U+')),
}

# Studio fields, read by name and only where they exist: this module depends
# on `product` alone, and a database without the taxonomy still prints.
POSITION_FIELD = 'x_prodotti2posizionamento'
REPARTO_FIELD = 'x_studio_prodotti2reparto'


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @staticmethod
    def _di_tag_barcode_key(product):
        """Barcode ascending, compared as a number where it is one.

        They are 6, 8 and 13 digits long on this database, so as text "9"
        follows "10" and a pile stops matching the order goods are counted
        in. Anything without a barcode sorts last rather than heading it.
        """
        barcode = (product.barcode or '').strip()
        if barcode.isdigit():
            return (0, int(barcode), '')
        return (1, 0, barcode)

    def _di_tag_taxonomy(self):
        """``(root category, child category, posizionamento, reparto)``,
        upper-cased, '' for anything missing."""
        self.ensure_one()
        template = self.product_tmpl_id
        category = self.categ_id
        root = category
        while root.parent_id:
            root = root.parent_id
        # A root category has no child: "ACCE" is not the same as ACCE/BORS.
        child = category.name if category.parent_id else ''
        position = reparto = ''
        if POSITION_FIELD in template._fields:
            position = template[POSITION_FIELD].x_name or ''
        if REPARTO_FIELD in template._fields:
            reparto = template[REPARTO_FIELD].x_name or ''
        return tuple(
            (value or '').strip().upper()
            for value in (root.name, child, position, reparto))

    def _di_tag_rank(self, warehouse_code=None):
        """Which step of ``warehouse_code``'s order this variant falls in."""
        self.ensure_one()
        root, child, position, reparto = self._di_tag_taxonomy()
        by_reparto = TAG_STEPS_BY_REPARTO.get(warehouse_code or '')
        if by_reparto is not None:
            for index, names in enumerate(by_reparto):
                if reparto in names:
                    return index
            return len(by_reparto)
        for index, (want_root, want_child, want_position) in enumerate(TAG_STEPS):
            if (root == want_root
                    and (want_child is None or child == want_child)
                    and (want_position is None or position == want_position)):
                return index
        return len(TAG_STEPS)

    def _di_tag_order_key(self, warehouse_code=None):
        """Sort key for one label inside a pile walked out of
        ``warehouse_code``: the step, then the barcode, then the id so the
        order is total and a reprint comes out identical."""
        self.ensure_one()
        return (self._di_tag_rank(warehouse_code),
                self._di_tag_barcode_key(self), self.id)

    @api.model
    def _di_tag_lines(self, piles):
        """The ``tag_lines`` the cartellino report prints, from piles.

        ``piles`` is a list of ``(shop, warehouse, entries)`` in the order the
        piles should print: ``shop`` and ``warehouse`` are display names (or
        False where the print has none), ``warehouse`` may also be a
        ``stock.warehouse``-like record with ``code`` and ``name``, and
        ``entries`` is ``[(product, pieces), ...]`` in any order. Each pile is
        sorted with its own warehouse's rule; a variant listed twice in one
        pile keeps both entries, side by side.

        When there is more than one pile, each opens with a blank card naming
        it - the warehouse, the shop, or both when both vary - with its piece
        count, so the stack is cut without reading a label. A single pile
        prints no card.
        """
        cleaned = []
        for shop, warehouse, entries in piles:
            code = getattr(warehouse, 'code', None) or None
            name = getattr(warehouse, 'name', warehouse) or ''
            labels = [(product, int(pieces)) for product, pieces in entries
                      if product and int(pieces) > 0]
            if labels:
                labels.sort(key=lambda entry: entry[0]._di_tag_order_key(code))
                cleaned.append((shop or '', name, labels))

        several_shops = len({shop for shop, _name, _labels in cleaned}) > 1
        several_warehouses = len({name for _shop, name, _labels in cleaned}) > 1
        lines = []
        for shop, name, labels in cleaned:
            if len(cleaned) > 1:
                if several_shops and several_warehouses:
                    text = '%s - %s' % (shop, name) if shop and name else shop or name
                elif several_shops:
                    text = shop or name
                else:
                    text = name or shop
                lines.append({
                    'separator': text,
                    'count': sum(pieces for _product, pieces in labels),
                })
            lines.extend([str(product.id), pieces] for product, pieces in labels)
        return lines
