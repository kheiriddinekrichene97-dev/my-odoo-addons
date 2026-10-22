import base64
import csv
import datetime
import io
import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _norm(value):
    """Normalise a header or lookup key: lowercase, trimmed, no separators."""
    return (value or '').strip().lower().replace(' ', '').replace('_', '')


# Files are recognised by their first bytes rather than by their extension: a
# worker renaming a spreadsheet to .csv is exactly the mistake this has to
# survive. XLSX is a zip; XLS is an OLE2 compound document.
XLSX_MAGIC = b'PK\x03\x04'
XLS_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


# Accepted column headers, mapped to their internal meaning. Both Italian and
# English spellings are accepted so the CSV template can be written in either.
COLUMN_ALIASES = {
    'code': ('codice', 'codart', 'code', 'internalreference', 'riferimentointerno', 'defaultcode'),
    'name': ('nome', 'descrizione', 'name', 'description', 'productname'),
    'quantity': ('quantita', 'quantità', 'qta', 'qty', 'quantity', 'pezzi'),
    # Four prices, mirroring the four ESC listini:
    #   ACQUISTO (4)         -> cost
    #   VENDITA (2)          -> sale price + Dettaglio pricelist
    #   VENDITA SCONTATO (1) -> Promo + Promo Franchising pricelists
    #   VENDITA INGROSSO (3) -> Ingrosso pricelist
    'price': ('prezzoacquisto', 'costo', 'cost', 'purchaseprice', 'priceunit', 'prezzo'),
    'sale_price': ('prezzodettaglio', 'prezzovendita', 'dettaglio', 'listino',
                   'saleprice', 'listprice'),
    'promo_price': ('prezzopromo', 'promo', 'prezzoscontato', 'promoprice'),
    'wholesale_price': ('prezzoingrosso', 'ingrosso', 'wholesaleprice'),
    'category': ('categoria', 'category', 'categ'),
    'color': ('colore', 'color', 'colour'),
    'size': ('taglia', 'size'),
    'brand': ('marca', 'brand'),
    'season': ('stagione', 'season'),
    'department': ('reparto', 'department'),
    'positioning': ('posizionamento', 'positioning'),
    'barcode': ('barcode', 'ean', 'codicebarre'),
}

# Studio taxonomy fields on product.template, and the model each points at.
# Looked up defensively: the module must install on a database that has not
# had these Studio customisations applied.
TAXONOMY_FIELDS = {
    'brand': ('x_studio_Prodotti2Marca', 'x_marche'),
    'season': ('x_studio_prodotti2stagione', 'x_stagioni'),
    'department': ('x_studio_prodotti2reparto', 'x_reparto'),
    'positioning': ('x_prodotti2posizionamento', 'x_posizionamenti'),
}

COLOR_ATTRIBUTE = 'Colore'
SIZE_ATTRIBUTE = 'Taglia'

# How many articles the chatter summary of already-known variants names. The
# full variant-by-warehouse grid is in the dialog shown after the import; the
# chatter keeps one line per article so a reassortment of hundreds of
# variants still reads at a glance.
EXISTING_REPORT_LIMIT = 30

# CSV price column -> the pricelists it feeds. ``sale_price`` also lands on
# product.template.list_price. Matched by name because the pricelists are
# customer records, not module data; a pricelist that is absent is skipped.
PRICELIST_TARGETS = {
    'sale_price': ('Dettaglio',),
    'promo_price': ('Promo', 'Promo Franchising'),
    'wholesale_price': ('Ingrosso',),
}


class StockArrivalImport(models.TransientModel):
    _name = 'di.stock.arrival.import'
    _description = "Import Arrival Lines from a CSV or XLSX File"

    arrival_id = fields.Many2one(
        'di.stock.arrival', string="Arrival", required=True,
        ondelete='cascade')
    file = fields.Binary(string="File", required=True,
                         help="A .csv or .xlsx file. The first sheet of a "
                              "workbook is read; its first row is the header.")
    filename = fields.Char(string="File Name")
    is_csv = fields.Boolean(
        string="Is CSV", compute='_compute_is_csv',
        help="Separator and encoding only apply to CSV; a workbook carries "
             "its own.")
    delimiter = fields.Selection(
        [(',', "Comma  ,"), (';', "Semicolon  ;"), ('\t', "Tab")],
        string="Separator", default=';', required=True,
        help="Excel in an Italian locale saves CSV with semicolons.")
    encoding = fields.Selection(
        [('utf-8-sig', "UTF-8"), ('latin-1', "Latin-1 / Windows")],
        string="Encoding", default='utf-8-sig', required=True)
    create_missing_products = fields.Boolean(
        string="Create Missing Products", default=True,
        help="Create products and variants that do not exist yet. Turn this "
             "off to import only what is already in the catalogue and be "
             "told what was skipped.")
    update_prices = fields.Boolean(
        string="Update Prices", default=True,
        help="Write the cost, the sale price and the Dettaglio / Promo / "
             "Promo Franchising / Ingrosso pricelist rules from the file. "
             "Turn off to leave existing prices untouched.")
    # Filled by the import, for the dialog that follows it: the variants the
    # catalogue already held, and where they are in stock.
    existing_html = fields.Html(string="Stock of Known Variants",
                                sanitize=False, readonly=True)

    @api.depends('filename')
    def _compute_is_csv(self):
        """Whether the separator and encoding options still mean anything.

        Driven by the file name because it is known as soon as the file is
        picked, without decoding the upload on every form onchange. The
        import itself trusts the file's own bytes, not this.
        """
        for wizard in self:
            name = (wizard.filename or '').lower()
            wizard.is_csv = not name.endswith(('.xlsx', '.xlsm', '.xls'))

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def action_import(self):
        self.ensure_one()
        arrival = self.arrival_id
        if arrival.state != 'draft':
            raise UserError(_(
                "Lines can only be imported into a draft arrival."))

        rows, headers = self._read_file()
        if not rows:
            raise UserError(_("The file contains no data rows."))

        mapping = self._map_columns(headers)
        if 'code' not in mapping:
            raise UserError(_(
                "The file needs a product code column (one of: %s).",
                ", ".join(COLUMN_ALIASES['code'])))
        if 'quantity' not in mapping:
            raise UserError(_(
                "The file needs a quantity column (one of: %s).",
                ", ".join(COLUMN_ALIASES['quantity'])))
        warehouse_columns = self._map_warehouse_columns(headers, mapping)

        created, updated, skipped, warnings, existing = self._import_rows(
            rows, mapping, warehouse_columns)
        articles = self._existing_by_article(existing)

        arrival.message_post(body=self._format_report(
            created, updated, skipped, warnings, articles))

        if not articles:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'di.stock.arrival',
                'res_id': arrival.id,
                'views': [(False, 'form')],
                'target': 'current',
            }

        # Show the worker what was already there, in this same dialog. The
        # arrival form behind it reloads when it is closed: Odoo hands the
        # import dialog's close handler on to the dialog that replaces it.
        # The arrival keeps the grid in its own tab, so it outlives the
        # dialog. A later import with nothing already known leaves it as is.
        self.existing_html = arrival.existing_html = self._existing_grid(
            articles)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Already in the Catalogue"),
            'res_model': self._name,
            'res_id': self.id,
            'views': [(self.env.ref(
                'di_stock_arrival.di_stock_arrival_import_view_existing').id,
                'form')],
            'target': 'new',
            'context': {'dialog_size': 'extra-large'},
        }

    # ------------------------------------------------------------------
    # Variants that were already in the catalogue
    # ------------------------------------------------------------------

    def _existing_by_article(self, products):
        """The already-known variants, grouped by article, with their stock.

        Returns one dict per article, in the order the file first names it::

            {'name': '[B-996508] MAGLIA ...', 'variants': [...],
             'stock': {warehouse: qty}, 'total': qty}

        and each variant as ``{'product', 'colour', 'size', 'stock', 'total'}``.
        Stock is summed per warehouse, since that is how the shops are named
        everywhere else, falling back to the location for stock held outside
        a warehouse. Only internal locations count, and only what is actually
        there: a warehouse holding nothing, or less than nothing, is not
        somewhere the article "is".

        Read before the arrival is confirmed, so the quantities are the stock
        that was there before these goods came in.
        """
        if not products:
            return []
        stock = {}
        for product, location, quantity in self.env['stock.quant']._read_group(
                [('product_id', 'in', products.ids),
                 ('location_id.usage', '=', 'internal')],
                ['product_id', 'location_id'], ['quantity:sum']):
            place = location.warehouse_id.name or location.complete_name
            per_place = stock.setdefault(product.id, {})
            per_place[place] = per_place.get(place, 0.0) + quantity

        articles = {}
        for product in products:
            template = product.product_tmpl_id
            article = articles.get(template.id)
            if article is None:
                code = product.default_code or template.default_code
                article = articles[template.id] = {
                    'name': f"[{code}] {template.name}" if code else template.name,
                    'variants': [],
                    'stock': {},
                    'total': 0.0,
                }
            held = {place: quantity
                    for place, quantity in stock.get(product.id, {}).items()
                    if quantity > 0}
            values = {
                ptav.attribute_id.name: ptav.name
                for ptav in product.product_template_attribute_value_ids
            }
            article['variants'].append({
                'product': product,
                'colour': values.get(COLOR_ATTRIBUTE, ''),
                'size': values.get(SIZE_ATTRIBUTE, ''),
                'stock': held,
                'total': sum(held.values()),
            })
            for place, quantity in held.items():
                article['stock'][place] = article['stock'].get(place, 0.0) + quantity
            article['total'] += sum(held.values())
        return list(articles.values())

    @staticmethod
    def _format_quantity(quantity):
        return str(int(quantity)) if quantity == int(quantity) else str(quantity)

    def _existing_grid(self, articles):
        """The dialog's grid: variants down, warehouses across.

        Laid out like the Distribution tab the worker already knows - one
        column per warehouse, the arrival's own first - with a heading row
        per article carrying its subtotals. Only warehouses that hold some of
        these variants get a column, so the grid is as narrow as the answer.
        Empty cells are a faint dot rather than a 0, so the numbers that
        matter are the only ones that stand out.
        """
        home = self.arrival_id.warehouse_id.name
        places = sorted(
            {place for article in articles for place in article['stock']},
            key=lambda place: (place != home, place))

        def cells(stock, total):
            out = Markup("").join(
                Markup('<td class="di_qty">%s</td>')
                % self._format_quantity(stock[place])
                if stock.get(place) else
                Markup('<td class="di_qty di_empty">&middot;</td>')
                for place in places)
            if total:
                return out + Markup('<td class="di_qty di_total">%s</td>') % (
                    self._format_quantity(total))
            return out + Markup(
                '<td class="di_qty di_total di_none">%s</td>') % _("none")

        head = Markup("").join(
            Markup('<th class="di_qty%s">%s</th>') % (
                Markup(" di_home") if place == home else Markup(""), place)
            for place in places)
        rows = []
        for article in articles:
            rows.append(Markup(
                '<tr class="di_article"><td colspan="3">%(name)s '
                '<span class="di_count">%(count)s</span></td>%(cells)s</tr>') % {
                    'name': article['name'],
                    'count': _("%s variant(s)", len(article['variants'])),
                    'cells': cells(article['stock'], article['total']),
                })
            for variant in article['variants']:
                rows.append(Markup(
                    '<tr><td class="di_barcode">%(barcode)s</td>'
                    '<td>%(colour)s</td><td>%(size)s</td>%(cells)s</tr>') % {
                        'barcode': variant['product'].barcode or '-',
                        'colour': variant['colour'] or '-',
                        'size': variant['size'] or '-',
                        'cells': cells(variant['stock'], variant['total']),
                    })
        variants = sum(len(article['variants']) for article in articles)
        banner = Markup(
            '<div class="alert alert-warning d-flex align-items-center gap-3 '
            'mb-3" role="status"><i class="fa fa-2x fa-cubes" aria-hidden="true"></i>'
            '<div><div class="fs-5 fw-bold">%(title)s</div><div>%(body)s</div>'
            '</div></div>') % {
                'title': _("%(variants)s variant(s) of %(articles)s article(s) "
                           "were already in the catalogue.",
                           variants=variants, articles=len(articles)),
                'body': _("They have been imported normally. Below is where "
                          "they are in stock right now, before this arrival."),
            }
        return banner + Markup(
            '<div class="di_existing"><table class="di_existing_table">'
            '<thead><tr><th>%(barcode)s</th><th>%(colour)s</th>'
            '<th>%(size)s</th>%(head)s<th class="di_qty di_total">%(total)s</th>'
            '</tr></thead><tbody>%(rows)s</tbody></table></div>') % {
                'barcode': _("Barcode"),
                'colour': _("Colour"),
                'size': _("Size"),
                'head': head,
                'total': _("Total"),
                'rows': Markup("").join(rows),
            }

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _skip_labels():
        """Why a row was dropped, in the language of the user importing.

        Built per call rather than as a module constant so the sentences are
        translated at import time and not once at server start-up.
        """
        return {
            'no_code': _("no product code"),
            'bad_quantity': _("the quantity is not a number"),
            'negative_quantity': _("the quantity is negative"),
            'not_found': _("the product is not in the catalogue"),
        }

    @staticmethod
    def _row_ranges(numbers, limit=15):
        """Render row numbers compactly: ``[4, 5, 6, 7, 9]`` -> ``4-7, 9``."""
        ranges = []
        start = previous = numbers[0]
        for number in numbers[1:]:
            if number == previous + 1:
                previous = number
                continue
            ranges.append((start, previous))
            start = previous = number
        ranges.append((start, previous))

        shown = ranges[:limit]
        text = ", ".join(
            str(first) if first == last else f"{first}-{last}"
            for first, last in shown)
        if len(ranges) > limit:
            text += _(", and %s more", len(ranges) - limit)
        return text

    def _format_report(self, created, updated, skipped, warnings,
                       articles=()):
        """Summarise the import as HTML for the chatter.

        Returned as ``Markup``: ``message_post`` escapes a plain string, which
        is why the old report showed its own ``<br/>`` tags as text.

        Rows are grouped by reason. A file with half of its quantity column
        empty used to post one identical sentence per row, which pushed the
        two lines that did import off the top of the message.
        """
        labels = self._skip_labels()
        parts = []

        if self.filename:
            parts.append(Markup("<p><b>%s</b></p>") % _(
                "Import from %s", self.filename))

        parts.append(Markup("<p>%s</p>") % _(
            "%(created)s line(s) created, %(updated)s updated.",
            created=created, updated=updated))

        if skipped:
            grouped = {}
            for number, code, reason in skipped:
                label = labels.get(reason, reason)
                grouped.setdefault(label, []).append((number, code))
            items = Markup("").join(
                Markup("<li>%(reason)s &mdash; %(rows)s%(codes)s</li>") % {
                    'reason': label,
                    'rows': self._format_rows(
                        [number for number, _code in entries]),
                    'codes': self._format_codes(
                        [code for _number, code in entries]),
                }
                for label, entries in grouped.items())
            parts.append(Markup("<p>%(title)s</p><ul>%(items)s</ul>") % {
                'title': _("%s row(s) skipped, nothing was imported for them:",
                           len(skipped)),
                'items': items,
            })

        if articles:
            # A small table, one row per article: what came back in and how
            # much of it was already in stock. The warehouse-by-warehouse
            # detail is in the arrival's "Already in the Catalogue" tab - in
            # the chatter it ran every article into one dense paragraph.
            rows = Markup("").join(
                Markup('<tr><td>%(name)s</td><td class="text-end">%(variants)s</td>'
                       '<td class="text-end">%(stock)s</td></tr>') % {
                    'name': article['name'],
                    'variants': len(article['variants']),
                    'stock': self._format_quantity(article['total'])
                    if article['total'] else Markup('<span class="text-muted">%s</span>')
                    % _("none"),
                }
                for article in articles[:EXISTING_REPORT_LIMIT])
            if len(articles) > EXISTING_REPORT_LIMIT:
                rows += Markup(
                    '<tr><td colspan="3" class="text-muted">%s</td></tr>') % _(
                    "...and %s more", len(articles) - EXISTING_REPORT_LIMIT)
            parts.append(Markup(
                '<p>%(title)s</p>'
                '<table class="table table-sm mb-2"><thead><tr>'
                '<th>%(article)s</th><th class="text-end">%(variants)s</th>'
                '<th class="text-end">%(stock)s</th></tr></thead>'
                '<tbody>%(rows)s</tbody></table>'
                '<p class="text-muted">%(where)s</p>') % {
                    'title': _("%s variant(s) were already in the catalogue:",
                               sum(len(article['variants'])
                                   for article in articles)),
                    'article': _("Article"),
                    'variants': _("Variants"),
                    'stock': _("In Stock"),
                    'rows': rows,
                    'where': _("Stock per warehouse is in the Already in the "
                               "Catalogue tab."),
                })

        if warnings:
            items = Markup("").join(
                Markup("<li>%s</li>") % _(
                    "row %(row)s: %(warning)s", row=number, warning=warning)
                for number, warning in warnings[:20])
            parts.append(Markup("<p>%(title)s</p><ul>%(items)s</ul>") % {
                'title': _("%s warning(s):", len(warnings)),
                'items': items,
            })

        return Markup("").join(parts)

    def _format_rows(self, numbers):
        """``rows 4-13`` / ``row 7`` - singular and plural both read right."""
        ranges = self._row_ranges(numbers)
        return _("row %s", ranges) if len(numbers) == 1 else _("rows %s", ranges)

    @staticmethod
    def _format_codes(codes, limit=5):
        """The distinct product codes behind a group, as a muted aside."""
        distinct = list(dict.fromkeys(code for code in codes if code))
        if not distinct:
            return Markup("")
        text = ", ".join(distinct[:limit])
        if len(distinct) > limit:
            text += _(", and %s more", len(distinct) - limit)
        return Markup(' <span class="text-muted">(%s)</span>') % text

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _read_file(self):
        """Return ``(data rows, header row)`` from the uploaded file.

        The format is decided by the file's own first bytes, so a spreadsheet
        saved with the wrong extension still imports instead of failing on a
        decode error the worker cannot act on.
        """
        data = base64.b64decode(self.file or b'')
        if data.startswith(XLSX_MAGIC):
            rows = self._read_xlsx(data)
        elif data.startswith(XLS_MAGIC):
            raise UserError(_(
                "This is a legacy .xls file, which cannot be read. Open it "
                "and save it as .xlsx or as CSV, then import again."))
        else:
            rows = self._read_csv(data)
        if not rows:
            return [], []
        return rows[1:], rows[0]

    def _read_csv(self, data):
        try:
            content = data.decode(self.encoding)
        except UnicodeDecodeError as exc:
            raise UserError(_(
                "The file is not valid %(encoding)s text. Try the other "
                "encoding option, or save it as .xlsx.\n%(error)s",
                encoding=self.encoding, error=exc)) from exc
        reader = csv.reader(io.StringIO(content), delimiter=self.delimiter)
        return [row for row in reader
                if any((cell or '').strip() for cell in row)]

    def _read_xlsx(self, data):
        """Read the first sheet of an .xlsx workbook into rows of text."""
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise UserError(_(
                "Reading .xlsx files needs the Python library openpyxl, which "
                "is not installed on this server. Ask your administrator to "
                "install it, or save the file as CSV and import that.")) from exc

        # ``data_only`` returns the value Excel last calculated for a formula
        # rather than the formula itself, which is what a worker who totalled
        # a column in the sheet expects. A file written by a script and never
        # opened in Excel carries no cached values, so its formula cells read
        # as empty and are reported as skipped rows.
        try:
            workbook = load_workbook(
                io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise UserError(_(
                "The file could not be read as an .xlsx workbook. If it came "
                "from LibreOffice or Google Sheets, export it as .xlsx or as "
                "CSV.\n%s", exc)) from exc

        try:
            sheet = workbook.worksheets[0] if workbook.worksheets else None
            if sheet is None:
                raise UserError(_("The workbook has no sheets."))
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = [self._cell_text(value) for value in row]
                if any(cells):
                    rows.append(cells)
        finally:
            workbook.close()
        return rows

    @staticmethod
    def _cell_text(value):
        """Render one spreadsheet cell as the text the CSV path would carry.

        Excel holds every number as a float, so a quantity of 10 arrives as
        ``10.0`` and a 13-digit barcode as ``8.0012e+12``. Integral values are
        rendered without a decimal part to keep both readable, and the rest
        are rounded: a price typed as 19,90 is stored as 19.899999999999999
        and must not reach a pricelist rule that way.
        """
        if value is None:
            return ''
        if isinstance(value, bool):
            return '1' if value else ''
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return f'{round(value, 4):f}'.rstrip('0').rstrip('.')
        if isinstance(value, int):
            return str(value)
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        return str(value).strip()

    def _map_columns(self, headers):
        """Map internal meaning -> column index, from the header row."""
        mapping = {}
        normalised = [_norm(h) for h in headers]
        for key, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in normalised:
                    mapping[key] = normalised.index(alias)
                    break
        return mapping

    def _map_warehouse_columns(self, headers, mapping):
        """Map column index -> warehouse, for optional pre-split columns.

        Any header matching a warehouse code or name (and not already used as
        a data column) is treated as a distribution column, letting a worker
        pre-fill the split in Excel.
        """
        used = set(mapping.values())
        warehouses = self.env['stock.warehouse'].search([
            ('company_id', '=', self.arrival_id.company_id.id)])
        by_key = {}
        for warehouse in warehouses:
            if warehouse == self.arrival_id.warehouse_id:
                continue  # stock staying put needs no column
            by_key[_norm(warehouse.code)] = warehouse
            by_key[_norm(warehouse.name)] = warehouse

        columns = {}
        for index, header in enumerate(headers):
            if index in used:
                continue
            warehouse = by_key.get(_norm(header))
            if warehouse:
                columns[index] = warehouse
        return columns

    @staticmethod
    def _cell(row, index):
        if index is None or index >= len(row):
            return ''
        return (row[index] or '').strip()

    @staticmethod
    def _to_float(value):
        if not value:
            return 0.0
        try:
            return float(value.replace(',', '.'))
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def _import_rows(self, rows, mapping, warehouse_columns):
        arrival = self.arrival_id
        Line = self.env['di.stock.arrival.line']
        Product = self.env['product.product']
        created = updated = 0
        # ``(row number, code, reason)`` rather than a ready-made sentence, so
        # the report can group the rows that were dropped for the same reason
        # instead of repeating it once per row.
        skipped = []
        warnings = []
        pricelists = self._resolve_pricelists() if self.update_prices else {}

        # Existing lines, so re-importing a corrected file tops up rather
        # than duplicating.
        lines_by_product = {
            line.product_id.id: line for line in arrival.line_ids}
        # Variants the catalogue already held before this file, so the worker
        # can be told about them. Anything this import creates gets a higher
        # id. A variant already on this arrival is left out: re-importing a
        # corrected file would otherwise flag every row of the first attempt.
        last_known_id = Product.sudo().with_context(active_test=False).search(
            [], order='id desc', limit=1).id
        on_arrival = set(lines_by_product)
        existing_ids = {}  # a dict keeps file order
        # Lookup tables for the loose matching below, built on the first miss
        # and then reused for the rest of the file.
        index = {}

        for number, row in enumerate(rows, start=2):
            code = self._cell(row, mapping.get('code'))
            if not code:
                skipped.append((number, '', 'no_code'))
                continue

            quantity = self._to_float(self._cell(row, mapping.get('quantity')))
            if quantity is None:
                skipped.append((number, code, 'bad_quantity'))
                continue
            if quantity < 0:
                skipped.append((number, code, 'negative_quantity'))
                continue

            try:
                product = self._resolve_product(row, mapping, code, index)
            except UserError as exc:
                skipped.append((number, code, exc.args[0]))
                continue
            if not product:
                skipped.append((number, code, 'not_found'))
                continue
            if product.id <= last_known_id and product.id not in on_arrival:
                existing_ids.setdefault(product.id)

            price = self._to_float(self._cell(row, mapping.get('price'))) or 0.0

            # Keep the ESC article code on the variant. Odoo stores
            # ``default_code`` per variant, and a template with several
            # variants reports it as empty, so stamping the template is not
            # enough - every variant of CS-320 carries "CS-320".
            if not product.default_code:
                product.default_code = code

            if self.update_prices:
                self._apply_prices(product, row, mapping, pricelists)

            line = lines_by_product.get(product.id)
            if line:
                line.quantity += quantity
                if price:
                    line.price_unit = price
                updated += 1
            else:
                line = Line.create({
                    'arrival_id': arrival.id,
                    'product_id': product.id,
                    'quantity': quantity,
                    'price_unit': price or product.standard_price,
                })
                lines_by_product[product.id] = line
                created += 1

            self._apply_warehouse_columns(line, row, warehouse_columns, number,
                                          warnings)

        return (created, updated, skipped, warnings,
                Product.browse(list(existing_ids)))

    def _apply_warehouse_columns(self, line, row, warehouse_columns, number,
                                 warnings):
        """Create allocations from optional per-warehouse columns.

        A bad cell here only costs one allocation - the line itself was
        imported - so these are reported as warnings, apart from the rows that
        were dropped altogether.
        """
        Allocation = self.env['di.stock.arrival.allocation']
        for index, warehouse in warehouse_columns.items():
            quantity = self._to_float(self._cell(row, index))
            if not quantity:
                continue
            if quantity < 0:
                warnings.append((number, _(
                    "negative quantity for %s ignored", warehouse.code)))
                continue
            existing = line.allocation_ids.filtered(
                lambda a, w=warehouse: a.warehouse_id == w)
            if existing:
                existing.quantity += quantity
            else:
                Allocation.create({
                    'line_id': line.id,
                    'warehouse_id': warehouse.id,
                    'quantity': quantity,
                })

    # ------------------------------------------------------------------
    # Product resolution
    # ------------------------------------------------------------------

    def _resolve_product(self, row, mapping, code, index=None):
        """Find, or create, the variant a CSV row refers to."""
        Product = self.env['product.product']
        index = {} if index is None else index
        color = self._cell(row, mapping.get('color'))
        size = self._cell(row, mapping.get('size'))
        barcode = self._cell(row, mapping.get('barcode'))

        if barcode:
            product = Product.search([('barcode', '=', barcode)], limit=1)
            if product:
                return product

        template = self.env['product.template'].search([
            ('default_code', '=', code),
            ('company_id', 'in', (False, self.arrival_id.company_id.id)),
        ], limit=1)
        if not template:
            template = Product.search(
                [('default_code', '=', code)], limit=1).product_tmpl_id

        if not template:
            if not self.create_missing_products:
                return Product
            product = self._create_product(
                row, mapping, code, color, size, index)
            return self._stamp_barcode(product, barcode)

        if not color and not size:
            variants = template.product_variant_ids
            if len(variants) == 1:
                return variants
            raise UserError(_(
                "%s has several variants; the row needs a Colore and/or "
                "Taglia to say which one.", template.display_name))

        # Which variants existed before this row, so a variant that had to be
        # materialised for it can be told from one that was already there.
        known = set(template.product_variant_ids.ids)
        product = self._get_or_create_variant(
            template, color, size, create=self.create_missing_products,
            index=index)
        if product and product.id not in known:
            self._stamp_barcode(product, barcode)
        return product

    def _stamp_barcode(self, product, barcode):
        """Put the file's barcode on a variant this import has just created.

        A new variant is given a sequence number by the database's own
        automation, which is what you want for an article nobody has a barcode
        for and wrong when the file is carrying the real one. Only ever
        applied to a variant created here, and only when the barcode is free -
        Odoo holds it unique across products.
        """
        if not product or not barcode or product.barcode == barcode:
            return product
        if self.env['product.product'].search_count([('barcode', '=', barcode)]):
            return product
        product.barcode = barcode
        return product

    def _create_product(self, row, mapping, code, color, size, index=None):
        name = self._cell(row, mapping.get('name'))
        if not name:
            raise UserError(_(
                "product does not exist and the row has no name to create it "
                "with."))
        # Everything received here is retail stock, so it is sellable at the
        # till. Odoo defaults this to False, and POS has no way to filter its
        # catalogue by stock anyway - available_in_pos is one global boolean,
        # not a per-shop one - so leaving it off would simply make new
        # arrivals unscannable.
        values = {
            'name': name,
            'type': 'consu',
            'is_storable': True,
            'available_in_pos': True,
            'purchase_ok': True,
            'sale_ok': True,
            'company_id': False,
        }
        category = self._resolve_category(self._cell(row, mapping.get('category')))
        if category:
            values['categ_id'] = category.id
        values.update(self._resolve_taxonomy(row, mapping))
        # Prices are applied per variant afterwards by ``_apply_prices``.

        template = self.env['product.template'].create(values)
        if not color and not size:
            return template.product_variant_ids[:1]
        return self._get_or_create_variant(
            template, color, size, create=True, index=index)

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def _resolve_pricelists(self):
        """Map each price column to the pricelists it feeds, once per run.

        Resolved up front rather than cached on ``self``: a recordset uses
        __slots__, so it cannot carry arbitrary attributes.
        """
        Pricelist = self.env['product.pricelist']
        company_domain = [('company_id', 'in',
                           (False, self.arrival_id.company_id.id))]
        resolved = {}
        for key, names in PRICELIST_TARGETS.items():
            records = Pricelist.browse()
            for name in names:
                found = Pricelist.search(
                    [('name', '=', name)] + company_domain, limit=1)
                if not found:
                    # Pricelists are usually named "Promo (EUR)", so fall back
                    # to the part before the currency suffix. Compared exactly
                    # so "Promo" cannot swallow "Promo Franchising".
                    candidates = Pricelist.search(
                        [('name', '=like', f'{name}%')] + company_domain)
                    found = candidates.filtered(
                        lambda p, n=name:
                        _norm(p.name.split('(')[0]) == _norm(n))[:1]
                if found:
                    records |= found
                else:
                    _logger.warning(
                        "Pricelist %r not found; its price column is ignored.",
                        name)
            resolved[key] = records
        return resolved

    def _apply_prices(self, product, row, mapping, pricelists):
        """Write the four prices carried by a row.

        They mirror the four ESC listini::

            ACQUISTO (4)         -> standard_price (cost, per variant)
            VENDITA (2)          -> list_price + "Dettaglio" pricelist
            VENDITA SCONTATO (1) -> "Promo" + "Promo Franchising" pricelists
            VENDITA INGROSSO (3) -> "Ingrosso" pricelist

        Prices live on the template, so several variant rows of the same
        article resolve to the same rules; existing rules are updated rather
        than duplicated.
        """
        template = product.product_tmpl_id

        cost = self._to_float(self._cell(row, mapping.get('price')))
        if cost:
            product.standard_price = cost

        sale = self._to_float(self._cell(row, mapping.get('sale_price')))
        if sale:
            template.list_price = sale

        for key in PRICELIST_TARGETS:
            price = self._to_float(self._cell(row, mapping.get(key)))
            if not price:
                continue
            for pricelist in pricelists.get(key, ()):
                self._set_pricelist_rule(template, pricelist, price)

    def _set_pricelist_rule(self, template, pricelist, price):
        Item = self.env['product.pricelist.item']
        item = Item.search([
            ('pricelist_id', '=', pricelist.id),
            ('product_tmpl_id', '=', template.id),
            ('applied_on', '=', '1_product'),
        ], limit=1)
        if item:
            if item.compute_price == 'fixed' and item.fixed_price != price:
                item.fixed_price = price
            return
        Item.create({
            'pricelist_id': pricelist.id,
            'applied_on': '1_product',
            'product_tmpl_id': template.id,
            'compute_price': 'fixed',
            'fixed_price': price,
        })

    def _resolve_category(self, name):
        if not name:
            return self.env['product.category']
        Category = self.env['product.category']
        category = Category.search([('complete_name', '=', name)], limit=1)
        if not category:
            category = Category.search([('name', '=', name)], limit=1)
        return category

    def _resolve_taxonomy(self, row, mapping):
        """Resolve the Studio taxonomy columns, if those models exist."""
        values = {}
        template_fields = self.env['product.template']._fields
        for key, (field_name, model_name) in TAXONOMY_FIELDS.items():
            if field_name not in template_fields:
                continue
            label = self._cell(row, mapping.get(key))
            if not label:
                continue
            if model_name not in self.env:
                continue
            Model = self.env[model_name].sudo()
            record = Model.search([('x_name', '=', label)], limit=1)
            if not record:
                record = Model.create({'x_name': label})
            values[field_name] = record.id
        return values

    def _get_or_create_variant(self, template, color, size, create=True,
                               index=None):
        """Return the variant of ``template`` for the given colour and size.

        Both attributes are configured as ``create_variant='dynamic'``, so a
        combination that has never been sold does not exist yet and has to be
        materialised on demand.
        """
        index = {} if index is None else index

        # Look for the variant that is already there before building anything.
        # Everything below this point changes the catalogue - it adds attribute
        # values, attribute lines, and finally a variant that the database's
        # "Barcode automatico articolo" automation stamps with an invented
        # barcode - so a row for stock that already exists must never reach it.
        matches = self._find_variant_by_values(template, color, size)
        if len(matches) == 1:
            return matches
        if len(matches) > 1:
            # Worded without the product in it on purpose: the report groups
            # skipped rows by reason and lists their codes, so one sentence
            # covers a file that leaves the Taglia column empty throughout
            # instead of repeating itself once per row.
            raise UserError(_(
                "several variants match that Colore and Taglia, so the row "
                "does not say which one arrived"))

        ptavs = self.env['product.template.attribute.value']
        for attribute_name, value in ((COLOR_ATTRIBUTE, color),
                                      (SIZE_ATTRIBUTE, size)):
            if not value:
                continue
            ptav = self._get_or_create_ptav(
                template, attribute_name, value, create=create, index=index)
            if not ptav:
                raise UserError(_(
                    "%(attribute)s '%(value)s' is not available on %(product)s.",
                    attribute=attribute_name, value=value,
                    product=template.display_name))
            ptavs |= ptav

        if not ptavs:
            return template.product_variant_ids[:1]

        variant = self._find_variant(template, ptavs)
        if variant:
            return variant
        if not create:
            return self.env['product.product']
        return self._materialise_variant(template, ptavs)

    @staticmethod
    def _find_variant_by_values(template, color, size):
        """Variants of ``template`` already carrying the row's colour and size.

        Matched on the value names, normalised, for two reasons the catalogue
        makes plain. Supplier files spell a colour freely - this database
        already holds ACQUA MARINA beside ACQUAMARINA, and BEIGE 172 beside
        BEIGE172 - and each of those pairs cost a second variant, stamped with
        an invented barcode, next to the one that held the real one. And a row
        that names a colour but no size has to find the variant of a one-size
        article rather than materialise a sizeless second one: attributes the
        row says nothing about are ignored rather than required to be absent.

        Names, not ids, because this runs before anything is created - the
        point is to change nothing when the goods are already in the
        catalogue. Comparing within one template, which was matched on its
        exact article code, so a loose name here cannot reach another article.
        """
        wanted = {
            attribute: _norm(value)
            for attribute, value in ((COLOR_ATTRIBUTE, color),
                                     (SIZE_ATTRIBUTE, size))
            if value
        }
        matches = template.product_variant_ids.browse()
        if not wanted:
            return matches
        for variant in template.product_variant_ids:
            carried = {
                ptav.attribute_id.name: _norm(ptav.name)
                for ptav in variant.product_template_attribute_value_ids
            }
            if all(carried.get(attribute) == value
                   for attribute, value in wanted.items()):
                matches |= variant
        return matches

    @staticmethod
    def _find_variant(template, ptavs):
        """Find the variant carrying exactly this combination.

        Matched on ``product_template_attribute_value_ids``, not on
        ``product_template_variant_value_ids``: the latter only lists values
        of attributes that have *several* values on the template, so it is
        empty for a single-colour, single-size article and would never match.
        """
        target = set(ptavs.ids)
        for variant in template.product_variant_ids:
            if set(variant.product_template_attribute_value_ids.ids) == target:
                return variant
        return template.product_variant_ids.browse()

    def _get_or_create_ptav(self, template, attribute_name, value_name,
                            create=True, index=None):
        index = {} if index is None else index
        Attribute = self.env['product.attribute']
        attribute = Attribute.search([('name', '=', attribute_name)], limit=1)
        if not attribute:
            if not create:
                return None
            attribute = Attribute.create({
                'name': attribute_name, 'create_variant': 'dynamic'})

        Value = self.env['product.attribute.value']
        value = Value.search([
            ('attribute_id', '=', attribute.id),
            ('name', '=ilike', value_name),
        ], limit=1)
        if not value:
            value = self._value_by_loose_name(attribute, value_name, index)
        if not value:
            if not create:
                return None
            value = Value.create({
                'attribute_id': attribute.id, 'name': value_name})

        line = template.attribute_line_ids.filtered(
            lambda l, a=attribute: l.attribute_id == a)
        if not line:
            if not create:
                return None
            line = self.env['product.template.attribute.line'].create({
                'product_tmpl_id': template.id,
                'attribute_id': attribute.id,
                'value_ids': [fields.Command.link(value.id)],
            })
        elif value not in line.value_ids:
            if not create:
                return None
            line.value_ids = [fields.Command.link(value.id)]

        return line.product_template_value_ids.filtered(
            lambda p, v=value: p.product_attribute_value_id == v)[:1]

    def _value_by_loose_name(self, attribute, value_name, index):
        """The attribute value whose name differs only in spacing or case.

        Colours come off supplier files as freely as codes do: this database
        already carries ACQUA MARINA next to ACQUAMARINA and BEIGE 172 next to
        BEIGE172. Each of those pairs is a second value on the template, so a
        second variant was materialised for it and stamped with an invented
        barcode, while the variant that was already there kept the real one.

        Read once per attribute, on the first name that misses.
        """
        values = index.setdefault('values', {})
        if attribute.id not in values:
            values[attribute.id] = {}
            for row in self.env['product.attribute.value'].search_read(
                    [('attribute_id', '=', attribute.id)], ['name'],
                    order='id'):
                values[attribute.id].setdefault(_norm(row['name']), row['id'])
        value_id = values[attribute.id].get(_norm(value_name))
        return self.env['product.attribute.value'].browse(value_id or [])

    def _materialise_variant(self, template, ptavs):
        """Create the variant for a combination of attribute values.

        Guarded because the private helper Odoo uses for this has moved
        between versions; falling back to a full rebuild is slower but always
        works.
        """
        if hasattr(template, '_create_product_variant'):
            variant = template._create_product_variant(ptavs, log_warning=False)
            if variant:
                return variant
        else:
            _logger.warning(
                "product.template._create_product_variant is unavailable; "
                "falling back to _create_variant_ids().")
        template._create_variant_ids()
        template.invalidate_recordset(['product_variant_ids'])
        variant = self._find_variant(template, ptavs)
        if variant:
            return variant
        raise UserError(_(
            "Could not create the variant of %s for that colour and size "
            "combination.", template.display_name))
