from odoo import api, fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    is_pos_salesperson = fields.Boolean(
        string='Available as Salesperson in POS',
        help="Shows this employee in the POS 'Salesperson' picker, so a "
             "cashier can credit them for helping a customer on an order "
             "they didn't personally ring up. Independent of POS login "
             "access (PIN/cashier lists) - an employee can be a cashier, "
             "a salesperson, or both.")

    @api.model
    def _load_pos_data_fields(self, config):
        fields_list = super()._load_pos_data_fields(config)
        return fields_list + ['is_pos_salesperson']
