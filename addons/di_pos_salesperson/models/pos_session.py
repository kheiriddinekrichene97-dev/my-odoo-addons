from odoo import api, models


class PosSession(models.Model):
    _inherit = 'pos.session'

    @api.model
    def _load_pos_data_models(self, config):
        """Load hr.employee into every POS session, regardless of whether
        "Log in with employees" (config.module_pos_hr) is turned on for
        this shop - the Salesperson picker is a separate concern from
        cashier login and shouldn't require rolling that feature out first.
        hr.employee's own _load_pos_data_domain (from pos_hr) already scopes
        this to the shop's configured employee lists when those are set.
        """
        models_list = super()._load_pos_data_models(config)
        if 'hr.employee' not in models_list:
            models_list = models_list + ['hr.employee']
        return models_list
