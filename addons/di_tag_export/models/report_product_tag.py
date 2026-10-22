from odoo import api, models

# Labels per HTML document, and per wkhtmltopdf run. Two limits meet here.
# Odoo turns every ``div.article`` into a document of its own, each loading
# the whole report stylesheet bundle, so one article per label made a
# 4,500-label arrival a 4,500-document job that never finished. And one run
# holds every page it renders in memory - each label's barcode alone decodes
# to some 380 KB - while it inherits the Odoo worker's memory cap, so 4,500
# labels in a single run were killed outright (error -11). A large print is
# therefore cut into runs of this many labels, one document each, and the
# PDFs joined afterwards (see ``ir_actions_report.py``).
TAGS_PER_DOCUMENT = 250


class ReportProductTag(models.AbstractModel):
    _name = 'report.di_tag_export.report_product_tag'
    _description = "Product Tag Report"

    @api.model
    def _get_report_values(self, docids, data=None):
        products, tags = self._tag_sequence(docids, data)
        return self._tag_values(docids, products, tags)

    @api.model
    def _tag_sequence(self, docids, data=None):
        """Decide how many labels to print, and in what order.

        ``data['tag_quantities']`` maps a product id to the number of labels
        that variant needs - three pieces of the same size and colour want
        three identical tags, not one. A caller that passes nothing (printing
        straight from the product list, say) still gets exactly one label per
        variant, which is what this report has always done.

        ``data['tag_lines']`` is the stronger form: a list of
        ``[product id, count]`` in the exact order the labels should come off
        the printer. A caller that knows how the pile will be picked - which
        warehouse each piece is walked out of, say - can express that here,
        and a variant taken from two warehouses appears twice, once per pile.
        Given, it is trusted as given: sorting it again by barcode would undo
        the very grouping it exists to express.

        An entry of ``tag_lines`` may also be ``{'separator': text}``
        (optionally with ``'count'``): it prints one blank card carrying
        that text where it sits in the sequence, so a stack of labels for
        several shops comes off the printer already divided - the worker
        cuts the pile at the card that names the next shop.

        Returns ``(products, tags)``: ``tags`` holds one entry per label,
        each either ``{'product': record}`` or
        ``{'separator': text, 'count': n}``.

        ``docids`` arrives empty whenever the action carries data: the web
        client then builds the report URL with the options and the context
        only, leaving the ids in ``active_ids``. Browsing nothing rendered a
        single blank page, so the context is the fallback.
        """
        Product = self.env['product.product']
        products = Product.browse(docids or [])
        if not products:
            products = Product.browse(
                self.env.context.get('active_ids') or [])

        ordered = (data or {}).get('tag_lines')
        if ordered:
            return products, self._tags_from_lines(ordered)

        quantities = (data or {}).get('tag_quantities') or {}
        tags = []
        # No warehouse here - the product list, or a caller passing only
        # quantities - so the general order applies: see tag_order.py.
        for product in products.sorted(key=lambda p: p._di_tag_order_key()):
            # The action's data survives a JSON round trip to the browser and
            # back, which turns the integer keys into strings.
            count = quantities.get(str(product.id), quantities.get(product.id, 1))
            tags.extend([{'product': product}] * max(int(count or 1), 1))

        return products, tags

    def _tag_values(self, docids, products, tags):
        """Attach each variant's printed strings and cut the documents.

        The template gets ``tags`` with ``'tag'`` added to every product
        entry: the printed strings, worked out once per variant rather than
        once per label - ten pieces of the same size and colour price the
        same and carry the same barcode image. ``sheets`` cuts the sequence
        into the documents wkhtmltopdf renders (see ``TAGS_PER_DOCUMENT``).
        """
        strings = {}
        for entry in tags:
            product = entry.get('product')
            if product is None:
                continue
            if product.id not in strings:
                strings[product.id] = product._get_tag_data()
            entry['tag'] = strings[product.id]
        return {
            'doc_ids': docids,
            'doc_model': 'product.product',
            'docs': products,
            'tags': tags,
            'sheets': [tags[start:start + TAGS_PER_DOCUMENT]
                       for start in range(0, len(tags), TAGS_PER_DOCUMENT)],
        }

    @api.model
    def _tag_line_batches(self, docids, data=None):
        """The labels of one print, cut into runs of ``TAGS_PER_DOCUMENT``.

        Each run is expressed as ``tag_lines`` - the form this report already
        takes as given - so rendering a run is simply rendering this report
        with that run as its data. Consecutive labels of the same variant are
        folded back into one ``[product id, count]``, and a separator card
        counts as a label, since it is a page like any other.
        """
        _products, tags = self._tag_sequence(docids, data)
        batches = []
        for start in range(0, len(tags), TAGS_PER_DOCUMENT):
            lines = []
            for entry in tags[start:start + TAGS_PER_DOCUMENT]:
                if 'separator' in entry:
                    lines.append({'separator': entry['separator'],
                                  'count': entry.get('count')})
                elif (lines and isinstance(lines[-1], list)
                        and lines[-1][0] == entry['product'].id):
                    lines[-1][1] += 1
                else:
                    lines.append([entry['product'].id, 1])
            batches.append(lines)
        return batches

    def _tags_from_lines(self, ordered):
        """Expand ``[[product id, count], ...]`` into one entry per label,
        keeping any separator card where the caller put it.

        Every id is browsed in a single query and checked against what came
        back: the list arrives from a caller across a JSON round trip, so a
        deleted product must drop out rather than render a blank tag.
        """
        Product = self.env['product.product']
        wanted = []
        for entry in ordered:
            if isinstance(entry, dict):
                text = str(entry.get('separator') or '').strip()
                if text:
                    wanted.append(('separator', text, entry.get('count')))
                continue
            try:
                wanted.append(('product', int(entry[0]), int(entry[1] or 1)))
            except (TypeError, ValueError, IndexError):
                continue
        by_id = {
            product.id: product
            for product in Product.browse(
                [pid for kind, pid, _c in wanted if kind == 'product']
            ).exists()
        }
        tags = []
        for kind, value, count in wanted:
            if kind == 'separator':
                tags.append({'separator': value, 'count': count})
                continue
            product = by_id.get(value)
            if product:
                tags.extend([{'product': product}] * max(count, 1))
        return tags
