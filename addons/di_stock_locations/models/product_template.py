import re

from odoo import _, api, fields, models

# Only where goods physically are. The unfiltered quant relation also carries
# the virtual locations, so a variant with 4 on hand also has an "Inventory
# adjustment -4" row against it, which reads like a bug to anybody who isn't
# an accountant.
INTERNAL_ONLY = [('location_id.usage', '=', 'internal')]

# Same two attribute names di_tag_export prints on the cartellino. Real
# product.attribute records, not Studio fields - every product has an
# attribute LINE of its own, or none, so nothing here needs the defensive
# "does this model exist" checks the x_studio_* taxonomy fields require.
COLOUR_ATTRIBUTE = 'Colore'
SIZE_ATTRIBUTE = 'Taglia'

# A run of digits, optionally with one decimal point: '44', '52.5'. Sizes
# that look like this sort numerically; anything else sorts as text. There is
# no curated garment order to read instead - product.attribute.value.sequence
# is 0 (unset) for every Taglia value on this database.
_NUMERIC_SIZE = re.compile(r'^\d+(\.\d+)?$')


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Anchor for the Colour/Size grid widget - stores nothing, like
    # di_stock_replenish.comparison. The widget reads and writes nothing
    # through this field; it exists so <field widget="..."/> has something
    # to attach to and so the widget's own component can reach
    # props.record.resId for this template.
    di_color_size_matrix = fields.Char(
        string="Colour / Size Matrix", readonly=True)

    @api.model
    def _di_matrix_warehouse_domain(self):
        """Warehouses this grid will ever show a row for: every one of this
        company's own `stock.warehouse` records - all 22, as Configuration >
        Warehouses lists them - never anything more.

        An earlier version of this method tried to narrow that further, to
        only warehouses holding stock somewhere right now. That solved a
        problem Dali did not actually have: checked directly, 21 of the 22
        warehouses hold stock of SOMETHING company-wide, so the filter would
        have removed exactly one (MAG - LECCE) and changed almost nothing.
        What Dali actually asked for was narrower and simpler: show real
        Odoo warehouses, all of them, and never one of the old software's
        own that is not real here - its Filiale dropdown named a store,
        GIORDANO BRUNO, this Odoo does not have at all. That is what this
        domain does: `company_id` only. Nothing about a warehouse's own
        stock activity narrows this list.
        """
        return [('company_id', '=', self.env.company.id)]

    @api.model
    def di_matrix_available_warehouses(self):
        """Warehouses for the grid's Filiale filter - see
        _di_matrix_warehouse_domain for which ones qualify."""
        warehouses = self.env['stock.warehouse'].search(
            self._di_matrix_warehouse_domain(), order='name')
        return [{'id': w.id, 'name': w.name} for w in warehouses]

    def get_color_size_matrix(self, warehouse_id=None):
        """Everything the grid draws for this template, in one call.

        Reproduces the shop's previous management software's "Esistenza per
        colori articolo" screen for this one product: every colour down the
        side, every size across the top, and - under each colour - one row
        per warehouse in the company, WITH zero rows for shops that carry
        none of that colour. That is deliberately more than
        di_internal_quant_ids on product.product shows (which only lists
        locations that actually hold stock): this answers "which shops carry
        this colour at all", not just "where does stock currently sit", and
        an absent row cannot say no.

        ``warehouse_id`` narrows the location rows to a single Filiale;
        ``None`` (the old screen's "0 Tutti") keeps every warehouse.
        """
        self.ensure_one()
        variants = self.product_variant_ids
        colours = self._di_matrix_axis(variants, COLOUR_ATTRIBUTE)
        sizes = self._di_matrix_axis(variants, SIZE_ATTRIBUTE)

        warehouses = self.env['stock.warehouse'].search(
            self._di_matrix_warehouse_domain()
            + ([('id', '=', int(warehouse_id))] if warehouse_id else []),
            order='name',
        )

        # variant -> (colour, size), for every variant that actually has
        # both axes; a product missing either attribute contributes nothing
        # to the grid rather than a malformed row.
        variant_axes = {}
        for variant in variants:
            colour = self._di_matrix_value(variant, COLOUR_ATTRIBUTE)
            size = self._di_matrix_value(variant, SIZE_ATTRIBUTE)
            if colour and size:
                variant_axes[variant.id] = (colour, size)

        quants = self.env['stock.quant'].search(
            [('product_id', 'in', list(variant_axes.keys())),
             ('warehouse_id', 'in', warehouses.ids)] + INTERNAL_ONLY,
        )

        # {(colour, warehouse_id): {size: qty}}
        by_row = {}
        for quant in quants:
            axes = variant_axes.get(quant.product_id.id)
            if not axes:
                continue
            colour, size = axes
            row = by_row.setdefault((colour, quant.warehouse_id.id), {})
            row[size] = row.get(size, 0.0) + quant.quantity

        totals = {colour: {} for colour in colours}
        breakdown = {colour: [] for colour in colours}
        for colour in colours:
            for warehouse in warehouses:
                cells = by_row.get((colour, warehouse.id), {})
                row_total = sum(cells.get(size, 0.0) for size in sizes)
                breakdown[colour].append({
                    'warehouse': warehouse.name,
                    'cells': {size: cells.get(size, 0.0) for size in sizes},
                    'total': row_total,
                })
                for size in sizes:
                    totals[colour][size] = (
                        totals[colour].get(size, 0.0) + cells.get(size, 0.0))
            totals[colour]['__total__'] = sum(
                totals[colour].get(size, 0.0) for size in sizes)

        size_totals = {
            size: sum(totals[colour].get(size, 0.0) for colour in colours)
            for size in sizes
        }
        size_totals['__total__'] = sum(size_totals.values())

        return {
            'product': {'name': self.name, 'code': self.default_code or ''},
            'colours': colours,
            'sizes': sizes,
            'totals': totals,
            'breakdown': breakdown,
            'size_totals': size_totals,
            'warehouses': [{'id': w.id, 'name': w.name} for w in warehouses],
            'labels': {
                'colour': self._di_matrix_attribute_label(COLOUR_ATTRIBUTE, _("Colour")),
                'size': self._di_matrix_attribute_label(SIZE_ATTRIBUTE, _("Size")),
            },
        }

    @api.model
    def _di_matrix_attribute_label(self, attribute_name, default):
        """The column heading for one axis: the real product.attribute's own
        name - "Colore", in whatever language it happens to have been
        entered or translated in - not a word this module invents. Falls
        back to a translated generic default only if no such attribute
        exists at all, which also means the grid itself has nothing to show
        for it."""
        attribute = self.env['product.attribute'].search(
            [('name', '=', attribute_name)], limit=1)
        return attribute.name if attribute else default

    @api.model
    def _di_matrix_value(self, variant, attribute_name):
        """This variant's value on one attribute, or '' if it has none -
        same lookup di_tag_export._tag_attribute uses for the cartellino.

        Read from ``product_template_attribute_value_ids``, never from
        ``product_template_variant_value_ids``: the latter drops every
        attribute that has only ONE value on the template, so a product
        made in a single colour would come back with no colour at all and
        the grid would wrongly report it has nothing to show - about half
        this catalogue is single-colour or single-size."""
        for ptav in variant.product_template_attribute_value_ids:
            if ptav.attribute_id.name == attribute_name:
                return ptav.name or ''
        return ''

    @api.model
    def _di_matrix_axis(self, variants, attribute_name):
        """Every value this product's variants take on one attribute,
        ordered numerically where the values are numbers, alphabetically
        otherwise - see _NUMERIC_SIZE."""
        seen = {
            self._di_matrix_value(variant, attribute_name)
            for variant in variants
        }
        seen.discard('')
        return sorted(seen, key=self._di_matrix_sort_key)

    @staticmethod
    def _di_matrix_sort_key(value):
        if _NUMERIC_SIZE.match(value):
            return (0, float(value), '')
        return (1, 0.0, value)
