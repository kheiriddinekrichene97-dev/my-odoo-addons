from odoo.tests import tagged
from odoo.addons.point_of_sale.tests.common import TestPoSCommon


@tagged('post_install', '-at_install')
class TestDiSalespersonLines(TestPoSCommon):
    """Attribution per line, and the order-level summary built from it.

    The sticky behaviour at the till is a patch on the product screen and
    is not covered here. What is covered is what has to survive an offline
    till syncing later: which employee each line ends up crediting.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.basic_config
        cls.dress = cls.create_product('ABITO CORTO', cls.categ_basic, 26.97)
        cls.scarf = cls.create_product('SCIARPA FANTASIA', cls.categ_basic, 9.95)
        cls.jessica = cls.env['hr.employee'].create({
            'name': 'BEVACQUA JESSICA', 'is_pos_salesperson': True})
        cls.davide = cls.env['hr.employee'].create({
            'name': 'CAMBRIA DAVIDE', 'is_pos_salesperson': True})

    # ------------------------------------------------------------------
    def _order(self, lines):
        """lines: [(product, price, salesperson or None)]"""
        self.open_new_session()
        order = self.env['pos.order'].create({
            'session_id': self.pos_session.id,
            'company_id': self.company_data['company'].id,
            'lines': [(0, 0, {
                'product_id': product.id,
                'qty': 1,
                'price_unit': price,
                'price_subtotal': price,
                'price_subtotal_incl': price,
                'salesperson_id': who.id if who else False,
            }) for product, price, who in lines],
            'amount_total': sum(p for _pr, p, _w in lines),
            'amount_tax': 0.0,
            'amount_paid': 0.0,
            'amount_return': 0.0,
        })
        order._di_apply_salesperson_summary()
        return order

    # ------------------------------------------------------------------
    def test_one_receipt_can_carry_two_salespeople(self):
        order = self._order([
            (self.dress, 26.97, self.jessica),
            (self.scarf, 9.95, self.davide),
        ])
        by_person = {line.product_id: line.salesperson_id for line in order.lines}
        self.assertEqual(by_person[self.dress], self.jessica)
        self.assertEqual(by_person[self.scarf], self.davide)

    def test_summary_names_a_single_seller(self):
        order = self._order([
            (self.dress, 26.97, self.jessica),
            (self.scarf, 9.95, self.jessica),
        ])
        self.assertEqual(order.salesperson_id, self.jessica)
        self.assertEqual(order.di_salesperson_label, 'BEVACQUA JESSICA')

    def test_summary_says_mixed_when_shared(self):
        order = self._order([
            (self.dress, 26.97, self.jessica),
            (self.scarf, 9.95, self.davide),
        ])
        self.assertFalse(order.salesperson_id,
                         "no single employee sold this order")
        self.assertEqual(order.di_salesperson_label, 'Mixed')

    def test_untagged_lines_do_not_block_anything(self):
        order = self._order([(self.dress, 26.97, None)])
        self.assertFalse(order.lines.salesperson_id)
        self.assertFalse(order.di_salesperson_label)

    def test_an_untagged_line_does_not_make_the_order_mixed(self):
        """One assistant sold the only tagged item - credit them."""
        order = self._order([
            (self.dress, 26.97, self.jessica),
            (self.scarf, 9.95, None),
        ])
        self.assertEqual(order.salesperson_id, self.jessica)

    def test_a_return_credits_back_the_original_seller(self):
        sale = self._order([(self.dress, 26.97, self.jessica)])
        sold = sale.lines[0]

        refund = self.env['pos.order.line'].create({
            'order_id': sale.id,
            'product_id': self.dress.id,
            'qty': -1,
            'price_unit': 26.97,
            'price_subtotal': -26.97,
            'price_subtotal_incl': -26.97,
            'refunded_orderline_id': sold.id,
        })
        self.assertEqual(
            refund.salesperson_id, self.jessica,
            "the return must come off whoever made the sale")

    def test_a_return_can_still_be_attributed_elsewhere(self):
        """An explicit value always wins over the inherited one."""
        sale = self._order([(self.dress, 26.97, self.jessica)])
        refund = self.env['pos.order.line'].create({
            'order_id': sale.id,
            'product_id': self.dress.id,
            'qty': -1,
            'price_unit': 26.97,
            'price_subtotal': -26.97,
            'price_subtotal_incl': -26.97,
            'refunded_orderline_id': sale.lines[0].id,
            'salesperson_id': self.davide.id,
        })
        self.assertEqual(refund.salesperson_id, self.davide)

    def test_the_line_field_reaches_the_till(self):
        fields = self.env['pos.order.line']._load_pos_data_fields(self.config)
        self.assertIn('salesperson_id', fields)
        # the core list must survive - a replaced list breaks every sync
        self.assertIn('product_id', fields)
        self.assertIn('refunded_orderline_id', fields)
