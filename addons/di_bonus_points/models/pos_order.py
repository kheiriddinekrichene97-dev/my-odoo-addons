from odoo import fields, models


class PosOrder(models.Model):
    _inherit = 'pos.order'

    bonus_points_credited = fields.Boolean(
        default=False, copy=False,
        help="Guards against crediting earned bonus points twice: "
             "_process_saved_order runs again on every later resync of an "
             "already-paid order (e.g. invoice generation), not just once.")
    bonus_points_refund_processed = fields.Boolean(
        default=False, copy=False,
        help="Guards against reversing bonus points twice for the same "
             "refund order, for the same reason as bonus_points_credited.")

    def _process_saved_order(self, draft):
        if not draft and self.state != 'cancel':
            Service = self.env['bonus.point.service']
            for line in self.lines:
                Service.reconcile_line_points(line, self.company_id)

        result = super()._process_saved_order(draft)

        if not draft and self.state != 'cancel':
            Service = self.env['bonus.point.service']
            if not self.bonus_points_credited:
                Service.credit_points(self)
                self.bonus_points_credited = True
            if not self.bonus_points_refund_processed:
                Service.process_refund(self)
                self.bonus_points_refund_processed = True

        return result

    def read_pos_data(self, data, config):
        """Piggy-back the order's partner(s) on the sync response so the POS
        frontend refreshes bonus_points_available in the same session (see
        di_bonus_points/static/src/app/screens/product_screen/control_buttons).

        Core's read_pos_data only returns pos.order/.line/.payment/etc, never
        res.partner, so credit_points/reconcile_line_points above update the
        DB but the frontend's already-loaded partner record is never told -
        it just shows a stale balance until the session is reloaded. The
        frontend's syncAllOrders merges ANY model key present here into its
        live store (data_service.js: missingRecursive/loadConnectedData
        aren't limited to "missing" records), so adding res.partner here is
        enough - no frontend change needed.
        """
        result = super().read_pos_data(data, config)
        partners = self.mapped('partner_id')
        if partners:
            result['res.partner'] = self.env['res.partner']._load_pos_data_read(
                partners, config)
        return result
