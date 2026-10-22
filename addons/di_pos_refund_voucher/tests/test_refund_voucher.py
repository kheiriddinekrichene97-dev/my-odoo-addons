from odoo.tests import tagged
from odoo.addons.point_of_sale.tests.common import TestPoSCommon


@tagged('post_install', '-at_install')
class TestDiRefundVoucher(TestPoSCommon):
    """Settling a refund with a voucher instead of money.

    The interface half - refusing Cassa and Carta on a return - is a
    patch on the payment screen and is not covered here. What is covered
    is the part that has to hold even when the till has been offline: the
    card gets issued, for the right amount, exactly once.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.basic_config
        cls.company_data['company'].di_voucher_validity_months = 24
        cls.product = cls.create_product('Cappotto', cls.categ_basic, 100.0)
        cls.customer = cls.env['res.partner'].create({'name': 'BONASERA ELVIRA'})

        cls.program = cls.env['loyalty.program'].search(
            [('program_type', '=', 'gift_card')], limit=1)
        if not cls.program:
            cls.program = cls.env['loyalty.program'].create({
                'name': 'Gift Cards',
                'program_type': 'gift_card',
            })

        # A method that issues a voucher, alongside the ordinary cash one.
        cls.voucher_method = cls.env['pos.payment.method'].create({
            'name': 'Rimborso con Buono',
            'company_id': cls.company_data['company'].id,
            'di_issues_gift_card': True,
        })
        cls.config.payment_method_ids = [(4, cls.voucher_method.id)]

    # ------------------------------------------------------------------
    def _refund_order(self, amount, method, partner=None):
        """A finalized order of -amount, settled through `method`."""
        self.open_new_session()
        order = self.env['pos.order'].create({
            'session_id': self.pos_session.id,
            'partner_id': (partner or self.customer).id,
            'company_id': self.company_data['company'].id,
            'lines': [(0, 0, {
                'product_id': self.product.id,
                'qty': -1,
                'price_unit': amount,
                'price_subtotal': -amount,
                'price_subtotal_incl': -amount,
            })],
            'amount_total': -amount,
            'amount_tax': 0.0,
            'amount_paid': -amount,
            'amount_return': 0.0,
        })
        self.env['pos.payment'].create({
            'pos_order_id': order.id,
            'payment_method_id': method.id,
            'amount': -amount,
        })
        return order

    # ------------------------------------------------------------------
    def test_voucher_is_issued_for_the_refunded_amount(self):
        order = self._refund_order(100.0, self.voucher_method)
        order._di_issue_refund_voucher()

        card = order.di_refund_voucher_id
        self.assertTrue(card, "a gift card should have been issued")
        self.assertEqual(card.points, 100.0)
        self.assertEqual(card.partner_id, self.customer)
        self.assertTrue(card.code, "the card must carry a number")
        self.assertTrue(card.expiration_date, "24 months was configured")

    def test_ordinary_payment_issues_nothing(self):
        cash = self.config.payment_method_ids.filtered(
            lambda m: not m.di_issues_gift_card)[:1]
        order = self._refund_order(100.0, cash)
        order._di_issue_refund_voucher()
        self.assertFalse(order.di_refund_voucher_id)

    def test_a_second_pass_does_not_issue_a_second_card(self):
        """_process_saved_order runs again on every later resync."""
        order = self._refund_order(100.0, self.voucher_method)
        order._di_issue_refund_voucher()
        first = order.di_refund_voucher_id
        order._di_issue_refund_voucher()
        self.assertEqual(order.di_refund_voucher_id, first)
        self.assertEqual(
            self.env['loyalty.card'].search_count([('id', '=', first.id)]), 1)

    def test_no_expiry_when_validity_is_zero(self):
        self.company_data['company'].di_voucher_validity_months = 0
        order = self._refund_order(50.0, self.voucher_method)
        order._di_issue_refund_voucher()
        self.assertFalse(order.di_refund_voucher_id.expiration_date)

    def test_anonymous_refund_gives_a_bearer_card(self):
        anonymous = self.env['res.partner'].create({'name': 'ANONIMO'})
        order = self._refund_order(30.0, self.voucher_method, partner=anonymous)
        order._di_issue_refund_voucher()
        self.assertEqual(order.di_refund_voucher_id.points, 30.0)

    def test_a_sale_paid_with_the_method_issues_nothing(self):
        """Only negative amounts issue a voucher - a positive one would be
        selling a gift card, which pos_loyalty handles through its own
        product."""
        self.open_new_session()
        order = self.env['pos.order'].create({
            'session_id': self.pos_session.id,
            'partner_id': self.customer.id,
            'company_id': self.company_data['company'].id,
            'lines': [(0, 0, {
                'product_id': self.product.id, 'qty': 1, 'price_unit': 100.0,
                'price_subtotal': 100.0, 'price_subtotal_incl': 100.0,
            })],
            'amount_total': 100.0, 'amount_tax': 0.0,
            'amount_paid': 100.0, 'amount_return': 0.0,
        })
        self.env['pos.payment'].create({
            'pos_order_id': order.id,
            'payment_method_id': self.voucher_method.id,
            'amount': 100.0,
        })
        order._di_issue_refund_voucher()
        self.assertFalse(order.di_refund_voucher_id)
