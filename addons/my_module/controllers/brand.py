from odoo import http
from odoo.http import request


class BrandController(http.Controller):

    @http.route("/my_module/brand/logo", type="http", auth="public", sitemap=False)
    def brand_logo(self, **kwargs):
        """Logo of the brand company, for pages shown before login (login page,
        favicon). Odoo's /web/binary/company_logo would show the main company's."""
        company = request.env["res.company"].sudo()._my_get_brand_company()
        if not company.logo or company.uses_default_logo:
            return request.redirect("/web/binary/company_logo")
        stream = request.env["ir.binary"]._get_image_stream_from(company, "logo")
        return stream.get_response(max_age=300)
