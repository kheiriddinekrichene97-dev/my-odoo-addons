# POS Stock by Shop

Answers "who else has it?" at the till, in the order a cashier actually needs
it, while a customer waits.

## What was already there

Core Odoo 19 already ships most of this, which is worth writing down so nobody
rebuilds it a third time:

- `product.template.get_product_info_pos()` returns a `warehouses` list with
  every warehouse in the company, each with `free_qty`,
  `forecasted_quantity` and its UoM, sorted so this register's own warehouse
  comes first.
- `ProductInfoPopup` renders that list as its `section-inventory` block.
- `product_screen.js` opens that popup on a **500ms long press** of a product
  card - `useLongPress`, bound for both mouse and touch - without adding
  anything to the order.

None of that needed writing. What needed fixing was the reading.

## What this module changes

**The list is all 22 warehouses, every time.** Checked against real data on the
staging build: a typical garment sits in 4 shops, so the popup printed 4 useful
rows and 18 rows of `0 Units available, 0 forecasted`, in no particular order.
This module reshapes that one section into:

- a summary line - *In stock at 4 of 22 shops · 8 Units in total*;
- the shops that hold it, **this register's shop first**, then by quantity,
  then by name;
- the empty shops behind a *Show the 18 shops with none* toggle.

**The empty shops are folded, not dropped.** `di_stock_locations` makes the
case for keeping zero rows on the backend grid - "which shops carry this at
all" is a real question and an undrawn row cannot answer it. That reasoning
still holds; it just shouldn't cost the cashier 18 lines before the answer they
asked for. Both questions stay answerable, one of them one tap further away.

**PV and MAG stay separate rows.** A shop and the warehouse behind it are
genuinely different places to send someone, and the data says so: for all three
products sampled, `PV - TORRE FARO` held none while `MAG - TORRE FARO` held
stock. Merging them would have reported the shop floor as stocked when it is
empty.

**A badge on the product card.** The long press above already worked; it is
simply not discoverable. The badge is a label for it and calls the same
`pos.onProductInfoClick`, so there is one code path, not two. It swallows
`mousedown`/`touchstart` so core's long-press timer never starts from the badge
itself, and stops its click from reaching the card's own add-to-order handler.

It sits in the card's **top-right** corner. The top-left is already taken:
`di_bonus_points` puts its gift badge there (`top-0 start-0`), and the first
version of this badge covered it up. Of the remaining corners, core uses
top-right only for `comboExtraPrice` inside the combo popup - which this badge
is hidden from anyway - and bottom-right for the cart quantity.

## Scope

Read-only. No cart, pricing, tax, payment or order-sync code is touched; the
only backend change is one extra field name in a POS load list.

## Why stock.picking.type carries an extra field

`get_product_info_pos` sorts this register's warehouse first but never says
which one it is, and the popup then re-sorts by quantity, so position stops
identifying it. `stock.picking.type._load_pos_data_fields` therefore also loads
`warehouse_id`, whose id is matched against the `id` core already puts on each
warehouse row.

`stock.warehouse` is deliberately **not** added to the POS's loaded models for
this: a relation pointing at an unloaded model degrades to its raw id on the
frontend, and a raw id is all the comparison needs.

## Styling

The SCSS names no Odoo or Bootstrap variable, using `currentColor` and neutral
alphas instead. An undefined SCSS variable fails the whole
`point_of_sale._assets_pos` bundle, and a failed POS bundle is a register that
will not open - too large a downside for colour that follows the theme just as
well this way.
