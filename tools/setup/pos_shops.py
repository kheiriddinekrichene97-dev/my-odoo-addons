"""Create one Point of Sale per shop warehouse ("PV - ...") of the company.

Run inside the Odoo container with `odoo shell` (see tools/esc_import/load_products.py
for the command). Safe to re-run: existing shops, payment methods, journals and
the gift card programme are reused.

Each shop gets:
- its own "Cassa" (cash) payment method: Odoo forbids sharing a cash method;
- the shared "Carta" (card) and "Rimborso con Buono" (refund as gift card,
  di_pos_refund_voucher) payment methods;
- "Log in with Employees" (pos_hr) with the admin as Administrator (di_pos_roles);
- price changes allowed with a reason (di_pos_price_change).
"""
COMPANY = globals().get("COMPANY") or env.ref("base.user_admin").company_id
env = env(context=dict(env.context, allowed_company_ids=[COMPANY.id], tracking_disable=True))
admin = env.ref("base.user_admin")


def get_or_create(model, domain, vals):
    record = env[model].with_context(active_test=False).search(domain, limit=1)
    return record or env[model].create(vals)


def company_domain(*extra):
    return [("company_id", "=", COMPANY.id), *extra]


# Administrator employee for the admin user
employee = get_or_create("hr.employee", company_domain(("user_id", "=", admin.id)), {
    "name": admin.name, "user_id": admin.id, "company_id": COMPANY.id})

# Shared card payment method, like point_of_sale does for a new company
bank_journal = env["account.journal"].search(company_domain(("type", "=", "bank")), limit=1)
outstanding = (env["account.chart.template"].ref("account_journal_payment_debit_account_id", raise_if_not_found=False)
               or COMPANY.transfer_account_id)
card = get_or_create("pos.payment.method", company_domain(("name", "=", "Carta")), {
    "name": "Carta", "journal_id": bank_journal.id, "outstanding_account_id": outstanding.id,
    "company_id": COMPANY.id, "sequence": 2})

# Refund paid with a gift card: its own journal collects the outstanding vouchers
voucher_journal = get_or_create("account.journal", company_domain(("code", "=", "RBUO")), {
    "name": "Rimborso con Buono", "code": "RBUO", "type": "bank", "company_id": COMPANY.id})
voucher = get_or_create("pos.payment.method", company_domain(("name", "=", "Rimborso con Buono")), {
    "name": "Rimborso con Buono", "journal_id": voucher_journal.id, "outstanding_account_id": outstanding.id,
    "di_issues_gift_card": True, "company_id": COMPANY.id, "sequence": 3})

# Sales on account (pay later) for identified customers: their debt goes on the
# customer's receivable at closing (di_pos_customer_account settles it).
customer_account = get_or_create("pos.payment.method", company_domain(("name", "=", "Conto Cliente")), {
    "name": "Conto Cliente", "split_transactions": True, "company_id": COMPANY.id, "sequence": 4})

# Gift card programme of the company. di_pos_refund_voucher takes the first active
# gift card programme of ANY company, so other companies' ones are archived.
program = env["loyalty.program"].search(company_domain(("program_type", "=", "gift_card")), limit=1)
if not program:
    env["loyalty.program"].create_from_template("gift_card")
    program = env["loyalty.program"].search(company_domain(("program_type", "=", "gift_card")), limit=1)
program.name = "Carta Regalo"
# pos_loyalty refuses to open a till whose gift card programme has no print report
if not program.pos_report_print_id:
    program.pos_report_print_id = env["ir.actions.report"].search(
        [("model", "=", "loyalty.card"), ("report_name", "=", "loyalty.gift_card_report_i18n")], limit=1)
others = env["loyalty.program"].sudo().search([("program_type", "=", "gift_card"), ("company_id", "!=", COMPANY.id)])
others.active = False
print("gift card programme:", program.name, "| archived from other companies:", others.mapped("name"))

for warehouse in env["stock.warehouse"].search(company_domain(("name", "=like", "PV - %")), order="name"):
    config = env["pos.config"].search(company_domain(("name", "=", warehouse.name)), limit=1)
    if config:
        if customer_account not in config.payment_method_ids:
            config.payment_method_ids = [(4, customer_account.id)]
        print("exists:", config.name, "| methods:", ", ".join(config.payment_method_ids.mapped("name")))
        continue
    place = warehouse.name.removeprefix("PV - ")
    cash = env["pos.config"]._create_cash_payment_method({
        "name": f"Cassa {place}", "code": f"C{warehouse.code[2:]}"[:5]})
    cash.write({"name": "Cassa", "sequence": 1})
    config = env["pos.config"].create({
        "name": warehouse.name,
        "company_id": COMPANY.id,
        "picking_type_id": warehouse.pos_type_id.id,
        "payment_method_ids": [(6, 0, (cash | card | voucher | customer_account).ids)],
        "module_pos_hr": True,
        "advanced_employee_ids": [(6, 0, employee.ids)],
        "di_allow_price_change": True,
    })
    print("created:", config.name, "| stock from", config.picking_type_id.default_location_src_id.complete_name)

# Products for sale at the tills
templates = env["product.template"].search(company_domain(("sale_ok", "=", True), ("available_in_pos", "=", False)))
for start in range(0, len(templates), 5000):
    templates[start:start + 5000].write({"available_in_pos": True})
print(f"{len(templates)} products made available in the POS")
env.cr.commit()
