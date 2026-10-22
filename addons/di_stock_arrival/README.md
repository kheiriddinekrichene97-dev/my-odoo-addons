# Stock Arrivals (Arrivi Merce)

Odoo addon that records incoming goods without a purchase order and divides
them across warehouses.

## Why It Exists

Dimensione Immagine receives goods from suppliers without ever raising a
purchase order, so Odoo's native Purchase → Receipt flow does not apply. In the
legacy ESC system the job was done by a pair of packing lists — one of type
`Fornitore` for the arrival, then one of type `Deposito` per destination shop.
This module reproduces that flow natively and adds the parent-to-children link
ESC never kept, so an arrival can always be traced to the transfers it produced.

## The Flow

1. **Draft** — a worker records what turned up: supplier, date, and one line
   per product variant with the quantity received. Lines can be typed, or
   imported from a CSV or Excel file.
2. **Confirm** — generates and validates one receipt (`Vendors → arrival
   warehouse`). The stock is now real and countable, and tags can be printed.
   The document moves to **Not Divided**.
3. **Divide** — the worker allocates each line across destination warehouses,
   either row by row or with the *Divide* wizard (a percentage or a fixed
   quantity per line, applied to one destination at a time).
4. **Mark as Divided** — generates and validates one internal transfer per
   destination warehouse. The document moves to **Divided**.

## Partial Division Is Normal

Goods routinely stay at the central warehouse. Whatever is not allocated simply
remains there and needs no transfer or paperwork.

`qty_remaining` on each line is therefore **informational only** — it is never
a validation gate, and an arrival can be marked as divided with any amount, or
all, of it left behind. Gating closure on full allocation would make the module
unusable.

Distributing *more* than arrived is the opposite case and is refused. The check
lives on `di.stock.arrival.allocation`, not only on the line: a constraint
declared on the line's `allocation_ids` runs only when that one2many is
written, so rows edited directly in the Distribution tab used to bypass it and
a line of 10 accepted an allocation of 11.

## Lines That Received Nothing

A supplier file lists the whole size grid and puts a `0` against the sizes
that did not come, so **a quantity of zero imports as a line** rather than
being dropped. The arrival then shows the grid as it was sent. Only a
negative quantity is refused, by the import and by the line's constraint.

A zero line receives nothing: `_create_receipt` builds moves from the lines
with a quantity above zero, and confirming an arrival whose every line is
zero is refused - that arrival received nothing. When such a size does turn
up, correcting the line while the arrival is still undivided adds a movement
of its own to the receipt (`_add_receipt_move`), dated when the goods were
found; nothing merges into the movements already there, because Odoo's move
merge skips candidates in state `done`.

## Correcting What Arrived

The count is often wrong the first time, and the mistake surfaces after the
arrival has been confirmed. Until **Divide** or **Mark as Divided** is pressed,
the quantities in *Products Received* stay editable, and each correction is
carried through to the receipt that confirming validated.

`di.stock.arrival.line.write()` does that: it takes the quantity from before
the write, and applies the **difference** to the matching move line of the
validated receipt. Writing a new quantity onto a done move line is the same
path Odoo takes when a validated transfer is unlocked and edited — it undoes
the original quant movement and re-applies it, so the stock on hand ends up
matching the correction, and Odoo logs a note on the transfer. The arrival
gets one of its own in the chatter, saying what changed and on which receipt.

Three details behind that:

- The movement is found **by product, not by a stored link**. Confirming runs
  `action_confirm`, which merges identical moves, so two lines of the same
  variant arrive as one movement; a link field would point at a move that no
  longer exists. Applying the difference rather than the total is what makes
  the shared movement come out right.
- **Products, and lines themselves, stay a draft-only edit.** Adding one after
  the goods are in means a movement that never happened; removing one means a
  movement that has to be undone. `create()` and `unlink()` refuse, and say
  what to do instead - correct the quantity, or record the rest as an arrival
  of its own. The *Add a line* row still appears in the list, because Odoo
  evaluates a list's `create`/`delete` attributes statically and they cannot
  depend on the record's state; the guard is what actually holds.
- Correcting **below what is already distributed** is refused by the existing
  over-allocation constraint, which names the excess.

**A correction and the distribution that goes with it are saved together.**
Zeroing a line and clearing its cells are only legal as one change: written
apart, whichever half lands first leaves more distributed than arrived and the
arrival is refused. *Save Distribution* therefore sends the pending received
quantities along with the cells, and `set_distribution_matrix` applies them in
that one transaction - raises before the cells that need the room, drops after
the cells have given it back, the same shrink-before-grow rule the allocations
themselves follow. The form is saved afterwards rather than before, and writes
the line again with the value that is already there.

Once the division is generated the lines are readonly again: the goods have
left for the shops and a correction there is a matter for the transfers.
`write()` refuses it as well, so it holds for anything going through the ORM
and not only for the form.

### The Distribution Grid

The Distribution tab is a matrix: **one row per arrival line, one column per
destination warehouse**, the shape the legacy ESC "Genera Picking" screen used.
A hundred-product arrival is one screen instead of a hundred rows repeated per
shop.

- **Received** and **Remaining** sit next to each product and update as you
  type; a row that sends out more than arrived turns red.
- **Only the destinations you pick get a column.** Thirty warehouses across
  the top is unusable no matter how it scrolls, so the grid opens on the
  destinations this arrival already sends to - none, on a fresh one - and the
  chips above it add or remove columns. This is the legacy *Seleziona Filiali*
  step, and it is what keeps the grid narrow enough to read.
- A chip turns amber when its destination holds quantities but is hidden, so a
  column cannot be forgotten. Hiding never clears anything: the values are
  still saved.
- Columns are headed by the **warehouse name**, wrapped over two lines, with
  the short code as the tooltip. One filter narrows the products, another the
  destination chips.
- **Received follows the other tab, saved or not.** Odoo's notebook renders
  only the open page, so the grid is rebuilt - and re-fetched - every time the
  tab is opened; what it fetched was the *saved* quantity, which made a
  correction typed in Products Received and not yet saved look as though it
  had not landed. The grid now reads those quantities off the form's own
  record (`syncReceivedFromRecord`), so the column follows them with no round
  trip and before they are written. Showing the unsaved number is the honest
  one to show: *Save Distribution* saves the form first, so that is the number
  the server will check the distribution against.
- **Each destination heading carries its own fill box**: a quantity typed
  there (Enter, or clicking away) goes down that column for every product
  listed and leaves the other columns exactly as they are - one shop takes two
  of each, the next five, without filling the whole grid and correcting it
  afterwards. It shares its code path with the toolbar fill below, so the
  rules are the same: it overwrites rather than fills the blanks, `0` clears
  the column, and the product filter narrows what it touches.
- **Same quantity everywhere** types a number once and writes it into every
  cell on screen - every product row listed, every destination column picked.
  A shop taking one piece of each of twenty products was twenty identical
  numbers typed by hand, per shop. It replaces what is in those cells rather
  than filling only the blanks, narrow the rows with the product filter first
  if that is not what you want; filling with `0` clears the block, and nothing
  reaches the database until *Save Distribution*, so *Discard* undoes it.
- The product column and the header stay frozen while the grid scrolls
  **inside itself** - it never widens the form. That containment comes from a
  `minmax(0, 1fr)` grid track on the field wrapper: `width: 100%` alone
  resolves against a containing block that has already stretched to fit the
  table, which is why the page scrolled sideways instead of the grid.
### Colour, and why it took four attempts

**Odoo's backend bundle exposes no CSS custom properties** — neither
Bootstrap's `--bs-*` nor Odoo's `--o-*`. Every `var(--bs-…)` silently resolves
to its fallback, which produced a different wrong result each round: a
transparent sticky header, then a white column with invisible text.

The grid therefore follows three rules:

- **Text colour is never declared.** Cells inherit the form's own colour,
  which the theme already gets right. This only works because the table does
  **not** carry Bootstrap's `.table` class — that class repaints cell text via
  `--bs-table-color` and swaps it on hover, which is what made every row read
  as dim grey while the hovered one looked bright.
- **Only sticky cells need an opaque background**, and that one value comes
  from an SCSS variable (`$o-view-background-color`, else `$body-bg`, else
  white), guarded with `variable-exists` so a rename cannot break the bundle.
  Odoo compiles the bundle once per theme, so SCSS gets the right value where
  a CSS variable gets nothing.
- **Every other tint is a translucent `rgba`**, which reads correctly over a
  light or a dark ground without needing to know which it is.
- Empty cells send nothing. The arrival warehouse is never a column: what stays
  there needs no transfer.

The grid writes straight to the database through two methods on
`di.stock.arrival`, rather than through the form's own save:

| Method | Role |
|---|---|
| `get_distribution_matrix()` | Lines, destination warehouses and current cells, in one round trip |
| `set_distribution_matrix(cells)` | Writes the grid back and returns the refreshed matrix |

`cells` maps `"<line id>-<warehouse id>"` to a quantity and is authoritative
for everything the grid can show, so a cleared cell (absent from the payload)
deletes its allocation. Allocations to a warehouse the grid never listed are
out of scope and left alone. Decreases and deletions are written **before**
increases: moving ten pieces from one shop to another would otherwise stand at
twenty midway through the write and trip the over-allocation constraint on a
distribution that is perfectly legal.

The component holds no business rules. Over-allocation shown in red is a
courtesy; the constraint on `di.stock.arrival.allocation` is what actually
refuses it, so the grid, the CSV import and the Divide wizard are all held to
the same rule.

**If the asset bundle ever fails to build**, remove `widget="di_distribution_matrix"`
from the `allocation_ids` field in `views/stock_arrival_views.xml` and the tab
falls back to a plain editable list. The same list is also available to
developers in the **Distribution Rows** tab.

### Row-by-Row Fallback

The Distribution tab is built for someone working through a long arrival:

- the product column shows the variant (`[CS-FOUTA 07] TELO MARE RIGHE
  (BORDEAUX)`), not a raw record reference;
- **Received** and **Still Unassigned** are shown per row, so what is left does
  not have to be worked out from the other tab;
- the product picker only offers lines that still have something left. It
  reflects **saved** state, so a line you have just filled in disappears from
  the list after the record is saved, not before.

## Models

| Model | Role |
|---|---|
| `di.stock.arrival` | The arrival document: supplier, date, status |
| `di.stock.arrival.line` | One row per variant, with the quantity received |
| `di.stock.arrival.allocation` | How much of a line goes to which warehouse |

The numbers answer the three questions that matter:

| Question | Field |
|---|---|
| How much of product X arrived? | `line.quantity` |
| How much goes to each shop or stockroom? | `allocation.quantity` |
| How much stays at the arrival warehouse? | `line.qty_remaining` |

## CSV / XLSX Import

Available on a draft arrival via **Import from CSV**.

**Both `.csv` and `.xlsx` are accepted.** The format is decided by the file's
own first bytes, not its extension, so a workbook a worker renamed to `.csv`
still imports. Only the first sheet is read and its first row is the header.
The *Separator* and *Encoding* options apply to CSV only and are hidden once a
workbook is selected.

Reading `.xlsx` needs `openpyxl`. It is **not** declared in
`external_dependencies`: CSV import must keep working on a server without it,
so the wizard raises a plain "ask your administrator to install it" message
instead of blocking the whole module from installing.

Two spreadsheet quirks are handled while reading:

- Excel stores every number as a float, so a quantity of `10` arrives as
  `10.0` and a 13-digit barcode as `8.0012e+12`. Integral values are rendered
  without a decimal part.
- A price typed as `19,90` is stored as `19.899999999999999`. Values are
  rounded to four decimals, so that does not reach a pricelist rule verbatim.

Formula cells are read as the value Excel last calculated (`data_only`). A
workbook written by a script and never opened in Excel carries no cached
values, so its formula cells read as empty and are reported as skipped rows.

Legacy `.xls` is not supported and says so; re-save it as `.xlsx` or CSV.

### Matching What Is Already There

An import must land on the variant the warehouse already holds. When it does
not, the consequence is not a missing row: the wizard materialises a new
variant, the database's own `Barcode automatico articolo` automation stamps it
with a sequence number, and the article ends up twice in the catalogue - once
with its real barcode, once with an invented one.

A row is resolved in this order:

1. **Barcode**, when the file carries one. Exact, and it wins outright.
2. **Article code**, exactly as spelled, against `default_code`. Deliberately
   exact: this catalogue holds `BU-ART 4` (a shawl) *and* `BU-ART4` (a long
   skirt), and `BG-P 922277` and `BG-P922277` are two different polos. Codes
   that differ only by a space are **not** the same article here, so matching
   them loosely would receive goods against the wrong product - a worse
   outcome than a duplicate.
3. **Colour and size, against the variants that exist**, before anything is
   created (`_find_variant_by_values`). Compared on normalised value names, so
   a row saying `ACQUA MARINA` finds the variant filed under `ACQUAMARINA`
   rather than building a second one beside it; the catalogue already carries
   pairs like that, and `BEIGE 172` / `BEIGE172`. Attributes the row says
   nothing about are ignored, so a row with a colour and no size still finds
   the single variant of a one-size article.

Only when none of that matches does the wizard create, and then:

- a row matching **several** variants is refused and reported rather than
  guessed at - a file that leaves the Taglia column empty for multi-size
  articles used to produce a sizeless phantom variant per row;
- a variant created for a row whose file **carries a barcode** is stamped with
  it, taking precedence over the sequence number the automation assigns - only
  ever on a variant created here, and only when no other product holds that
  barcode.

Attribute values are looked up the same loose way before a new one is created,
so an import stops adding `ACQUAMARINA` next to `ACQUA MARINA`.

### Telling the Worker What Was Already There

When rows land on variants the catalogue already held, the import ends on a
dialog instead of going straight back to the arrival. A banner gives the count
("210 variants of 14 articles were already in the catalogue"), and under it a
grid laid out like the Distribution tab: one row per variant (barcode, colour,
size), one column per warehouse that holds any of them - the arrival's own
first and tinted - and a total. Each article opens with a heading row carrying
its subtotals. Empty cells are a faint dot, so only real stock stands out. The
grid scrolls under a sticky header, so a reassortment of hundreds of variants
stays usable. **OK** closes it and the arrival reloads with its new lines.

The grid is also saved on the arrival (`existing_html`) and shown in its own
**Already in the Catalogue** tab, which only appears once an import has found
something - so it is still there after the dialog is closed. A later import
that finds nothing already known leaves it as it was.

The chatter keeps a small table instead, one row per article with its
variants and the pieces already in stock (up to 30 articles,
`EXISTING_REPORT_LIMIT`), and points to the tab for the per-warehouse detail.
Written out as sentences, every warehouse of every article ran into one dense
block nobody could scan.

- *Already held* means the variant existed before this file was read. Anything
  the import itself created has a higher id and is not reported.
- A variant that already had a line on **this** arrival is left out, so
  re-importing a corrected file does not flag every row of the first attempt.
- Stock is summed per warehouse over internal locations, counting only what is
  actually there (a quantity above zero). It is read while the arrival is
  still a draft, so it is the stock from before these goods came in.
- It is information, not a block: the rows are imported either way. Goods that
  come back in on a reassortment are normal.
- The banner sentence is built in Python with the grid, so `_()` translates it
  whole. Text split around inline fields in a view is stored as fragments
  that the Italian catalogue easily fails to match.
- The first version was a sticky notification with one text line per variant.
  It was unreadable at 210 variants, and its follow-up action crashed the web
  client (`Cannot read properties of undefined (reading 'map')`): Odoo adds
  `views` to an action a button returns, but not to one nested in a
  notification's `next`.

### Templates for Workers

Two ready-to-fill templates live in `static/csv_templates/`, so they can be
handed out as a file or as a download link:

| File | URL | Use |
|---|---|---|
| `modello_arrivo.csv` | `/di_stock_arrival/static/csv_templates/modello_arrivo.csv` | Record what arrived; divide it in Odoo afterwards |
| `modello_arrivo_con_divisione.csv` | `/di_stock_arrival/static/csv_templates/modello_arrivo_con_divisione.csv` | Same, plus one pre-filled column per destination warehouse |

Both are semicolon-separated UTF-8 (the wizard's defaults) and carry two
`ESEMPIO-*` rows to delete — coded that way so a forgotten example row is
reported as a skipped row rather than quietly creating a product.
`static/csv_templates/ISTRUZIONI.md` documents them in Italian for warehouse
staff.

The `PTREM` / `PTF` / `MTREM` columns of the second file are placeholders: the
header has to match a warehouse's short code or name on the database it is
used against.

**Required columns:** `codice` (internal reference), `quantita`.

**Price columns**, mirroring the four ESC listini:

| CSV column | ESC listino | Written to |
|---|---|---|
| `prezzo_acquisto` | 4 ACQUISTO | `standard_price` (cost, per variant) |
| `prezzo_dettaglio` | 2 VENDITA | `list_price` and the **Dettaglio** pricelist |
| `prezzo_promo` | 1 VENDITA SCONTATO | **Promo** and **Promo Franchising** pricelists |
| `prezzo_ingrosso` | 3 VENDITA INGROSSO | the **Ingrosso** pricelist |

Pricelists are matched by name and skipped if absent. Existing rules for the
product are updated rather than duplicated. Untick *Update Prices* to leave
prices alone.

**Other optional columns:** `nome`, `colore`, `taglia`, `categoria`, `marca`,
`stagione`, `reparto`, `posizionamento`, `barcode`. English header names are
accepted too.

`codice` is written to **each variant**, not the template: Odoo stores
`default_code` per variant and reports it as empty on a template with several
variants, which is why every variant of `CS-320` carries `CS-320`.

**Distribution columns:** any additional column named after a warehouse's short
code (`PTREM`, `PTF`, `MTREM`, …) is read as a quantity to send there, so the
split can be prepared in Excel and the document arrives ready to validate.

**Barcodes are not imported.** Odoo mints its own via the
`dimensione.barcode.articolo` sequence and an on-create automation; supplier
barcodes are discarded by policy. Only supply a `barcode` column to match a
product that already has one.

Products and variants that do not exist are created when *Create Missing
Products* is ticked, and are marked available in POS: everything received here
is retail stock. Both `Colore` and `Taglia` use dynamic variants, so a
combination that has never existed is materialised on demand. Rows that cannot
be resolved are skipped and reported in the chatter rather than aborting the
whole import.

## Languages

The module ships English source strings and an Italian catalogue in `i18n/`,
so it follows each user's own language preference: a worker set to Italiano
gets Italian labels, buttons, help text and import messages, one set to
English gets the source text. Nothing is hardcoded per site.

| File | Role |
|---|---|
| `i18n/di_stock_arrival.pot` | Extracted source terms, no translations |
| `i18n/it.po` | Italian |

Odoo loads these when the module is installed or updated, for every language
already active on the database. To add Italian to a database that does not
have it: **Settings → Translations → Languages**, then
`odoo-bin -u di_stock_arrival` to load the catalogue.

**Every entry taken from code needs its kind marked**, on its own comment
line above the references:

```po
#. module: di_stock_arrival
#. odoo-javascript
#: code:addons/di_stock_arrival/static/src/distribution_matrix/distribution_matrix.xml:0
msgid "Save Distribution"
msgstr "Salva Distribuzione"
```

Odoo reads `.po` files straight from disk for code terms and filters them by
that comment: `odoo-javascript` for anything under `static/src` (both `_t()`
calls and the literal text in an OWL template), `odoo-python` for `_()`. An
entry without it is silently dropped - the catalogue looks complete, `msgfmt`
is happy, and the grid still comes out in English. Terms the web client
cannot find in this module fall back to Odoo's own dictionary, which is why
*Discard* and *Qty* appeared in Italian while *Save Distribution*, from this
module, did not. A real export adds the comments; a hand-edited entry has to
carry them too.

After changing any user-facing string, re-export from a running instance so
the terms stay in step:

```bash
odoo-bin --i18n-export=i18n/di_stock_arrival.pot --modules=di_stock_arrival -d <db>
```

## Printing Cartellini

**Print Tags** appears once the arrival is confirmed — the goods have to be in
before they can be labelled — and prints through `di_tag_export`'s existing
report.

It sends the **quantities** along with the products, not just the products:
three pieces of the same size and colour get three identical labels. The stack
comes out in the **receiving warehouse's shelving order** - at ING - CONTESSE
ABBI SCA, ACCE / BORS, ABBI APP, ACCE SCA, ascending barcode inside each -
which is `di_tag_export`'s `models/tag_order.py`, the order every screen
prints cartellini in.

The report reads the ids from `active_ids` when `docids` is empty: a report
action that carries data gets a URL with the options and the context but no
ids, so browsing `docids` alone printed a single blank page.

Capped at 10000 tags (`TAG_PRINT_LIMIT`), which only catches a mistyped
quantity. The cap used to be 1000: the tag report rendered every label as an
HTML document of its own, each loading Odoo's whole report stylesheet, and an
arrival of a few thousand pieces never came back from wkhtmltopdf.
`di_tag_export` now groups the labels into documents of 250
(`TAGS_PER_DOCUMENT`) and works out each variant's prices and barcode once
rather than once per piece. Grouping alone was not enough: wkhtmltopdf keeps
every page of a run in memory and inherits the Odoo worker's memory cap, so
4,500 labels in one run were killed with error -11. A print larger than one
document is therefore rendered as one wkhtmltopdf run per 250 labels and the
PDFs joined in order (`di_tag_export/models/ir_actions_report.py`), so the
worker still downloads a single file.

## The Transfer Note (Delivery Slip)

The receipt note printed from a transfer - Odoo's *Delivery Slip*,
`stock.report_delivery_document` - is adjusted for a warehouse that only ever
prints it for itself.

- **It prints in the language of whoever presses Print.** Core takes the
  language from the partner, which is right for a delivery note that ends up
  in a customer's hands; suppliers imported from ESC carry `en_US`, so an
  Italian warehouse got an English *Goods Receipt Note* for every arrival.
  `stock.picking._get_report_lang()` is overridden to answer with the user's
  own language for `incoming` and `internal` transfers. Outgoing transfers to
  a real customer keep Odoo's behaviour.
- **One quantity column, not two.** *Ordered* and *Delivered* repeated the
  same number on every line of a document that is printed after validation.
  Only *Delivered* is kept.
- **A Barcode column** sits next to each product, so a line can be checked
  against the cartellino without looking the article up.
- **A total** sits once in the header, after the operator: how many pieces
  the document moves, counted from the records rather than from the printed
  rows, which are merged by product. It used to be the table's footer, and
  wkhtmltopdf repeats a table footer at the bottom of every page.
- **The destination** is named on an internal transfer - the shop it is bound
  for, by warehouse name. It is the one note of the three with no address
  block of its own (a receipt shows the warehouse, a delivery the customer),
  so where the goods were going appeared nowhere on the page.

The columns are changed in three templates at once - the header, the
aggregated row and the one-row-per-lot variant - because the table picks its
row template at render time and the cells have to stay under the right
heading. `report/report_delivery_document.xml` says so in comments; the
anchors were checked against the arch as installed.

## Configuration

**Inventory → Configuration → Settings → Warehouse → Arrival Warehouse** sets
where goods land. It is a company setting rather than a hardcoded constant; if
unset it falls back to the warehouse with code `ICONT`, then to any warehouse.

## Security

| Group | Can |
|---|---|
| Stock Arrivals / User | Create, import, divide and validate arrivals |
| Stock Arrivals / Manager | The above, plus deleting draft or cancelled arrivals |

Workers validate their own arrivals; no manager counter-signature is required.
Both groups imply `stock.group_stock_user`.

The two groups sit under their own **Stock Arrivals** privilege
(`res.groups.privilege`, the Odoo 19 replacement for `res.groups.category_id`)
in the Supply Chain category. It is deliberately separate from stock's
"Inventory" privilege: a privilege renders as a single selector on the user
form, so sharing one would make these groups alternatives to Inventory
User/Administrator rather than additions to them.

## Cancelling

An arrival can only be cancelled while in **Draft**. Once confirmed it has
already moved stock, so it must be reversed with a return or an inventory
adjustment — that keeps the stock movement traceable rather than silently
rewriting history.

## Requirements

- Odoo 19.0
- `stock`, `product`, `mail`

The Studio taxonomy fields (`x_studio_Prodotti2Marca`,
`x_studio_prodotti2stagione`, `x_studio_prodotti2reparto`,
`x_prodotti2posizionamento`) and their models are used by the importer when
present, and skipped when absent, so the module installs on a clean database.

## Installation

1. Copy `di_stock_arrival` into your Odoo addons path.
2. Restart the Odoo server.
3. Update the apps list.
4. Install `Stock Arrivals (Arrivi Merce)`.

## Updating

```bash
odoo-bin -u di_stock_arrival
```

## Files

- `di_stock_arrival/__manifest__.py`: module metadata and dependencies
- `di_stock_arrival/models/stock_arrival.py`: document, workflow, stock moves
- `di_stock_arrival/models/stock_arrival_line.py`: received lines and quantities
- `di_stock_arrival/models/stock_arrival_allocation.py`: per-warehouse split
- `di_stock_arrival/models/res_company.py`: arrival warehouse setting
- `di_stock_arrival/models/stock_picking.py`: link back from generated transfers, and the report language
- `di_stock_arrival/wizard/stock_arrival_import.py`: CSV import
- `di_stock_arrival/wizard/stock_arrival_divide.py`: bulk distribution
- `di_stock_arrival/security/`: groups, record rules and access rights
- `di_stock_arrival/views/`: list, form, search, wizards, settings and menus
- `di_stock_arrival/report/`: the transfer note (delivery slip) overrides
- `di_stock_arrival/i18n/`: extracted terms (`.pot`) and the Italian catalogue
- `di_stock_arrival/static/csv_templates/`: blank templates for workers, with instructions
- `di_stock_arrival/static/src/distribution_matrix/`: the distribution grid (OWL component, template, styles)
