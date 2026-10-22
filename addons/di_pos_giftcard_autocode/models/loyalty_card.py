from odoo import api, models


class LoyaltyCard(models.Model):
    _inherit = 'loyalty.card'

    @api.model
    def di_generate_gift_card_code(self):
        """Public wrapper around loyalty.card._generate_code().

        The POS "Sell/Manage physical gift card" popup needs a card number
        before it will save the amount and expiry (pos_loyalty's
        processGiftCard only records expiration_date/partner_id inside its
        `if (newGiftCardCode)` branch), so the popup pre-fills one - see
        ../static/src/app/components/popups/manage_giftcard_popup.

        Odoo's own generator is private, so it can't be called over RPC.
        Wrapping it keeps the generated numbers in exactly the same format
        as the ones Odoo creates itself, instead of inventing a second
        format in the browser.
        """
        return self._generate_code()
