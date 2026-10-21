from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    nickname = fields.Char(string="Nickname")
