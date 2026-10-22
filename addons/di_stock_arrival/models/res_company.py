from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    di_arrival_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string="Arrival Warehouse",
        check_company=True,
        help="Warehouse where incoming goods physically land before being divided. "
             "All arrival documents receive into this warehouse.",
    )

    def _di_get_arrival_warehouse(self):
        """Return the warehouse arrivals are received into.

        Falls back to the warehouse historically used for goods-in (code
        ``ICONT``) and then to any warehouse of the company, so the module is
        usable before anyone visits the settings page.
        """
        self.ensure_one()
        if self.di_arrival_warehouse_id:
            return self.di_arrival_warehouse_id
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.id), ('code', '=', 'ICONT')], limit=1)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].search(
                [('company_id', '=', self.id)], limit=1)
        return warehouse
