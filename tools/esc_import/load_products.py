"""Load an ESC catalogue snapshot (see extract_products.py) into Odoo.

Run inside the Odoo container with `odoo shell`, e.g.:
    docker cp tools/esc_import/esc_products.json.gz my-odoo-addons-odoo-1:/tmp/
    docker cp tools/esc_import/load_products.py my-odoo-addons-odoo-1:/tmp/
    echo "exec(open('/tmp/load_products.py').read())" | docker exec -i my-odoo-addons-odoo-1 \
        odoo shell -d dev --db_host db --db_user odoo --db_password odoo --no-http

Optional variables, set before the exec():
    DATA_FILE  snapshot path (default /tmp/esc_products.json.gz)
    LIMIT      only import the first N articles still to import
    DRY_RUN    True to roll everything back at the end

Each article becomes a product template with Size/Color attributes and only the
size/colour combinations that exist in ESC as variants. Templates get the
external id esc_import.article_<ESC ID>, so the import is resumable: articles
already imported are skipped.
"""
import gzip
import json
import time

DATA_FILE = globals().get("DATA_FILE", "/tmp/esc_products.json.gz")
LIMIT = globals().get("LIMIT")
DRY_RUN = globals().get("DRY_RUN", False)
CHUNK = 200
XMLID_MODULE = "esc_import"
TAX_NAME = "22% (incl.)"

# Products are created in the admin user's default company (prices in its currency).
COMPANY = globals().get("COMPANY") or env.ref("base.user_admin").company_id
env = env(context=dict(env.context, tracking_disable=True, mail_create_nolog=True, mail_notrack=True,
                       allowed_company_ids=[COMPANY.id]))


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def get_or_create(model, domain, vals):
    record = env[model].with_context(active_test=False).search(domain, limit=1)
    return record or env[model].create(vals)


def get_sale_tax():
    """22% VAT-included sales tax, copied from the chart of accounts' 22% when there is one."""
    domain = [("type_tax_use", "=", "sale"), ("company_id", "=", COMPANY.id)]
    tax = env["account.tax"].search(domain + [("name", "=", TAX_NAME)], limit=1)
    if tax:
        return tax
    base_tax = env["account.tax"].search(domain + [("amount", "=", 22.0), ("amount_type", "=", "percent")], limit=1)
    if base_tax:
        return base_tax.copy({"name": TAX_NAME, "price_include_override": "tax_included"})
    return env["account.tax"].create({
        "name": TAX_NAME, "amount": 22.0, "amount_type": "percent", "type_tax_use": "sale",
        "company_id": COMPANY.id, "price_include_override": "tax_included",
    })


def get_attribute_values(attribute_name, names_with_sequence):
    """Return {value name: product.attribute.value id}, creating missing values."""
    attribute = get_or_create("product.attribute", [("name", "=", attribute_name)],
                              {"name": attribute_name, "create_variant": "always"})
    existing = {v.name: v.id for v in attribute.with_context(active_test=False).value_ids}
    missing = [{"attribute_id": attribute.id, "name": name, "sequence": sequence}
               for name, sequence in sorted(names_with_sequence.items(), key=lambda i: (i[1], i[0]))
               if name not in existing]
    for value in env["product.attribute.value"].create(missing):
        existing[value.name] = value.id
    return attribute, existing


def get_categories(articles):
    cache = {}
    Category = env["product.category"]

    def category(path):
        if path not in cache:
            parent = category(path[:-1]) if len(path) > 1 else Category
            cache[path] = get_or_create("product.category",
                                        [("name", "=", path[-1]), ("parent_id", "=", parent.id or False)],
                                        {"name": path[-1], "parent_id": parent.id or False})
        return cache[path]

    result = {}
    for article in articles:
        path = tuple(p for p in (article["famiglia"] or "ALTRO", article["gruppo"]) if p)
        result[article["id"]] = category(path).id
    return result


def get_tags(articles):
    names = {a["brand"] for a in articles if a["brand"]} | {f"Reparto: {a['reparto']}" for a in articles if a["reparto"]}
    existing = {t.name: t.id for t in env["product.tag"].search([("name", "in", list(names))])}
    for tag in env["product.tag"].create([{"name": n} for n in sorted(names - set(existing))]):
        existing[tag.name] = tag.id
    return existing


def get_studio_values(articles):
    """Brand and department in the production Studio fields (from di_studio_fields), when installed.

    Returns {product.template field: (article key, {name: record id})}.
    """
    result = {}
    for field, model, key in (("x_studio_Prodotti2Marca", "x_marche", "brand"),
                              ("x_studio_prodotti2reparto", "x_reparto", "reparto")):
        if field not in env["product.template"]._fields:
            continue
        existing = {r.x_name: r.id for r in env[model].search([])}
        names = {a[key] for a in articles if a[key]} - set(existing)
        for record in env[model].create([{"x_name": n} for n in sorted(names)]):
            existing[record.x_name] = record.id
        result[field] = (key, existing)
    return result


def run():
    started = time.time()
    with gzip.open(DATA_FILE, "rt", encoding="utf-8") as f:
        articles = json.load(f)["articles"]
    log(f"snapshot: {len(articles)} articles")

    done = set(env["ir.model.data"].search([
        ("module", "=", XMLID_MODULE), ("model", "=", "product.template")]).mapped("name"))
    todo = [a for a in articles if f"article_{a['id']}" not in done]
    if LIMIT:
        todo = todo[:LIMIT]
    log(f"already imported: {len(done)}, to import now: {len(todo)}")
    if not todo:
        return

    tax = get_sale_tax()
    sizes, colours = {}, {}
    for article in articles:
        for v in article["variants"]:
            sizes[v["size"]] = min(sizes.get(v["size"], 10**6), v["size_order"])
            colours.setdefault(v["colour"], 0)
    # Same attribute names as production (the di_* modules look them up by name)
    size_attr, size_values = get_attribute_values("Taglia", sizes)
    colour_attr, colour_values = get_attribute_values("Colore", colours)
    categories = get_categories(todo)
    tags = get_tags(todo)
    studio = get_studio_values(todo)
    used_barcodes = set(env["product.product"].with_context(active_test=False).search(
        [("barcode", "!=", False)]).mapped("barcode"))
    env.cr.commit() if not DRY_RUN else None
    log(f"setup done: tax '{tax.name}', {len(size_values)} sizes, {len(colour_values)} colours, "
        f"{len(set(categories.values()))} categories, {len(tags)} tags")

    stats = {"templates": 0, "variants": 0, "barcodes": 0, "barcode_conflicts": 0, "failed": []}
    Template = env["product.template"].with_context(create_product_product=False)

    def template_vals(article):
        variants = article["variants"]
        tag_ids = [tags[n] for n in (article["brand"], f"Reparto: {article['reparto']}") if n in tags]
        return {
            "name": article["name"],
            "company_id": COMPANY.id,
            "type": "service" if article["service"] else "consu",
            "is_storable": not article["service"],
            "categ_id": categories[article["id"]],
            "list_price": article["list_price"],
            "standard_price": article["cost"],
            "taxes_id": [(6, 0, [tax.id])],
            "supplier_taxes_id": [(5, 0, 0)],
            "product_tag_ids": [(6, 0, tag_ids)],
            **{field: values.get(article[key], False) for field, (key, values) in studio.items()},
            "attribute_line_ids": [
                (0, 0, {"attribute_id": size_attr.id,
                        "value_ids": [(6, 0, sorted({size_values[v["size"]] for v in variants}))]}),
                (0, 0, {"attribute_id": colour_attr.id,
                        "value_ids": [(6, 0, sorted({colour_values[v["colour"]] for v in variants}))]}),
            ],
        }

    def import_chunk(chunk):
        templates = Template.create([template_vals(a) for a in chunk])
        variant_vals = []
        for article, template in zip(chunk, templates):
            ptav = {(line.attribute_id.id, value.product_attribute_value_id.id): value.id
                    for line in template.attribute_line_ids for value in line.product_template_value_ids}
            for v in article["variants"]:
                barcode = next((b for b in v["barcodes"] if b not in used_barcodes), False)
                if v["barcodes"] and not barcode:
                    stats["barcode_conflicts"] += 1
                if barcode:
                    used_barcodes.add(barcode)
                variant_vals.append({
                    "product_tmpl_id": template.id,
                    "product_template_attribute_value_ids": [(6, 0, [
                        ptav[(size_attr.id, size_values[v["size"]])],
                        ptav[(colour_attr.id, colour_values[v["colour"]])],
                    ])],
                    "default_code": article["code"],
                    "barcode": barcode,
                    "standard_price": article["cost"],
                })
        variants = env["product.product"].create(variant_vals)
        env["ir.model.data"].create([{
            "module": XMLID_MODULE, "name": f"article_{a['id']}", "model": "product.template",
            "res_id": t.id, "noupdate": True,
        } for a, t in zip(chunk, templates)])
        stats["templates"] += len(templates)
        stats["variants"] += len(variants)
        stats["barcodes"] += sum(1 for vals in variant_vals if vals["barcode"])

    for start in range(0, len(todo), CHUNK):
        chunk = todo[start:start + CHUNK]
        barcodes_before = set(used_barcodes)
        try:
            with env.cr.savepoint():
                import_chunk(chunk)
        except Exception as error:  # isolate the faulty article(s) and keep going
            used_barcodes.clear()
            used_barcodes.update(barcodes_before)
            env.invalidate_all()
            log(f"chunk at {start} failed ({error}); retrying one by one")
            for article in chunk:
                try:
                    with env.cr.savepoint():
                        import_chunk([article])
                except Exception as single_error:
                    env.invalidate_all()
                    stats["failed"].append((article["id"], article["code"], str(single_error)[:200]))
        if not DRY_RUN:
            env.cr.commit()
        env.invalidate_all()
        elapsed = time.time() - started
        progress = min(start + CHUNK, len(todo))
        eta = elapsed / progress * (len(todo) - progress)
        log(f"{progress}/{len(todo)} articles, {stats['variants']} variants, "
            f"elapsed {elapsed / 60:.1f} min, ETA {eta / 60:.1f} min")

    if DRY_RUN:
        env.cr.rollback()
        log("DRY RUN: rolled back")
    log(f"DONE in {(time.time() - started) / 60:.1f} min: {stats['templates']} products, "
        f"{stats['variants']} variants, {stats['barcodes']} barcodes, "
        f"{stats['barcode_conflicts']} barcodes skipped (already used), {len(stats['failed'])} failed")
    for failure in stats["failed"][:20]:
        log("FAILED", *failure)


run()
