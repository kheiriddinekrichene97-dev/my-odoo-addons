from odoo.tests import tagged
from odoo.addons.point_of_sale.tests.common import TestPoSCommon


@tagged('post_install', '-at_install')
class TestDiPriceChange(TestPoSCommon):
    """The reason itself is asked for at the till and is not covered here.
    What is covered is what must hold for it to survive a sync."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.basic_config

    def test_the_reason_fields_reach_the_till(self):
        fields = self.env['pos.order.line']._load_pos_data_fields(self.config)
        for name in ('di_price_note', 'di_original_price_unit',
                     'di_original_discount', 'di_price_changed_by_id'):
            self.assertIn(name, fields)
        # appended, not replacing the core list - a replaced list breaks
        # every sync
        self.assertIn('product_id', fields)
        self.assertIn('price_type', fields)

    def test_price_changes_are_off_until_a_shop_turns_them_on(self):
        self.assertFalse(self.config.di_allow_price_change)

    def test_the_line_records_the_change(self):
        self.open_new_session()
        product = self.create_product('ABITO GRIFFAI', self.categ_basic, 100.0)
        employee = self.env['hr.employee'].create({'name': 'SITO ANGELA'})
        order = self.env['pos.order'].create({
            'session_id': self.pos_session.id,
            'company_id': self.company_data['company'].id,
            'lines': [(0, 0, {
                'product_id': product.id,
                'qty': 1,
                'price_unit': 45.0,
                'price_subtotal': 45.0,
                'price_subtotal_incl': 45.0,
                'price_type': 'manual',
                'di_price_note': 'Difetto sulla cucitura',
                'di_original_price_unit': 100.0,
                'di_price_changed_by_id': employee.id,
            })],
            'amount_total': 45.0, 'amount_tax': 0.0,
            'amount_paid': 0.0, 'amount_return': 0.0,
        })
        line = order.lines
        self.assertEqual(line.di_price_note, 'Difetto sulla cucitura')
        self.assertEqual(line.di_original_price_unit, 100.0)
        self.assertEqual(line.di_price_changed_by_id, employee)
        self.assertEqual(line.di_config_id, self.config,
                         "the shop must be stored for the Price Changes list")
