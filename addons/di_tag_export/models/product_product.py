import base64

from odoo import _, models
from odoo.exceptions import UserError

# Attribute names used to pull the colour/size shown on the tag.
COLOUR_ATTRIBUTE = 'Colore'
SIZE_ATTRIBUTE = 'Taglia'

# Fallback "buono acquisto" code, printed when di_bonus_points isn't installed.
# This module only depends on `product` (see _get_ba_code), so a cartellino
# still prints on a database without the bonus points engine.
BA_DEFAULT = '0BA'

# Pricelist names. Overridable through system parameters so they can be renamed
# in Odoo without touching the code:
#   di_tag_export.dettaglio_pricelist / di_tag_export.ingrosso_pricelist
#   di_tag_export.promo_pricelist
DETTAGLIO_PRICELIST = 'Dettaglio'
INGROSSO_PRICELIST = 'Ingrosso'
PROMO_PRICELIST = 'Promo'


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # ------------------------------------------------------------------
    # Report data (called from the QWeb tag template)
    # ------------------------------------------------------------------
    def _get_tag_data(self):
        """Return the dict of ready-to-print strings for one label."""
        self.ensure_one()
        template = self.product_tmpl_id
        dettaglio, ingrosso, promo = self._tag_pricelists()

        # lst_price, not the template's list_price: it carries the variant's
        # own attribute price extra, which is the number di_bonus_points
        # judges the BA code at the bottom of this same tag against.
        list_price = self.lst_price or 0.0
        sale_price = self._tag_sale_price(dettaglio, promo)
        ingrosso_price = (
            ingrosso._get_product_price(self, 1.0) if ingrosso else 0.0)
        barcode = self.barcode or ''

        # A pricelist above the sales price would otherwise print as a
        # negative discount; there is simply nothing off in that case.
        discount = 0
        if list_price:
            discount = max(0, int(round(
                (list_price - sale_price) / list_price * 100)))

        return {
            'codice': self.default_code or '',
            'colore': self._tag_attribute(COLOUR_ATTRIBUTE),
            'descrizione': template.name or '',
            'misura': self._tag_attribute(SIZE_ATTRIBUTE),
            'prezzo': self._tag_price(list_price),
            'discount': discount,
            'sconto': '%d%%' % discount,
            'prezzo_scontato': self._tag_price(sale_price),
            'barcode': barcode,
            'barcode_image': self._tag_barcode_image(barcode),
            'marca': self._tag_m2o(template.x_studio_Prodotti2Marca),
            'stagione': self._tag_m2o(template.x_studio_prodotti2stagione),
            'reparto': self._tag_m2o(template.x_studio_prodotti2reparto),
            'posizionamento': self._tag_m2o(template.x_prodotti2posizionamento),
            'ingrosso': self._tag_ingrosso(ingrosso_price),
            'ba': self._get_ba_code(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _tag_pricelists(self):
        Pricelist = self.env['product.pricelist']
        Param = self.env['ir.config_parameter'].sudo()
        dettaglio_name = Param.get_param(
            'di_tag_export.dettaglio_pricelist', DETTAGLIO_PRICELIST)
        ingrosso_name = Param.get_param(
            'di_tag_export.ingrosso_pricelist', INGROSSO_PRICELIST)

        dettaglio = Pricelist.search([('name', '=', dettaglio_name)], limit=1)
        ingrosso = Pricelist.search([('name', '=', ingrosso_name)], limit=1)
        if not dettaglio:
            raise UserError(_(
                "Retail pricelist '%s' was not found. Set the correct name in "
                "the system parameter 'di_tag_export.dettaglio_pricelist'."
            ) % dettaglio_name)
        return dettaglio, ingrosso, self._tag_promo_pricelist()

    def _tag_promo_pricelist(self):
        """The pricelist the shops actually sell from.

        di_bonus_points already owns that choice - the BA code printed at the
        bottom of this same tag is judged against its Promo pricelist - so the
        tag follows that setting wherever the module is installed, and both
        halves of the label agree. Detected at runtime for the same reason
        ``_get_ba_code`` is: printing cartellini must not require the POS.

        Without it, a pricelist named 'Promo' (renameable through
        ``di_tag_export.promo_pricelist``), and an empty recordset if there is
        none - the caller then falls back to Dettaglio.
        """
        if 'bonus.point.settings' in self.env:
            settings = self.env['bonus.point.settings'].get_settings_readonly()
            if settings.promo_pricelist_id:
                return settings.promo_pricelist_id
        name = self.env['ir.config_parameter'].sudo().get_param(
            'di_tag_export.promo_pricelist', PROMO_PRICELIST)
        return self.env['product.pricelist'].search(
            [('name', '=', name)], limit=1)

    def _tag_sale_price(self, dettaglio, promo):
        """What the till will charge for this variant - the price on the tag.

        The shops sell from the Promo pricelist, so that is what a customer
        pays and what the tag has to print; Dettaglio holds the full price for
        most of the catalogue and printing it would put the wrong number on
        the shelf. An article with no promo rule prices at its sales price
        through either list and simply prints without a discount.

        Dettaglio stays the fallback for a database that has no Promo
        pricelist at all.
        """
        self.ensure_one()
        return (promo or dettaglio)._get_product_price(self, 1.0)

    def _tag_attribute(self, attribute_name):
        """Value of a variant attribute (e.g. Colore -> NAVY).

        Read from ``product_template_attribute_value_ids``, never from
        ``product_template_variant_value_ids``: the latter drops every
        attribute that has only ONE value on the template, so a single-colour
        article would print a blank colour on its tag."""
        self.ensure_one()
        for ptav in self.product_template_attribute_value_ids:
            if ptav.attribute_id.name == attribute_name:
                return ptav.name or ''
        return ''

    def _tag_barcode_image(self, value):
        """Code128 PNG embedded in the report, avoiding an HTTP fetch per tag."""
        if not value:
            return ''
        image = self.env['ir.actions.report'].barcode(
            'Code128', value, width=600, height=160, quiet=0)
        return base64.b64encode(image).decode('ascii')

    def _get_ba_code(self):
        """'Buono acquisto' code for this variant.

        Read straight off product.product.bonus_ba_code (di_bonus_points),
        which owns the rules - Promo pricelist, Reparto discount cap, price
        brackets - so a tag can never print a different BA than the till
        charges. Detected at runtime rather than declared in the manifest's
        `depends`: di_bonus_points pulls in point_of_sale, and printing
        cartellini shouldn't require POS on the database.
        """
        self.ensure_one()
        if 'bonus_ba_code' not in self._fields:
            return BA_DEFAULT
        return self.bonus_ba_code or BA_DEFAULT

    @staticmethod
    def _tag_m2o(record):
        """Display name of a many2one, or '' when empty."""
        return record.display_name or '' if record else ''

    @staticmethod
    def _tag_price(value):
        """9.9 -> '9,90' (Italian comma, two decimals)."""
        return ('%.2f' % (value or 0.0)).replace('.', ',')

    @staticmethod
    def _tag_ingrosso(value):
        """Wholesale price as an unformatted integer: 9.95 -> '995', 5.0 -> '500'."""
        return str(int(round((value or 0.0) * 100)))
