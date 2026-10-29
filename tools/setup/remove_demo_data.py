"""Remove Odoo's demo data, keeping only the company's real data.

The database was created with demo data: two demo companies (San Francisco,
the "main company", and Chicago), demo products, contacts, employees, POS shops
and their orders/invoices. The real company (Dimensione Immagine) is itself
Odoo's Italian demo company renamed, so it is kept explicitly.

Run inside the Odoo container with `odoo shell` (see tools/esc_import/load_products.py
for the command). DRY_RUN (default) rolls everything back and only reports;
set DRY_RUN = False before the script to commit. Back up the database first.

What is kept: the real company and all its records, the ESC import
(esc_import.* xmlids), records defined by modules' data files (e.g. the Tips or
Gift Card products), the users' own contacts (no demo xmlid).
"""
import csv
import io
import os
from collections import Counter

from lxml import etree
from odoo.modules.module import get_manifest
from odoo.tools.misc import file_open

DRY_RUN = globals().get("DRY_RUN", True)
COMPANY = env.ref("base.user_admin").company_id
SUDO = env(su=True, context=dict(env.context, active_test=False, tracking_disable=True,
                                 _force_unlink=True, force_delete=True))
IMD = SUDO["ir.model.data"]
print(f"Keeping {COMPANY.name} (id {COMPANY.id}), dry run: {DRY_RUN}")


def file_xmlids(module, kind):
    """xmlids of the records written by a module's data or demo files."""
    ids = set()
    for name in (get_manifest(module) or {}).get(kind, []):
        try:
            content = file_open(os.path.join(module, name), "rb").read()
        except OSError:
            continue
        if name.endswith(".xml"):
            for el in etree.fromstring(content).iter("record", "template", "menuitem"):
                if el.get("id"):
                    mod, _, xid = el.get("id").rpartition(".")
                    ids.add((mod or module, xid))
        elif name.endswith(".csv"):
            for row in csv.DictReader(io.StringIO(content.decode("utf-8"))):
                if row.get("id"):
                    mod, _, xid = row["id"].rpartition(".")
                    ids.add((mod or module, xid))
    return ids


modules = SUDO["ir.module.module"].search([("state", "=", "installed")]).mapped("name")
demo_xmlids, data_xmlids = set(), set()
for module in modules:
    demo_xmlids |= file_xmlids(module, "demo")
    data_xmlids |= file_xmlids(module, "data")
# Records only written by demo files (demo files also tweak data records, e.g. activate EUR)
demo_xmlids -= data_xmlids


def snapshot():
    """Record count per model, overall and for the kept company."""
    counts = {}
    for model in SUDO.registry.values():
        if model._abstract or model._transient or not model._auto or model._name.startswith("ir."):
            continue
        records = SUDO[model._name]
        field = records._fields.get("company_id")
        try:
            with SUDO.cr.savepoint():
                total = records.search_count([])
                ours = records.search_count([("company_id", "=", COMPANY.id)]) if (
                    field and field.store and field.comodel_name == "res.company") else None
        except Exception:
            continue
        counts[model._name] = (total, ours)
    return counts


def unlink(records, failed):
    """Delete records; on failure, split the batch to delete what can be."""
    records = records.exists()
    if not records:
        return 0
    try:
        with SUDO.cr.savepoint():
            records.unlink()
        # some models archive instead (e.g. bank accounts already used)
        return len(records - records.exists())
    except Exception as error:
        if len(records) == 1:
            failed[records._name, records.id] = str(error).splitlines()[0][:160]
            return 0
        half = len(records) // 2
        return unlink(records[:half], failed) + unlink(records[half:], failed)


before = snapshot()
esc_before = Counter(IMD.search([("module", "=", "esc_import")]).mapped("model"))
failed = {}

# 1. The real company becomes Odoo's main company; system users move to it.
demo_companies = SUDO["res.company"].search([("id", "!=", COMPANY.id)])
for xmlid, record in (("main_company", COMPANY), ("main_partner", COMPANY.partner_id)):
    IMD.search([("module", "=", "base"), ("name", "=", xmlid)]).write({"res_id": record.id})
IMD.search([("module", "=", "base"), ("name", "in", ("demo_company_it", "partner_demo_company_it"))]).unlink()
system_users = SUDO.ref("base.user_root") | SUDO.ref("base.user_admin") | SUDO.ref("base.public_user") \
    | SUDO.ref("base.template_portal_user_id")
system_users.write({"company_id": COMPANY.id, "company_ids": [(6, 0, COMPANY.ids)]})
system_users.partner_id.filtered("company_id").write({"company_id": COMPANY.id})


def repoint(xmlid, record):
    """Make a module's xmlid designate the real company's own record, so that
    upgrading the module neither recreates nor overwrites it."""
    module, name = xmlid.split(".")
    if record:
        IMD.search([("module", "=", module), ("name", "=", name)]).write({"res_id": record.id, "noupdate": True})


def same(model, record, field="display_name"):
    return SUDO[model].search([("company_id", "=", COMPANY.id)]).filtered(lambda r: r[field] == record[field])[:1]


# 2. Records defined by modules' data files (not demo) belonging to the demo companies:
#    point their xmlid to the real company's equivalent, or move them to it
gift_card = SUDO["loyalty.program"].search([("company_id", "=", COMPANY.id), ("program_type", "=", "gift_card")], limit=1)
repoint("stock.warehouse0", SUDO["stock.warehouse"].search([("company_id", "=", COMPANY.id)], order="id", limit=1))
repoint("hr.employee_admin", SUDO["hr.employee"].search([("company_id", "=", COMPANY.id), ("user_id", "=", SUDO.ref("base.user_admin").id)], limit=1))
repoint("loyalty.gift_card_program", gift_card)
repoint("loyalty.gift_card_program_reward", gift_card.reward_ids[:1])
repoint("loyalty.gift_card_program_rule", gift_card.rule_ids[:1])
for i in range(5):
    repoint(f"di_bonus_points.bonus_point_bracket_{i}", same("bonus.point.bracket", SUDO.ref(f"di_bonus_points.bonus_point_bracket_{i}")))
for rule in ("donna", "uomo"):
    repoint(f"di_bonus_points.bonus_point_category_rule_{rule}", same("bonus.point.category.rule", SUDO.ref(f"di_bonus_points.bonus_point_category_rule_{rule}"), "name"))
repoint("di_bonus_points.bonus_point_settings_default", SUDO["bonus.point.settings"].search([("company_id", "=", COMPANY.id)], limit=1))
moved = []
for data in IMD.search([("res_id", "!=", False)]):
    if (data.module, data.name) not in data_xmlids or data.model not in SUDO or data.model.startswith("ir."):
        continue
    record = SUDO[data.model].browse(data.res_id).exists()
    if "company_id" in record and record.company_id in demo_companies:
        vals = {"company_id": COMPANY.id}
        if "address_id" in record and record.address_id in demo_companies.partner_id:
            vals["address_id"] = COMPANY.partner_id.id
        if "parent_id" in record and record.parent_id and record.parent_id._name == record._name:
            vals["parent_id"] = False
        try:
            with SUDO.cr.savepoint():
                record.write(vals)
            moved.append(f"{data.module}.{data.name}")
        except Exception as error:
            print(f"cannot move {data.module}.{data.name}: {error}")
print("moved to the company:", moved)
# Files (e.g. web.image_placeholder) are kept
SUDO["ir.attachment"].search([("company_id", "in", demo_companies.ids)]).write({"company_id": COMPANY.id})

# 3. What to delete
demo_users = SUDO["res.users"].search([("id", "not in", system_users.ids)])
demo_employees = SUDO["hr.employee"].search([("company_id", "in", demo_companies.ids)])
leftover_partners = (demo_users.partner_id | demo_companies.partner_id | demo_employees.work_contact_id) \
    - system_users.partner_id
first = ["pos.order", "pos.session", "pos.config", "sale.order", "purchase.order", "account.payment",
         "account.bank.statement.line", "account.move", "stock.picking", "stock.move", "stock.quant",
         "loyalty.card", "loyalty.program", "hr.employee", "hr.department"]
company_models = [m for m in first if m in SUDO] + sorted(
    name for name, model in SUDO.registry.items()
    if name not in first and not model._abstract and not model._transient and model._auto
    and name not in ("res.users", "res.company", "ir.attachment")
    and (f := model._fields.get("company_id")) and f.store and f.comodel_name == "res.company")

# By id: once a company is deleted, its remaining records no longer point to it
company_records = {name: set(SUDO[name].search([("company_id", "in", demo_companies.ids)]).ids)
                   for name in company_models}
demo_records = {}
for data in IMD.search_read([("module", "!=", "esc_import")], ["module", "name", "model", "res_id"]):
    if (data["module"], data["name"]) in demo_xmlids or data["name"].startswith(f"{COMPANY.id}_demo_"):
        # also the demo invoices/bank statements generated for the company's chart of accounts
        demo_records.setdefault(data["model"], set()).add(data["res_id"])
# The demo POS shops' products and categories come from their scenario files
pos_data = {xid for mod, xid in data_xmlids if mod == "point_of_sale"} | {
    "product_product_tip", "product_product_tip_product_template"}
for data in IMD.search([("module", "=", "point_of_sale"), ("model", "in", (
        "product.template", "product.product", "product.combo", "product.combo.item", "pos.category",
        "product.category"))]):
    if data.name not in pos_data:
        demo_records.setdefault(data.model, set()).add(data.res_id)
# A demo variant means a demo product (its other variants have no xmlid)
demo_records.setdefault("product.template", set()).update(
    SUDO["product.product"].browse(demo_records.get("product.product", ())).exists().product_tmpl_id.ids)
# Demo attributes used by the ESC products (Taglia, Colore) are real now
esc_templates = SUDO["product.template"].browse(
    IMD.search([("module", "=", "esc_import"), ("model", "=", "product.template")]).mapped("res_id"))
used_attributes = esc_templates.attribute_line_ids.attribute_id
demo_records["product.attribute"] = demo_records.get("product.attribute", set()) - set(used_attributes.ids)
demo_records["product.attribute.value"] = demo_records.get("product.attribute.value", set()) - set(
    used_attributes.value_ids.ids)
# Demo bank statement lines loaded with the chart of accounts, without xmlid
demo_records.setdefault("account.bank.statement.line", set()).update(SUDO["account.bank.statement.line"].search([
    ("company_id", "=", COMPANY.id), ("create_uid", "=", SUDO.ref("base.user_root").id)]).ids)
# Empty POS sessions opened by the temporary test user
demo_records.setdefault("pos.session", set()).update(SUDO["pos.session"].search([
    ("user_id", "in", demo_users.ids), ("order_ids", "=", False)]).ids)
demo_records.pop("res.company", None)

# 4. Delete, in rounds, until nothing more can be
for _round in range(10):
    deleted = 0
    for name in company_models:
        deleted += unlink(SUDO[name].browse(company_records[name]), failed)
    deleted += unlink(SUDO["account.account"].search([("company_ids", "in", demo_companies.ids)]).filtered(
        lambda a: not (a.company_ids - demo_companies)), failed)
    for model, ids in demo_records.items():
        if model in SUDO:
            deleted += unlink(SUDO[model].browse(ids), failed)
    # bonus points earned by demo customers
    deleted += unlink(SUDO["bonus.point.batch"].search([("partner_id", "in", list(demo_records.get("res.partner", ())) + leftover_partners.ids)]), failed)
    deleted += unlink(demo_users, failed) + unlink(demo_companies, failed) + unlink(leftover_partners, failed)
    if not deleted:
        break
    print(f"round {_round + 1}: {deleted} records deleted")
SUDO["res.users"].search([("id", "not in", system_users.ids)]).write({"active": False})

# 5. Products only used by the deleted demo loyalty programs
reward_products = SUDO["product.template"].search([
    ("create_date", "<", COMPANY.create_date.replace(hour=23)), ("type", "=", "service"),
    ("id", "not in", IMD.search([("model", "=", "product.template")]).mapped("res_id"))])
reward_products = reward_products.filtered(lambda t: not SUDO["loyalty.reward"].search_count(
    ["|", ("discount_line_product_id", "in", t.product_variant_ids.ids),
     ("reward_product_id", "in", t.product_variant_ids.ids)]) and not SUDO["loyalty.rule"].search_count(
    [("product_ids", "in", t.product_variant_ids.ids)]) and not SUDO["loyalty.program"].search_count(
    [("trigger_product_ids", "in", t.product_variant_ids.ids)]))
print("unused reward products:", reward_products.mapped("name"))
unlink(reward_products, failed)

# 6. No demo data for modules installed later
SUDO["ir.module.module"].search([("demo", "=", True)]).write({"demo": False})

# Report
after = snapshot()
esc_after = Counter(IMD.search([("module", "=", "esc_import")]).mapped("model"))
print("\nDeleted per model (all / of the kept company):")
for name in sorted(before):
    total, ours = before[name]
    total2, ours2 = after.get(name, (0, 0))
    if total != total2:
        lost = f" / {ours - ours2} OF {COMPANY.name.upper()}" if ours is not None and ours != ours2 else ""
        print(f"  {name}: {total - total2}{lost}")
print("ESC import changes:", {m: esc_before[m] - esc_after[m] for m in esc_before if esc_before[m] != esc_after[m]} or "none")
failed = {key: error for key, error in failed.items() if SUDO[key[0]].browse(key[1]).exists()}
print(f"\nNot deleted ({len(failed)}):")
for (model, rid), error in sorted(failed.items())[:80]:
    print(f"  {model},{rid}: {error}")
print("\nRemaining: companies", SUDO["res.company"].search([]).mapped("name"),
      "| POS", SUDO["pos.config"].search_count([]), "| users", SUDO["res.users"].search([]).mapped("login"),
      "| products not ESC", SUDO["product.template"].search(
          [("id", "not in", IMD.search([("module", "=", "esc_import"), ("model", "=", "product.template")]).mapped("res_id"))]).mapped("name"))
print("Contacts:", sorted(SUDO["res.partner"].search([]).mapped("name")))
print("Without company:", {name: SUDO[name].search_count([("company_id", "=", False)]) for name in (
    "stock.location", "stock.picking.type", "stock.route", "stock.rule", "stock.warehouse", "resource.calendar")})

if DRY_RUN:
    env.cr.rollback()
    print("\nDRY RUN: rolled back")
else:
    env.cr.commit()
    print("\nCommitted")
