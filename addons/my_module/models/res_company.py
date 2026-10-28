import re

from odoo import api, models


class ResCompany(models.Model):
    _inherit = "res.company"

    @api.model
    def _my_get_brand_company(self):
        """The company whose brand (name, tagline, logo, colours) is shown
        before login: the default company of the administrator."""
        admin = self.env.ref("base.user_admin", raise_if_not_found=False)
        return (admin and admin.sudo().company_id) or self.env.company

    def _my_brand_tagline(self):
        """The company tagline (Settings > Companies) as plain text."""
        self.ensure_one()
        return re.sub(r"<[^>]+>", " ", self.report_header or "").strip()
