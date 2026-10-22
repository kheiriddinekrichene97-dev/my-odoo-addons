"""Create the Dettaglio / Ingrosso / Promo pricelists from an ESC snapshot.

Run inside the Odoo container with `odoo shell` (same as load_products.py) after
extract_pricelists.py, with the snapshot copied to /tmp/esc_pricelists.json.gz.

- Dettaglio has no rules: it is the product's Sales Price (ESC VENDITA).
- Ingrosso gets a fixed price per product from ESC VENDITA INGROSSO.
- Promo gets a fixed price only where ESC VENDITA SCONTATO differs from the
  Sales Price; elsewhere it falls back to the Sales Price.

Re-running replaces the Ingrosso and Promo rules with the snapshot's prices.
"""
import gzip
import json
import time

DATA_FILE = globals().get("DATA_FILE", "/tmp/esc_pricelists.json.gz")
XMLID_MODULE = "esc_import"
COMPANY = globals().get("COMPANY") or env.ref("base.user_admin").company_id
env = env(context=dict(env.context, allowed_company_ids=[COMPANY.id], tracking_disable=True))


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


with gzip.open(DATA_FILE, "rt", encoding="utf-8") as f:
    prices = json.load(f)

# Pricelists are hidden in the UI until the feature is enabled
if not env.user.has_group("product.group_product_pricelist"):
    env["res.config.settings"].create({"group_product_pricelist": True}).execute()
    log("enabled the Pricelists feature")

pricelists = {}
for name in ("Dettaglio", "Ingrosso", "Promo"):
    pricelist = env["product.pricelist"].search([("name", "=", name), ("company_id", "=", COMPANY.id)], limit=1)
    pricelists[name] = pricelist or env["product.pricelist"].create({
        "name": name, "company_id": COMPANY.id, "currency_id": COMPANY.currency_id.id})

templates = {int(x["name"].split("_", 1)[1]): x["res_id"] for x in env["ir.model.data"].search_read(
    [("module", "=", XMLID_MODULE), ("model", "=", "product.template")], ["name", "res_id"])}
list_prices = {t["id"]: t["list_price"] for t in env["product.template"].search_read(
    [("id", "in", list(templates.values()))], ["list_price"])}

items = {"Ingrosso": [], "Promo": []}
for article_id, article_prices in prices.items():
    template_id = templates.get(int(article_id))
    if not template_id:
        continue
    if "ingrosso" in article_prices:
        items["Ingrosso"].append((template_id, article_prices["ingrosso"]))
    promo = article_prices.get("promo")
    if promo and abs(promo - list_prices[template_id]) > 0.001:
        items["Promo"].append((template_id, promo))

for name, rows in items.items():
    pricelist = pricelists[name]
    pricelist.item_ids.unlink()
    for start in range(0, len(rows), 5000):
        env["product.pricelist.item"].create([{
            "pricelist_id": pricelist.id, "applied_on": "1_product", "product_tmpl_id": template_id,
            "compute_price": "fixed", "fixed_price": price,
        } for template_id, price in rows[start:start + 5000]])
    log(f"{name}: {len(rows)} product prices")
log("Dettaglio: no rules (uses each product's Sales Price)")
env.cr.commit()
