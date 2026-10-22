# Stock by Location (Ubicazioni)

A **Locations** tab, after Inventory, on the product and on the variant -
shaped differently for each, because they answer different questions.

## On the product: the old screen, rebuilt

Reproduces "Esistenza per colori articolo" from the shop's previous
management software: every colour down the side, every size across the top,
and - under each colour - one row per warehouse in the company, **including
a zero row for a shop that carries none of it** (dimmed, not hidden). A
Filiale filter narrows to a single warehouse.

**Every one of the company's `stock.warehouse` records - all 22, exactly as
Configuration > Warehouses lists them - never anything more.** An earlier
version of this grid tried to narrow that list to warehouses "actually in
use", reasoning from the fact that most warehouses show a zero row for any
one product. Checked directly, that reasoning did not hold: 21 of the 22
warehouses hold stock of *something* company-wide, so an activity filter
would have removed exactly one (`MAG - LECCE`) and changed almost nothing -
solving a problem that was not the actual complaint. What was actually
being asked was narrower: show real Odoo warehouses, all of them, and never
one of the old software's own that is not real here - its Filiale dropdown
named a store, `GIORDANO BRUNO`, this Odoo does not have at all. That is all
`_di_matrix_warehouse_domain` does now: filter by `company_id`, nothing
else.

Verified row for row against a real screenshot of the old screen, for
`BQ-TM11313`:

| | L | M | S | XL | XXL | Totali |
|---|---|---|---|---|---|---|
| BIANCO — old screen | 4 | 0 | 0 | 17 | 8 | **29** |
| BIANCO — this module | 4 | 0 | 0 | 17 | 8 | **29** |
| PV - TREMESTIERI — old screen | 0 | 0 | 0 | 7 | 3 | **10** |
| PV - TREMESTIERI — this module | 0 | 0 | 0 | 7 | 3 | **10** |
| PV - TORRE FARO — old screen | 2 | 0 | 0 | 7 | 3 | **12** |
| PV - TORRE FARO — this module | 2 | 0 | 0 | 7 | 3 | **12** |

Zero rows are shown on purpose. This tab answers "which shops carry this
colour at all", not just "where does stock currently sit" - an absent row
cannot say no.

**Structured after the old screen, styled as native Odoo.** A title bar
naming the product sits above the grid (with a collapse arrow, since the
full grid runs long), and one thing is carried over exactly: a colour's own
total row prints an explicit `0` for a size it doesn't have
("BIANCO 4 0 0 17 8 29"), but a location row underneath leaves that cell
**blank** rather than printing a zero - only the row's own Totali column
gets a number, even when that number is 0. The bold total says "none of
this size"; the blank cell says "nothing here to report on this shelf".

The *colours* are not the old screen's own navy/blue, though - an earlier
version hardcoded a fixed palette to look like a screenshot of the old
software, and got corrected: this grid should look like the rest of
Inventory, in whichever theme that is. It reads its colours from Odoo's own
SCSS variables (`$o-view-background-color`, `$o-brand-primary`), the exact
mechanism `di_stock_arrival`'s distribution grid already uses, and for the
same documented reason: Bootstrap's `var(--bs-*)` custom properties are not
exposed in Odoo's backend bundle, so `var(--border-color, #ccc)`-style rules
were always silently falling back to their hardcoded fallback value, never
actually reading the theme.

**Takes the tab's full width.** Getting there took two separate fixes, not
one. A grid whose body scrolls (`overflow-x: auto`) needs `width: 100%` and
`min-width: 0` on its own root or the scrolling child quietly caps the whole
box to its content width - the same fix `di_stock_arrival`'s grid already
needed. That alone was not enough here: Odoo wraps every field widget in its
own `<div class="o_field_<widget name> ...">`, and because this field never
carries a real value, Odoo marks that wrapper `o_field_empty` and collapses
it to **0 width** - confirmed directly in a browser's devtools. A component
filling 100% of a 0-wide parent is still 0. The real fix targets that
wrapper by its generated class name (`o_field_di_color_size_matrix`)
directly, forcing it back to `display: block; width: 100%`.

## Speaks whichever language Odoo is set to

Every word this module writes itself - "Warehouse", "All", "Total",
"Loading...", "Expand" / "Collapse", the title bar's "Stock by colour:" -
goes through `_t()` and is translated from `i18n/it.po`, the same mechanism
this database already uses correctly (verified directly: `fields_get` on an
existing field under `lang=it_IT` vs `lang=en_US` returns two different
strings, so custom-module translations do reach users here).

**"Filiale" is the Italian translation of "Warehouse", not a separate
word invented for this screen** - the English source string is the generic
concept; `it.po` supplies the old software's own term for it.

**The Colore / Taglia column headers are not translated text at all** -
they are the real `product.attribute` records' own names, read live and
handed to the grid by the server. Whatever this database's attributes are
actually called, in whatever language they were entered, is what shows -
translating a hardcoded word here would say "Colour" over data that is
still going to read "BIANCO" and "SMERALDO" regardless.

## On a variant: still the flat list

One variant already has one fixed colour **and** one fixed size, so a
colour/size grid for it would be a single cell - not useful. The variant tab
stays what it always was: every internal location holding that variant, one
row each, only where it actually has stock (no zero rows here - the question
being answered is "where is it", and an empty location has nothing to add).

Both are read-only. The **Quantity On Hand** figure elsewhere on the form is
a link (`action_open_quants`) whose action carries `'inventory_mode': True` -
it opens the editable adjustment screen, where typing a number corrects real
stock. Neither tab is one keystroke from that.

## Two filters the old screen had are gone

The old screen's dropdowns were Filiale, Colore Fornitore, Stagione, Anno.
Checked against this database: only **Filiale** (`stock.warehouse`) is real
data that still applies here. **Stagione** does not apply to a per-product
grid the way it did to the old screen's product search - the template is
already fixed once you are on its form, so a season filter has nothing left
to narrow. **Colore Fornitore** and **Anno** name data nothing in this
database has ever captured; inventing fields to satisfy them would be
guessing at a business meaning, not fixing anything. If either turns out to
matter, that is a real scoping conversation about what field they should be,
not something to fake here.

The old **Filiale** dropdown itself listed a store this Odoo does not have
(`GIORDANO BRUNO`) - stale data left in the old system's own list. This
module's Filiale list is read live from `stock.warehouse`.

## Sizes sort numerically where they can

`product.attribute.value.sequence` is unset (0) for every Taglia value on
this database, so there is no curated garment order (S < M < L < XL) to
read. Sizes that are plain numbers (`44`, `52`) sort as numbers; named sizes
(`L`, `M`, `XL`) sort alphabetically - which happens to reproduce the old
screen's own order (`L, M, S, XL, XXL`) exactly, and is honest about being
alphabetical rather than a hardcoded garment-size table that would be wrong
for a different attribute's values. If a real garment order matters, the fix
is curating `sequence` on the attribute values, not code here.

## What this replaced

An earlier iteration shipped this as a separate module,
`di_stock_color_size_matrix`, opened from its own menu with a product search
box - the closest match to how the old software's own window worked. It was
folded back into this module and the standalone menu removed: once the grid
is reached from the product you are already looking at, a product picker is
one field this form doesn't need, and two modules answering overlapping
"where is my stock" questions is one more than the answer needs.

An iteration before that put a flat quant list at the foot of General
Information and a `Locations` **column** in the Product Variants list. The
column was dropped separately: at an average of 3.7 lines per row it made an
80-row page roughly four times taller, for less than the tab now gives for
free.

## Anchors

Every view inherit was checked against the *resolved* arch on the live
database via `get_view()` before being written:

| View | Model | Anchor |
|---|---|---|
| `product.product_template_only_form_view` | `product.template` | `//page[@name='inventory']` |
| `product.product_normal_form_view` | `product.product` | `//page[@name='inventory']` |
| `product.product_variant_easy_edit_view` | `product.product` | `//sheet` (no notebook on this form) |

`product.product` has **two** form views in play, and the obvious one is not
the one the shop uses: `product.product_variant_action` (the Product
Variants menu, and the Variants button on a template) pins `form` to
`product_variant_easy_edit_view`. That form also carries no `is_storable`,
which a field named in an `invisible` expression has to be present in the
view to be read - hence the hidden field added there.
