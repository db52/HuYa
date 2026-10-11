"""Actual bag parsing guards: offline, no production cookies or requests."""
import unittest
from unittest.mock import MagicMock

import main


class ActualBagTests(unittest.TestCase):
    def test_exact_names_and_validated_positive_other_gift_prove_absence(self):
        parse = main.HuYaAuto._parse_actual_bag_rows
        self.assertEqual(parse([{'name': '虎粮', 'count': '35'},
                                {'name': '超粉虎粮', 'count': '10'}]),
                         {'ordinary': 35, 'super_fans': 10})
        self.assertEqual(parse([{'name': '超粉虎粮', 'count': '10'}]),
                         {'ordinary': 0, 'super_fans': 10})
        self.assertEqual(parse([{'name': '虎粮礼包', 'count': '35'},
                                {'name': '超级超粉虎粮', 'count': '10'}]),
                         {'ordinary': 0, 'super_fans': 0})

    def test_empty_or_all_zero_is_unavailable_not_empty_stock(self):
        for rows in (None, [], {}, [{'name': '虎粮', 'count': '0'}],
                     [{'name': '虎粮', 'count': '0'}, {'name': '超粉虎粮', 'count': '0'}]):
            with self.subTest(rows=rows), self.assertRaises(main.HuyaError) as caught:
                main.HuYaAuto._parse_actual_bag_rows(rows)
            self.assertEqual(caught.exception.code, 'INVENTORY_PAGE_UNAVAILABLE')

    def test_every_row_requires_exact_integer_count(self):
        for name in ('虎粮', '超粉虎粮', '其他礼物'):
            for count in (None, True, 10, '', '-1', '1.5', '999+', '１２', '10个',
                          ' 10', '10 ', '1e3', str(main.MAX_COUNT + 1), '9' * 5000):
                with self.subTest(name=name, count=str(count)[:30]), self.assertRaises(main.HuyaError) as caught:
                    main.HuYaAuto._parse_actual_bag_rows([
                        {'name': '礼物', 'count': '1'}, {'name': name, 'count': count}])
                self.assertEqual(caught.exception.code, 'INVENTORY_RESPONSE_INVALID')

    def test_malformed_and_ambiguous_rows_rejected_without_exporting_values(self):
        for row in (None, {}, {'count': '10'}, {'name': '', 'count': '10'},
                    {'name': '虎粮 ', 'count': '10'}, {'name': 'SECRET', 'count': 'SECRET'}):
            with self.subTest(row=row), self.assertRaises(main.HuyaError) as caught:
                main.HuYaAuto._parse_actual_bag_rows([row])
            self.assertNotIn('SECRET', str(caught.exception))
        with self.assertRaises(main.HuyaError):
            main.HuYaAuto._parse_actual_bag_rows([
                {'name': '虎粮', 'count': '35'}, {'name': '虎粮', 'count': '35'}])

    def test_legacy_cached_snapshot_cannot_bypass_actual_bag(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app._authenticated_inventory = {'ordinary': 0, 'super_fans': 0}
        app._get_inventory_api = MagicMock()
        app._get_inventory_page = MagicMock(return_value={'ordinary': 35, 'super_fans': 10})
        self.assertEqual(app.get_inventory(), {'ordinary': 35, 'super_fans': 10})
        app._get_inventory_page.assert_called_once()
        app._get_inventory_api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
