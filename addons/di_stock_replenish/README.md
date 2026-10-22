# Stock Replenishment (Riassortimento)

Compares what a shop has on the shelf against what the central warehouse still
holds, so the products that have run out can be spotted and sent.

## Why It Exists

The workers' question is: *which products is PV-Silvi out of, that ING-CONTESSE
still has, in this season?* Standard Odoo cannot answer it.

**A product with no stock at a shop has no `stock.quant` record there.** Quants
are created by movement, so something that never reached the shop — or sold out
and was cleaned up — simply has no row. The Locations report, pivoted by product
and warehouse, renders a blank a person can read but a filter cannot. The
absence is not queryable anywhere in standard Odoo, and the absence is the whole
question.

The legacy ESC "Genera Picking" screen answered it by showing `Esistenza` per
branch beside the article. This is that screen.

## The Flow

1. **Compare** — pick one or more sources, the shops, and any of the seasons, brands or
   categories, and read the gaps.
2. **Plan** — type quantities straight into the destination cells. A cell is
   **one number**: empty, it shows the shop's own stock as a placeholder, so
   the zero worth correcting is the thing you click on; typed in, it turns
   bold and becomes the quantity to send. The shop's stock stays on the cell's
   tooltip. The document is a **draft** while this happens.
3. **Print** — the document (a pick list, one block per destination, with a
   tick column) and the cartellini for what is on it.
4. **Adjust** — a product that could not be found is removed on the *To
   Transfer* tab, or cleared back to zero in the grid.
5. **Confirm** — generates **one internal transfer per source and destination**, source
   stock to the shop, and leaves each one **Ready** with its stock reserved.
   Nothing moves yet: the worker picks the pile with the printed sheet and
   cartellini, opens the transfer (the *Transfers* button on the document,
   or Inventory › Operations › Transfers, or the Barcode app) and scans the
   pieces against it, and validates it there once the quantities agree. The
   document becomes *Transfers Generated* and stops being editable; it can be
   cancelled again only once its transfers have been cancelled in Inventory.

   **Print Document** prints one sheet per destination, each on its own
   page(s), headed with the shop and listing its transfer. **Print Tags**
   prints the cartellini in destination piles, each pile opened by a blank
   card naming the shop and its piece count, so the stack can be cut without
   reading a label.

   **Posizionamento** is a chip picker above the grid like the seasons,
   offering **APP and SCA only** - `x_posizionamenti` also holds three dozen
   leftovers from the old software's import that nobody filters on - stored
   as ids in `position_keys` and read the same defensive way
   (`x_prodotti2posizionamento` → `x_posizionamenti`). The grid shows it as
   a column beside the season.

Nothing moves until step 5. A confirmed document cannot be edited or deleted:
it has moved stock, so it is reversed with a transfer of its own.

## Several Sources

**Origine** takes more than one warehouse, so a worker can compare the shops
against two stockrooms at once instead of running the comparison twice. Each
source gets its own free-to-use column, and a product is listed when **any**
source has some free.

The destination cells stay **one number each**. Confirming fills every line
from the sources in the order the field lists them, moving to the next only
once the first has nothing left (`_allocate_sources`), and what one line takes
is taken off for the lines after it. A variant sits in one source only on
nearly the whole catalogue, so there is rarely anything to choose; the order
matters only for the few held in both.

A transfer leaves from a single location, so a shop served from two sources
gets **two transfers**, one per building. With more than one source, the
printed sheet adds a column saying where each line is picked
(`MCP 2 · ING 1`): before confirming it is the split confirming would make
now, afterwards it is read off the transfers themselves, since re-running the
split then would count the document's own reservations as gone.

Existing documents keep their single source: `migrations/19.0.12.0.0` copies
the old `source_warehouse_id` column into the new relation.

## The Source Guard

A quantity larger than the sources can give together is refused, and the check **adds up
across destinations** — three shops asking for four each is only a problem
once you total them. It runs against *free to use*, not on hand, so stock
already reserved for another transfer is not offered twice.

The grid warns as you type and turns the row red; the server refuses it again
on save and once more on confirm. The client warning is a courtesy, the server
check is the guard.

## The Screen

**Inventory → Replenishment → Comparisons.**

Pick one or more sources (defaults to the arrival warehouse from `di_stock_arrival`), pick
the shops to look at, optionally narrow by season, brand or product category,
then **Refresh**.

| Column | Shows | Why that measure |
|---|---|---|
| Source | **Free to use** | Stock already reserved for another transfer cannot be sent anywhere, so offering it would create a picking nobody can fulfil. |
| Each destination | **On hand** | What is actually on the shop's shelf, reservations included — a reserved item is still stock the shop has. |

Each row also carries the detail a worker needs to act without leaving the
screen: **barcode**, **brand**, **supplier**, and — behind a tick — the
**sales price** and the **promo price**. The filter box searches all of them,
so scanning a barcode into it jumps to the row.

The two price columns are **hidden until the tick beside the filter box is
set**, because the grid is normally read down the quantity columns and two
more numbers per row push them off the side. The tick is labelled with the
column headings themselves, and is a per-session display choice: it is not
saved on the document, so reopening the comparison starts with prices hidden
again.

The sales price is `product.product.lst_price`, so a variant carrying an
attribute price extra shows its own price rather than its template's. The
promo price comes from the *Promo* pricelist, matched by name with the same
`Promo (EUR)` fallback the arrival importer uses — and never from *Promo
Franchising*. A rule on the variant wins over one on the template; a product
with no rule shows blank rather than 0.00.

A highlighted zero is the thing worth acting on. **Only Missing** (on by
default) keeps only rows where **at least one** chosen destination has none —
with several shops selected, requiring *all* of them to be empty would hide
almost everything.

Products with no free stock at any source are never listed: there is nothing to
send.

**The form has no chatter and no width cap.** The grid is the whole screen,
and the side panel cost it a third of the width for a log nobody reads here.
`mail.thread` stays on the model, so state changes are still tracked. The
form's `di_full_width_form` class (`static/src/full_width_form/`, shared with
`di_pos_replenish`) lifts Odoo's sheet and renderer max widths for this view
only.

**Columns can be dragged.** Every heading after the product one can be
dropped onto another to move its column to that side; the frozen product
column stays put. The order is a per-browser convenience in `localStorage`
(one order for every comparison, since warehouse ids are stable), and a shop
added later slots in after the column it follows by default. The ↺ button
beside Refresh puts the default back.

**Enter moves down a shop's column**, Shift+Enter up, like filling a
spreadsheet: type 1, Enter, type 2, Enter. Moving focus is what commits the
cell left behind, and the colour/size sub-rows, which have no input, are
stepped over.

## Rows Are Variants, Not Articles

The comparison groups `stock.quant` by `product_id`, which is
`product.product` — **the variant**. A row is one colour in one size, and the
product column shows it that way: `[BQ-DIF-SL11045] CAMICIA (CACAO, 46)`.

This is deliberate and it is the point of the screen. A shop is rarely out of
an article; it is out of *the 42 in black* while the 44 sits on the rail. The
warehouse may well hold that exact variant, and that exact variant is what gets
sent.

## Reading the Numbers

One grouped query per warehouse, on `stock.quant`, over internal locations
under that warehouse's view location.

`available_quantity` is computed and **not stored**, so it cannot be aggregated
in a `read_group`. The free quantity is derived from the two stored columns
instead: `quantity - reserved_quantity`.

## Editing Quantities From the Grid

`set_replenish_cells` carries **only the cells that changed**, including
explicit zeroes for cleared ones. It is deliberately *not* authoritative for
the whole document the way the arrival's distribution grid is: the comparison
is paged, so a payload can never describe the rows a worker cannot currently
see, and treating it as complete would delete them. A key that is absent is
left alone.

## The Printable Document

One block per destination — the sheet is carried round the stockroom and
picked shop by shop — with a tick column, a subtotal per destination and the
generated transfers listed once confirmed.

Values are printed with `<span t-field="..."/>` **inside** each cell, never
`<td t-field="..."/>`. QWeb asserts on the second form, because the field
widget renders its own wrapper markup that would be invalid inside a row, and
it only fails when the report is rendered — not at install.

**The sheet carries no literal words.** Every heading comes from
`report_labels()`, which reads the label of the field it prints, and the state
badge from that field's selection label — the same reason the grid's headings
do. Free text in a QWeb template is a view term, and view terms do not reach
this module's catalogue on every database, so the sheet would otherwise print
English next to an Italian screen. Where no field carries the word, the sheet
uses a symbol instead: `Σ` for the subtotal, and an empty header over the
column of tick boxes.

## Printing Cartellini

Every row carries a tick box in the product column, and the header box ticks
or clears the rows currently on screen — respecting the filter box, so
filtering to a brand and ticking the header selects just that brand.

**A selection survives paging.** The ticks are held by product id, not by row,
so a worker can work through several pages of gaps and print once at the end.
The count next to the button is the running total.

Printing hands the ticked variants to `di_tag_export`'s existing
`action_report_product_tag`, which owns the layout, the 70×38 mm paper format
and the pricelists the tag shows — this module only decides which variants go
into it, and gets one page per variant back. The button carries the report's
own name, so it says *Cartellino* in any language.

The document header's **Print Tags** button follows the saved transfer plan
instead: it prints one cartellino per planned piece. A quantity of two prints
two labels for that variant; when the same variant is planned for more than
one destination, the labels remain in destination piles.

A pile is **one destination out of one source**, since that is where the
pieces are walked out of (`_line_allocations`: the split confirming would
make, or once confirmed, the transfers themselves). Inside a pile the labels
follow that source's shelving order - ABBI SCA, ACCE / BORS, ABBI APP, ACCE
SCA, by barcode inside each, or Donna then Uomo at MAG - CENTRALE PISTUNINA -
and with more than one pile each opens with a blank card naming the shop, the
source, or both. Both rules are `di_tag_export`'s `models/tag_order.py`,
shared with every other screen that prints cartellini. The grid's ticked
print is one pile, in the source's order when the document has one source and
the general order when it has several.

`di_tag_export` is therefore a real dependency, not an optional one. The button
still hides itself if the report is missing.

## Paging

The grid shows **500 rows at a time** (`ROW_LIMIT`) with a pager, so the whole
comparison is reachable — 1254 products is three pages, not a truncated list.
Rows are ordered by **ascending barcode**, with all variants sharing an
internal reference kept together as a block placed at that block's lowest
barcode — so a product code is never split across a page break. Barcodes are
compared as numbers, since they are 6, 8 and 13 digits long here and sort
wrongly as text; rows without one go last. The order is total and stable, so
slicing it into pages is safe.

The comparison itself is recomputed on every page, because the *Only Missing*
filter depends on the full result; only the ordered page of products is sent
to the browser. Refreshing after changing a filter returns to page one, since
the old offset would point into a different result.

**The filter box searches the loaded page only** — hence its "Filter this
page…" label. Narrowing by season, brand or category first is the way to
search the whole catalogue.

## Where the Column Headings Come From

**This module's own code translations do not reach `_()` on every database.**
The headings came out English whether they sat in the OWL template or were
resolved server-side with `_()`, while the *field* labels on the same form
(`Solo Mancanti`, `Origine`) were correctly Italian — those load by a
different mechanism.

So the grid does not use its own catalogue for headings. Every column shows a
core field, and `_matrix_labels()` borrows that field's own label:

| Column | Label taken from |
|---|---|
| Product | the `product.product` model name |
| Barcode | `product.product.barcode` |
| Brand | the Studio brand field on `product.template` |
| Season | the Studio season field on `product.template` |
| Category | `product.template.categ_id` |
| Supplier | `product.supplierinfo.partner_id` |
| Sales Price | `product.product.lst_price` |
| Promo | the pricelist name — not a word, identical in either language |
| free to use / on hand | `stock.quant.available_quantity` / `.quantity` |

Those are translated on every Odoo database, so the grid speaks the same
words as the rest of Inventory. `_()` remains the fallback for a database
where a field is absent.

Anything the grid can express as a number is expressed as one for the same
reason: the pager reads `1-500 / 3428` and the gap counter `500 / 500`,
neither of which needs translating.

## The Three Filters

Categories and brands take **several values at once**, and both appear as a
column in the grid so a row can be read without going back to the product.

**Categories** are an ordinary `Many2many` on `product.category`, shown as tags
in the form. Child categories are included (`child_of`).

**Brands** are a `Many2many` too, on the Studio model `x_marche`, shown with
the same `many2many_tags` widget right beside the categories. There are 379 of
them, so type-to-search with Odoo's own autocomplete beats any chip list.
`no_create` is set: this is a filter over the brands that exist, never a place
to invent one.

> **This is why the module now requires `x_marche` to exist.** Binding a
> Studio model in a field declaration means `di_stock_replenish` will not
> install on a database without that taxonomy — where previously it degraded
> to hiding the picker. The trade was made deliberately, for the widget.
> `pricelist_extend` already binds the same model the same way. Think hard
> before adding a third: a fresh database has none of these models.

**Seasons** stay chips above the grid rather than a relation. The season lives
on `product.template` as the Studio field `x_studio_prodotti2stagione`,
pointing at the Studio model `x_stagioni`. There are 77 of them and they are
picked constantly, so they belong above the grid where they are being read.
The chosen ids are kept as text in `season_keys` and resolved when the
comparison runs; where the taxonomy is absent the picker does not appear.

Several values **of one kind widen** the search (any of these seasons), while
the kinds **together narrow** it (this brand *and* that season) — which is how
a worker asks the question.

A season whose whole name is one letter — `A`, `Z`, `C`, `E`, `I`, `M`, `P`,
`Q` — is also the **family** of every season starting with that letter: one
click on `A` picks A, A17, A18 … A26 together, a second click drops them all,
and the chip lights up only once its whole family is picked. The individual
chips still toggle one at a time inside it. That is how the workers ask for
it ("tutte le A"), and it costs the letter nothing: it is still a real
season of its own, selected along with the rest.

Picking a season writes the selection back through the same call that returns
the comparison, so a choice is one round trip and survives reopening the
document. It always returns to page one — a different season is a different
comparison, and the old offset would point into the wrong one. Brands and
categories are ordinary form fields, so they take effect on **Refresh**, which
saves the record before it re-reads.

`di_stock_arrival`'s importer looks the same fields up the same defensive way,
and for the same reason.

**Discount %** is a plain single-value select, unlike the other two: only one
discount tier applies to a product at a time, so there is nothing to widen by
picking several. It filters against the Promo pricelist a Sales Price
already has a column for - (Sales Price − Promo Price) / Sales Price,
**rounded to the nearest 10%**.

**Promo Price** is the same kind of select, but exact rather than rounded: a
tag says 9.95, not "about 10". Its options are every Promo price present in
the comparison after the other filters have narrowed it — not a fixed list,
so it never offers a price nothing on the page is at. A product with no rule
on the Promo pricelist has no promo price, so it drops out once a price is
picked. Stored as text in `price_bucket`, the way `discount_bucket` is.

**Colours and sizes.** The arrow in front of a row opens it onto every
variant of the same style — one sub-row per colour and size, with the same
free-at-source and on-hand-at-destination columns as the row above, the
variant the row is about in bold. Read on demand through
`get_variant_breakdown`, not sent with the page: a page is five hundred rows
and almost none of them get opened. The filters deliberately do not apply
there — the question is "what else of this style exists", and a sibling a
filter hid is still an answer.

Rounded, not exact, because real promo prices land close to a round number
but very rarely exactly on one: a product priced 49.99 → 29.95 is a 40.088%
discount by the numbers, and nobody sets a promo price by first deciding on
40.088%. Checked against the whole Promo pricelist (1,958 products), rounding
to the nearest 10% produces a clean set of buckets that match how the
discounts were actually set - large, obviously-intentional clusters at 30%,
50%, 70%, 80%, 90%, smaller ones at 20%/40%/60% - rather than the 143 distinct
near-values an unrounded comparison would scatter across.

**"None" is its own real option**, not the same thing as "Tutti" (no filter).
76 products on this database sit on the Promo pricelist priced at exactly
Sales Price - no actual markdown - and "None" finds precisely those. A
product not on the Promo pricelist at all does not appear under any bucket,
"None" included: it has no promo price to compare, so there is nothing to
round.

**The option list is computed fresh on every call**, from whatever the
comparison currently holds - after Filiale, categories, brands and Only
Missing have already narrowed it, before Discount % narrows it further. It
never offers a bucket nothing in the current comparison is actually at, and
it is never a hardcoded list: the business's own discount tiers can change
without this module needing a code change to notice.

## Security

Reuses the `di_stock_arrival` groups — the same workers do both jobs:

| Group | Can |
|---|---|
| Stock Arrivals / User | Create and run comparisons |
| Stock Arrivals / Manager | The same |

## Languages

English source strings and an Italian catalogue in `i18n/`, following each
user's language preference. Re-export after changing any user-facing string:

```bash
odoo-bin --i18n-export=i18n/di_stock_replenish.pot --modules=di_stock_replenish -d <db>
```

## Requirements

- Odoo 19.0
- `stock`, `product`, `di_stock_arrival`

`di_stock_arrival` is a real dependency, not a convenience: this module reuses
its groups, its arrival-warehouse company setting, and the grid styling its
asset bundle loads. `di_tag_export` is one too: the Print button hands it the
ticked variants and it produces the cartellini.

## Files

- `models/stock_replenish.py`: the comparison document and the stock reading
- `views/stock_replenish_views.xml`: form, list and action
- `views/stock_replenish_menus.xml`: menus under Inventory
- `static/src/replenish_matrix/`: the comparison grid (OWL component, template, styles)
- `i18n/`: extracted terms and the Italian catalogue
