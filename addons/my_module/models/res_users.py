from odoo import _, api, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools.sql import escape_psql


class ResUsers(models.Model):
    _inherit = "res.users"

    @api.model
    def _get_login_domain(self, login):
        """Logins are e-mail addresses: match them regardless of case, as
        Kheiriddine@... and kheiriddine@... are the same address. Used to sign
        in, reset a password and sign up. `=ilike` is an exact match once its
        wildcards (% and _) are escaped, so a login cannot match other users."""
        if not login:
            return super()._get_login_domain(login)
        return Domain("login", "=ilike", escape_psql(login))

    @api.constrains("login")
    def _check_login_case_insensitive_unique(self):
        """With case-insensitive sign-in, two logins differing only by case
        would be ambiguous."""
        for user in self:
            if not user.login:
                continue
            duplicate = self.with_context(active_test=False).search_count([
                ("id", "!=", user.id),
                ("login", "=ilike", escape_psql(user.login)),
            ], limit=1)
            if duplicate:
                raise ValidationError(_(
                    "Another user already has the login %s (with a different case).",
                    user.login,
                ))
