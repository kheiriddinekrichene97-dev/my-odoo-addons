# Sales Replenishment (Riassortimento di Vendita)

The daily job as the shop floor actually does it: take the till receipts from
the day before, shop by shop, and put the same pieces back on the shelf out of
**ING - CONTESSE** and **MAG - CENTRALE PISTUNINA**.

On a Monday the period covers Saturday *and* Sunday, because the shops sell on
both and nobody restocks on the Sunday. The default is worked out from the day
the run is created, so on a normal morning the worker changes nothing and
presses Compute.

A new run lists every point of sale **except Torregrotta and Silvi**
(`DEFAULT_EXCLUDED_CONFIGS`), which are restocked on a different rhythm; they
can still be added by hand.

## What a run does

1. **Reads what sold.** Net quantity per variant per point of sale over the
   period. Net, because a return is a negative line at the till - whether it
   came through Odoo's refund flow or was typed as `-1` the way ESC did it -
   and a returned piece is already back on the shop's own shelf. The table
   shows sold, returned and net, so a worker can see why a number dropped.
   Only stocked products count: gift cards, services and the "Articolo ESC
   non migrato" stand-in for ESC's rounding lines have nothing to send back.
   Each shop's tab, and the counter above the table, show the till's own net
   total in pieces, the figure ESC prints, not a count of rows. That total
   also takes off returns that no sale of the same variant offsets in the
   period (a piece sold last week and brought back today). Those have no row,
   since there is nothing to replace, so the rows can add up to a little more
   than the tab.
2. **Puts the warehouses beside it.** Free stock (on hand less what is already
   reserved) at each source, and a proposed quantity to send capped by what is
   actually there: five sold with three in the warehouse sends three, and the
   row is flagged short by two.
3. **Orders the table the way it is read.** Posizionamento first - SCA, then
   APP, then anything else, then products with none - and inside that by
   product category, ABBI first and the rest alphabetically. A child category
   is shown by its own name (CAMI, GIAC, PANT); a category with no parent is
   shown as it is. Inside a group, rows run in ascending barcode without ever
   splitting a product code across the page.
4. **Lets a whole Stagione be dropped.** Press the chip and its rows leave the
   table, the cartellini and the transfers. It is a filter, not a delete:
   quantities already adjusted by hand come straight back if the season is
   picked up again.
5. **Prints the cartellini shop by shop.** One label per piece - two of a
   variant means two labels - in the order the warehouse picks in (see
   *Cartellini order* below). The layout is `di_tag_export`'s; this module
   only decides which variants go into it, how many of each, and where the
   piles are cut.
6. **Creates the transfers.** One internal transfer per source warehouse and
   shop, left **ready to validate** rather than validated: the paperwork is
   printed before the goods move, so the warehouse validates as it picks.

## Cartellini order

Every **source warehouse is its own pile**, sorted on its own: a low barcode
at one never pulls a label forward at the other. Shops follow the document's
order, warehouses the source field's.

| Warehouse | Order inside the pile |
|---|---|
| ING - CONTESSE, any other, or none (product list) | 1. ABBI SCA · 2. ACCE / BORS · 3. ABBI APP · 4. ACCE SCA · then anything else |
| MAG - CENTRALE PISTUNINA (`MCP`) | 1. Donna (Reparto D, DN, D+, DO) · 2. Uomo (U, U+) · then anything else (B/O, UNI…) |

Inside each step, ascending barcode, compared as a number. Bags come before
the rest of ACCE SCA because every BORS at ING - CONTESSE is itself SCA. The
steps are `TAG_STEPS` and `TAG_STEPS_BY_REPARTO` in `di_tag_export`'s
`models/tag_order.py`, read live from the product. They are not this
module's: every screen that prints cartellini - the stock comparison, an
arrival, the product list - uses the same order.

When a print covers **more than one pile** - a shop served from both
warehouses, or the whole run - each pile opens with a **blank card** naming the
warehouse (and the shop, when there are several) with its piece count, so the
stack is cut without reading a label. A single pile prints no card.

## What is left off the table

A variant that sold but has **no free stock at any source and nothing
planned** gets no row on screen or on the printed sheet: there is nothing a
worker could send. It is hidden, not deleted (`_unstocked_line_ids`):

- the line is still built, so the shop's tab keeps the till's own total;
- it comes back by itself once stock reaches a source, with a cell to type
  into, because the check reads live stock every time the table loads;
- a row with something planned always shows, so a confirmed run - whose own
  transfers have reserved the stock it asked for - never loses its rows.

The line above the table says how many were left off, so the table being
shorter than the till receipt is never a mystery. The *All Rows* tab still
lists everything.

## The screen

The form has **no chatter and no width cap**: the grid is the whole screen.
It uses `di_stock_replenish`'s shared `di_full_width_form` class; the model
keeps `mail.thread`, so state changes are still tracked.

**Columns can be dragged** into any order except the frozen product column,
remembered in this browser under its own key, with ↺ beside Refresh to go
back to the default. **Enter moves down a warehouse's column**, Shift+Enter
up, skipping the group headings. Both come from `di_stock_replenish`'s
`static/src/column_order/column_order.js`, shared with the stock comparison
so the two screens behave alike.

## The two warehouses

A variant sits in one source or the other on nearly this whole catalogue - a
direct count against the live database put it at 90 variants in both, out of
some nine thousand - so there is normally nothing to prioritise. The proposal
fills from the first source that has the variant, which saves typing the
obvious case eight times a morning; where both hold it, both cells are
editable and the worker moves the quantity between them.

Nothing is proposed twice. The run carries a running count of what is left at
each source, so two shops that both sold the last piece cannot both be told to
take it - the first is served and the second sees the shortfall on its row.
A hand edit can still ask for more than exists across the shops; that is
refused on save, totalled across the whole document rather than one row.

## Translation

The module ships `i18n/it.po`, so switching a user's language switches the
screen. Column headings that show a core field borrow that field's own label
rather than translating a string of their own: Odoo translates its field
labels on every database, where a custom catalogue has to be loaded to reach
`_()`. The same reasoning applies to the printed document, whose headings are
built server-side - free text inside a QWeb template is a view term, and those
do not reach a custom module's catalogue on every database.

## Studio fields

Stagione, Posizionamento and Marca are hand-made Studio fields. They are read
by name and guarded with a presence check rather than declared as relations,
so the module installs on a database where Studio never made them. The
taxonomy is snapshotted onto each line when the run is built: reclassifying a
product next month must not rewrite last week's paperwork.

## Where it sits

Inventory ▸ Riassortimento ▸ Sales Replenishment, beside the stock comparison
from `di_stock_replenish`. The two answer the same question from opposite
ends - what a shop has run out of, and what a shop has just sold - and the
same workers use both.
