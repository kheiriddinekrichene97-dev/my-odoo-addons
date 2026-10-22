"""Create the ESC staff as Odoo employees and give them access to their shops' tills.

Run inside the Odoo container with `odoo shell` (same as load_products.py), with
the snapshot of extract_employees.py copied to /tmp/esc_employees.json.gz.

- Suspended people, technical accounts and till logins ("CV - ...") are skipped.
- Salespeople get the POS salesperson flag (di_pos_salesperson) and their ESC
  badge as Badge ID.
- At each shop's POS (by its ESC deposit): cashiers get the Basic tier (Cashier
  role of di_pos_roles), salespeople who are not cashiers the Minimal tier
  (Salesperson role).

Employees get the external id esc_import.employee_<name>; re-running updates them.
"""
import gzip
import json
import re

DATA_FILE = globals().get("DATA_FILE", "/tmp/esc_employees.json.gz")
DRY_RUN = globals().get("DRY_RUN", False)
XMLID_MODULE = "esc_import"
SKIP = {"ESC SOFTWARE", "SUPERVISORE", "MAGAZZINO SUPER", "UTENTE PROVA",
        "DISTORE01", "DISTORE02", "DISTORE03", "DISTORE04"}

COMPANY = globals().get("COMPANY") or env.ref("base.user_admin").company_id
env = env(context=dict(env.context, allowed_company_ids=[COMPANY.id], tracking_disable=True))


def skipped(person):
    if not person["active"]:
        return "suspended"
    if person["key"] in SKIP:
        return "technical account"
    if person["key"].startswith("CV -"):
        return "till login"
    return None


def display_name(key):
    return key.title()


# ESC deposit id -> POS of that shop
pos_by_deposit = {}
for data in env["ir.model.data"].search([("module", "=", XMLID_MODULE), ("model", "=", "stock.warehouse")]):
    warehouse = env["stock.warehouse"].browse(data.res_id)
    config = env["pos.config"].search([("picking_type_id.warehouse_id", "=", warehouse.id)], limit=1)
    if config:
        pos_by_deposit[int(data.name.split("_")[1])] = config

with gzip.open(DATA_FILE, "rt", encoding="utf-8") as f:
    people = json.load(f)

Employee = env["hr.employee"]
IMD = env["ir.model.data"]
used_badges = set(Employee.with_context(active_test=False).search([("barcode", "!=", False)]).mapped("barcode"))
created = updated = 0
skips, notes = {}, []
access = {config: {"basic": set(), "minimal": set()} for config in pos_by_deposit.values()}

for person in people:
    reason = skipped(person)
    if reason:
        skips.setdefault(reason, []).append(person["key"])
        continue
    xmlid = "employee_" + re.sub(r"[^a-z0-9]+", "_", person["key"].lower()).strip("_")
    data = IMD.search([("module", "=", XMLID_MODULE), ("name", "=", xmlid)], limit=1)
    employee = Employee.browse(data.res_id).exists()
    vals = {"name": display_name(person["key"]), "company_id": COMPANY.id,
            "is_pos_salesperson": person["salesperson"]}
    badge = person["badge"]
    if badge and badge != employee.barcode:
        if badge in used_badges:
            notes.append(f"{vals['name']}: badge {badge} already used, not set")
        else:
            vals["barcode"] = badge
            used_badges.add(badge)
    if employee:
        employee.write(vals)
        updated += 1
    else:
        employee = Employee.create(vals)
        IMD.create({"module": XMLID_MODULE, "name": xmlid, "model": "hr.employee",
                    "res_id": employee.id, "noupdate": True})
        created += 1
    if person["cashier"]:
        for deposit in person["cashier_shops"]:
            if deposit in pos_by_deposit:
                access[pos_by_deposit[deposit]]["basic"].add(employee.id)
    if person["salesperson"]:
        for deposit in person["salesperson_shops"]:
            if deposit in pos_by_deposit:
                access[pos_by_deposit[deposit]]["minimal"].add(employee.id)

for config, tiers in access.items():
    minimal = tiers["minimal"] - tiers["basic"]  # a cashier keeps the higher tier
    config.write({
        "basic_employee_ids": [(4, emp_id) for emp_id in tiers["basic"]],
        "minimal_employee_ids": [(4, emp_id) for emp_id in minimal],
    })
    print(f"{config.name}: {len(tiers['basic'])} cashiers, {len(minimal)} salespeople")

print(f"employees: {created} created, {updated} updated")
for reason, names in skips.items():
    print(f"skipped ({reason}): {len(names)} - {', '.join(names)}")
for note in notes:
    print("NOTE", note)
if DRY_RUN:
    env.cr.rollback()
    print("DRY RUN: rolled back")
else:
    env.cr.commit()
