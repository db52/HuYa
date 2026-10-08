"""Secret-free two-gift snapshots and fail-closed delivery fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import main
import report


def stock(ordinary=0, super_fans=0):
    return dict(ordinary=ordinary, super_fans=super_fans)


class TwoGiftTests(unittest.TestCase):
    def app(self, counts, rooms=(998,)):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app.mode, app.rooms = 'send', list(rooms)
        app.inventory, app.super_fans_inventory = None, None
        app.submitted, app.super_fans_submitted = 0, 0
        app.outcome, app.driver = 'NOT_STARTED', None
        app.login = MagicMock(return_value=True)
        app._ensure_browser_session = MagicMock()
        app.get_inventory = MagicMock(side_effect=counts)
        app.send_to_room = MagicMock(side_effect=lambda room, count, **kw: count)
        app._record_result = MagicMock()
        return app

    def test_super_only_triggers_send(self):
        app = self.app([stock(super_fans=10), stock()])
        self.assertTrue(app.run())
        app.send_to_room.assert_called_once_with(998, 10, gift_name='超粉虎粮')
        self.assertEqual((app.submitted, app.super_fans_submitted), (0, 10))
        self.assertEqual(app.get_inventory.call_count, 2)

    def test_both_types_sequential_distribution_and_independent_readbacks(self):
        app = self.app([stock(35, 10), stock(0, 10), stock()], rooms=(998, 123, 456))
        self.assertTrue(app.run())
        self.assertEqual(app.send_to_room.call_args_list, [
            call(998, 12, gift_name='虎粮'), call(123, 12, gift_name='虎粮'), call(456, 11, gift_name='虎粮'),
            call(998, 4, gift_name='超粉虎粮'), call(123, 3, gift_name='超粉虎粮'), call(456, 3, gift_name='超粉虎粮')])
        self.assertEqual(app.get_inventory.call_count, 3)
        self.assertEqual((app.submitted, app.super_fans_submitted), (35, 10))
        self.assertEqual(app.outcome, 'SENT_INVENTORY_VERIFIED')

    def test_small_stock_skips_zero_rooms(self):
        app = self.app([stock(1, 1), stock(0, 1), stock()], rooms=(998, 123))
        self.assertTrue(app.run())
        self.assertEqual(app.send_to_room.call_args_list, [call(998, 1, gift_name='虎粮'), call(998, 1, gift_name='超粉虎粮')])

    def test_both_zero_no_browser_or_send(self):
        app = self.app([stock()])
        self.assertTrue(app.run())
        app._ensure_browser_session.assert_not_called()
        app.send_to_room.assert_not_called()
        self.assertEqual(app.outcome, 'EMPTY_STOCK')

    def test_diagnostic_positive_both_never_sends(self):
        app = self.app([stock(35, 10)])
        app.mode = 'diagnose'
        self.assertTrue(app.run())
        app._ensure_browser_session.assert_not_called()
        app.send_to_room.assert_not_called()

    def test_ordinary_failure_prevents_super_and_remaining_rooms(self):
        app = self.app([stock(35, 10)], rooms=(998, 123))
        app.send_to_room.side_effect = main.HuyaError('SEND_FAILED_OR_UNKNOWN', 'uncertain')
        self.assertFalse(app.run())
        app.send_to_room.assert_called_once_with(998, 18, gift_name='虎粮')
        self.assertEqual(app.super_fans_submitted, 0)
        self.assertEqual(app.get_inventory.call_count, 1)

    def test_ordinary_postcondition_failure_prevents_super(self):
        for remaining in [stock(1, 10), stock(0, 9), stock(0, 11)]:
            with self.subTest(remaining=remaining):
                app = self.app([stock(35, 10), remaining])
                self.assertFalse(app.run())
                app.send_to_room.assert_called_once_with(998, 35, gift_name='虎粮')
                self.assertEqual(app.outcome, 'SEND_FAILED_OR_UNKNOWN')

    def test_super_unknown_stops_no_retry(self):
        app = self.app([stock(35, 10), stock(0, 10)], rooms=(998, 123))
        app.send_to_room.side_effect = [18, 17, main.HuyaError('SEND_FAILED_OR_UNKNOWN', 'uncertain')]
        self.assertFalse(app.run())
        self.assertEqual(app.send_to_room.call_args_list, [call(998, 18, gift_name='虎粮'), call(123, 17, gift_name='虎粮'), call(998, 5, gift_name='超粉虎粮')])
        self.assertEqual(app.super_fans_submitted, 0)
        self.assertEqual(app.get_inventory.call_count, 2)

    def test_super_remaining_not_zero_is_unknown(self):
        app = self.app([stock(super_fans=10), stock(super_fans=1)])
        self.assertFalse(app.run())
        app.send_to_room.assert_called_once()
        self.assertEqual(app.outcome, 'SEND_FAILED_OR_UNKNOWN')

    def test_incorrect_receipt_count_stops(self):
        for receipt in [True, 0, 34, None]:
            app = self.app([stock(35, 10)])
            app.send_to_room.side_effect = None
            app.send_to_room.return_value = receipt
            self.assertFalse(app.run())
            app.send_to_room.assert_called_once()

    def test_full_snapshot_schema_and_exact_names(self):
        parse = main.HuYaAuto._parse_inventory_response
        response = lambda items: {'status': 200, 'data': {'package': items}}
        self.assertEqual(parse(response([
            {'cName': '虎粮', 'num': '5'}, {'cName': '虎粮', 'num': 7}, {'cName': '超粉虎粮', 'num': '10'},
            {'cName': '虎粮礼包', 'num': 99}, {'cName': '超级超粉虎粮', 'num': 99}])), stock(12, 10))
        for name in ['虎粮', '超粉虎粮', '其他礼物']:
            for bad in [True, False, None, '', -1, 1.5, '1.5', '１２', '2e3', main.MAX_COUNT + 1]:
                with self.subTest(name=name, bad=bad), self.assertRaises(main.HuyaError):
                    parse(response([{'cName': name, 'num': bad}]))
        for name in main.GIFTS.values():
            with self.assertRaises(main.HuyaError):
                parse(response([{'cName': name, 'num': main.MAX_COUNT}, {'cName': name, 'num': 1}]))
        for bad in [stock(super_fans=True), {'ordinary': 0}, dict(stock(), unexpected=1), None]:
            with self.assertRaises(main.HuyaError): main.HuYaAuto._validate_inventory(bad)

    def test_bad_initial_snapshot_never_becomes_empty_or_sends(self):
        for bad in [stock(super_fans=True), {'ordinary': 0}, None]:
            app = self.app([bad])
            self.assertFalse(app.run())
            self.assertEqual(app.outcome, 'INVENTORY_RESPONSE_INVALID')
            app.send_to_room.assert_not_called()
            app._ensure_browser_session.assert_not_called()

    def test_login_snapshot_consumed_once_then_fresh_read(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app._get_inventory_api = MagicMock(side_effect=[stock(35, 10), stock(0, 10)])
        app._ensure_browser = MagicMock()
        self.assertTrue(app.login())
        self.assertEqual(app.get_inventory(), stock(35, 10))
        self.assertEqual(app._get_inventory_api.call_count, 1)
        self.assertEqual(app.get_inventory(), stock(0, 10))
        self.assertEqual(app._get_inventory_api.call_count, 2)
        app._ensure_browser.assert_not_called()

    def test_legacy_ordinary_projection_no_duplicate_requests(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app._get_inventory_api = MagicMock(return_value=stock(35, 10))
        self.assertEqual(app.get_hl_count(), 35)
        app._get_inventory_api.assert_called_once()

    def test_only_allowlisted_gifts_and_integer_counts_before_browser(self):
        app = self.app([stock()])
        for name, count in [('火箭', 1), ('虎粮礼包', 1), ('超粉虎粮', True), ('虎粮', -1)]:
            with self.assertRaises(main.HuyaError):
                main.HuYaAuto.send_to_room(app, 998, count, gift_name=name)
        app._ensure_browser_session.assert_not_called()

    def test_new_artifact_and_old_report_compatibility(self):
        with tempfile.TemporaryDirectory() as d:
            app = self.app([stock(35, 10), stock(0, 10), stock()])
            app.debug_dir = Path(d)
            self.assertTrue(app.run())
            main.HuYaAuto._record_result(app)
            data = report.safe_result(Path(d) / 'result.json')
            self.assertEqual(data, dict(mode='send', outcome='SENT_INVENTORY_VERIFIED', ordinary_huliang=35, submitted=35, super_fans_huliang=10, super_fans_submitted=10))
            path = Path(d) / 'old.json'
            path.write_text(json.dumps(dict(mode='send', outcome='EMPTY_STOCK', ordinary_huliang=0, submitted=0)))
            old = report.safe_result(path)
            self.assertIsNone(old['super_fans_huliang'])
            self.assertIsNone(old['super_fans_submitted'])
            summary = Path(d) / 'summary.md'
            with patch.dict(os.environ, GITHUB_STEP_SUMMARY=str(summary)):
                report.summarize(data, 'https://example.test/run')
            text = summary.read_text()
            for line in ['普通虎粮库存：35', '普通虎粮已确认送出：35', '超粉虎粮库存：10', '超粉虎粮已确认送出：10']:
                self.assertIn(line, text)
            path.write_text(json.dumps(dict(mode='send', outcome='EMPTY_STOCK', ordinary_huliang=0, submitted=0, super_fans_huliang=True, super_fans_submitted='SECRET', cookie='SECRET')))
            self.assertNotIn('SECRET', json.dumps(report.safe_result(path)))
            self.assertIsNone(report.safe_result(path)['super_fans_huliang'])

    def test_incident_notice_explicit_two_type_counts_only(self):
        data = dict(mode='send', outcome='SEND_FAILED_OR_UNKNOWN', ordinary_huliang=35,
                    submitted=35, super_fans_huliang=10, super_fans_submitted=0)
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'db52/HuYa', 'GITHUB_RUN_ID': '1'}), \
                patch('sys.argv', ['report.py']), patch.object(report, 'safe_result', return_value=data), \
                patch.object(report, 'previous_state', return_value=None), patch.object(report, 'Path'), \
                patch.object(report, 'summarize'), patch.object(report, 'send_notice') as send:
            self.assertEqual(report.main(), 0)
            message = send.call_args.args[0]
            for line in ['普通虎粮库存：35', '普通虎粮已确认送出：35', '超粉虎粮库存：10', '超粉虎粮已确认送出：0']:
                self.assertIn(line, message)
            self.assertIn('请勿直接重跑送礼', message)


if __name__ == '__main__':
    unittest.main()
