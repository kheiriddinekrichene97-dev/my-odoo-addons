"""Create the ESC deposits as Odoo warehouses and load their stock (see extract_stock.py).

Run inside the Odoo container with `odoo shell`, after load_products.py:
    docker cp tools/esc_import/esc_stock.json.gz my-odoo-addons-odoo-1:/tmp/
    docker cp tools/esc_import/load_stock.py my-odoo-addons-odoo-1:/tmp/
    echo "exec(open('/tmp/load_stock.py').read())" | docker exec -i my-odoo-addons-odoo-1 \
        odoo shell -d dev --db_host db --db_user odoo --db_password odoo --no-http

Optional variables, set before the exec():
    DATA_FILE  snapshot path (default /tmp/esc_stock.json.gz)
    LIMIT      only load the first N stock lines still to load
    DRY_RUN    True to roll everything back at the end

Warehouses get the external id esc_import.warehouse_<ESC deposit ID>. Stock is
set with inventory adjustments (one done stock move per line, like Inventory >
Physical Inventory). It is resumable: a variant that already has stock in a
warehouse is skipped.
"""
import gzip
import json
import time
from collections import defaultdict

DATA_FILE = globals().get("DATA_FILE", "/tmp/esc_stock.json.gz")
LIMIT = globals().get("LIMIT")
DRY_RUN = globals().get("DRY_RUN", False)
CHUNK = 2000
XMLID_MODULE = "esc_import"
INVENTORY_NAME = "Giacenza iniziale ESC"

COMPANY = globals().get("COMPANY") or env.ref("base.user_admin").company_id
env = env(context=dict(env.context, tracking_disable=True, mail_create_nolog=True, mail_notrack=True,
                       allowed_company_ids=[COMPANY.id]))


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def get_warehouses(deposits):
    """{ESC deposit id: stock.warehouse}, creating the missing ones."""
    IMD = env["ir.model.data"]
    result = {}
    for deposit in deposits:
        xmlid = f"warehouse_{deposit['esc_id']}"
        data = IMD.search([("module", "=", XMLID_MODULE), ("name", "=", xmlid)], limit=1)
        warehouse = env["stock.warehouse"].with_context(active_test=False).browse(data.res_id).exists()
        if not warehouse:
            warehouse = env["stock.warehouse"].create({
                "name": deposit["name"], "code": deposit["code"],
                "sequence": deposit["sequence"], "company_id": COMPANY.id,
            })
            IMD.create({"module": XMLID_MODULE, "name": xmlid, "model": "stock.warehouse",
                        "res_id": warehouse.id, "noupdate": True})
            log(f"created warehouse {warehouse.code} {warehouse.name}")
        result[deposit["esc_id"]] = warehouse
    # Archive the company's other warehouses when they hold nothing (e.g. the default "WH")
    for other in env["stock.warehouse"].search([("company_id", "=", COMPANY.id),
                                                ("id", "not in", [w.id for w in result.values()])]):
        if not env["stock.quant"].search_count([("location_id", "child_of", other.view_location_id.id),
                                                 ("quantity", "!=", 0)]):
            other.active = False
            log(f"archived empty warehouse {other.code} {other.name}")
    return result


def get_variant_map():
    """{(ESC article id, size, colour): product.product id} for the imported products."""
    template_by_article = {int(x["name"].split("_", 1)[1]): x["res_id"] for x in env["ir.model.data"].search_read(
        [("module", "=", XMLID_MODULE), ("model", "=", "product.template")], ["name", "res_id"])}
    ptav_names = {}
    for ptav in env["product.template.attribute.value"].search_read(
            [("product_tmpl_id", "in", list(template_by_article.values()))], ["attribute_id", "name"]):
        ptav_names[ptav["id"]] = (ptav["attribute_id"][1], ptav["name"])
    by_template = defaultdict(dict)
    for variant in env["product.product"].search_read(
            [("product_tmpl_id", "in", list(template_by_article.values()))],
            ["product_tmpl_id", "product_template_attribute_value_ids"]):
        values = dict(ptav_names[p] for p in variant["product_template_attribute_value_ids"])
        by_template[variant["product_tmpl_id"][0]][(values.get("Taglia"), values.get("Colore"))] = variant["id"]
    return {(article_id, size, colour): product_id
            for article_id, template_id in template_by_article.items()
            for (size, colour), product_id in by_template[template_id].items()}


def run():
    started = time.time()
    with gzip.open(DATA_FILE, "rt", encoding="utf-8") as f:
        data = json.load(f)
    log(f"snapshot: {len(data['deposits'])} deposits, {len(data['stock'])} stock lines")

    warehouses = get_warehouses(data["deposits"])
    if not DRY_RUN:
        env.cr.commit()
    variants = get_variant_map()
    location_by_deposit = {esc_id: w.lot_stock_id.id for esc_id, w in warehouses.items()}
    stocked = {(q["product_id"][0], q["location_id"][0]) for q in env["stock.quant"].search_read(
        [("location_id", "in", list(location_by_deposit.values())), ("quantity", "!=", 0)],
        ["product_id", "location_id"])}
    log(f"{len(variants)} variants mapped, {len(stocked)} variant/warehouse pairs already stocked")

    # Sum per variant and warehouse: two ESC colours can share a name, hence an Odoo variant.
    quantities, missing = defaultdict(float), 0
    for article_id, size, colour, deposit, qty in data["stock"]:
        product_id = variants.get((article_id, size, colour))
        if not product_id:
            missing += 1
            continue
        key = (product_id, location_by_deposit[deposit])
        if key not in stocked:
            quantities[key] += qty
    todo = [{"product_id": product_id, "location_id": location_id, "inventory_quantity": qty}
            for (product_id, location_id), qty in quantities.items()]
    if LIMIT:
        todo = todo[:LIMIT]
    log(f"to load now: {len(todo)} lines ({sum(v['inventory_quantity'] for v in todo):.0f} pieces); "
        f"{missing} lines skipped (variant not imported)")

    Quant = env["stock.quant"].with_context(inventory_mode=True, inventory_name=INVENTORY_NAME)
    for start in range(0, len(todo), CHUNK):
        quants = Quant.create(todo[start:start + CHUNK])
        quants.action_apply_inventory()
        if not DRY_RUN:
            env.cr.commit()
        env.invalidate_all()
        done = min(start + CHUNK, len(todo))
        elapsed = time.time() - started
        log(f"{done}/{len(todo)} lines, elapsed {elapsed / 60:.1f} min, "
            f"ETA {elapsed / done * (len(todo) - done) / 60:.1f} min")

    if DRY_RUN:
        env.cr.rollback()
        log("DRY RUN: rolled back")
    log(f"DONE in {(time.time() - started) / 60:.1f} min")


run()
