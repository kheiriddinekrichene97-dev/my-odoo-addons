"""Configure the di_* modules for the company (run after load_stock.py and load_pricelists.py).

Run inside the Odoo container with `odoo shell`. Safe to re-run.

- ING - CONTESSE gets production's warehouse code ICONT and becomes the
  arrival warehouse of di_stock_arrival.
- di_bonus_points: the company's settings use the Promo pricelist, and the
  default price brackets and Donna/Uomo category rules (installed for the
  main company) are copied to this company, the rules linked to their Reparto.
"""
COMPANY = globals().get("COMPANY") or env.ref("base.user_admin").company_id
env = env(context=dict(env.context, allowed_company_ids=[COMPANY.id], tracking_disable=True))
main_company = env.ref("base.main_company")

# Category rule -> Reparto codes (x_reparto, from ESC)
REPARTI = {"Donna": ["D", "D+", "DN", "DO"], "Uomo": ["U", "U+"]}

# Arrival warehouse
central = env.ref("esc_import.warehouse_1")
if central.code != "ICONT":
    central.code = "ICONT"
COMPANY.di_arrival_warehouse_id = central
print("arrival warehouse:", central.code, central.name)

# Bonus points settings
settings = env["bonus.point.settings"].get_settings(COMPANY)
settings.promo_pricelist_id = env["product.pricelist"].search(
    [("name", "=", "Promo"), ("company_id", "=", COMPANY.id)], limit=1)
print("bonus settings:", settings.point_value, "€/point, 1 point per", settings.earn_divisor, "€,",
      settings.expiry_days, "days, promo pricelist:", settings.promo_pricelist_id.name)

Bracket = env["bonus.point.bracket"].sudo()
if not Bracket.search_count([("company_id", "=", COMPANY.id)]):
    for bracket in Bracket.search([("company_id", "=", main_company.id)], order="sequence"):
        bracket.copy({"company_id": COMPANY.id})
print("brackets:", Bracket.search([("company_id", "=", COMPANY.id)], order="sequence").mapped(
    lambda b: f"{b.name} -> {b.points}"))

Rule = env["bonus.point.category.rule"].sudo()
for name, codes in REPARTI.items():
    rule = Rule.search([("company_id", "=", COMPANY.id), ("name", "=", name)], limit=1)
    if not rule:
        template = Rule.search([("company_id", "=", main_company.id), ("name", "=", name)], limit=1)
        rule = template.copy({"company_id": COMPANY.id, "name": name})
    rule.reparto_ids = env["x_reparto"].search([("x_name", "in", codes)])
    print(f"rule {rule.name}: max discount {rule.max_discount_rate}% for Reparto {rule.reparto_ids.mapped('x_name')}")

env.cr.commit()
