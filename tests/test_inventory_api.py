import unittest
from unittest.mock import MagicMock, patch
import main


class InventoryApiTests(unittest.TestCase):
    def test_authenticated_api_login_does_not_open_user_page(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app.mode = 'diagnose'
        app._get_hl_count_api = MagicMock(return_value=25)
        app._safe_get = MagicMock()
        app.driver = MagicMock()
        self.assertTrue(app.login())
        app._safe_get.assert_not_called()
        app.driver.execute_cdp_cmd.assert_not_called()

    def test_send_mode_defers_cookie_injection_until_browser_needed(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app.mode, app.cookie = 'send', 'test=fixture'
        app._get_hl_count_api = MagicMock(return_value=25)
        app.driver = MagicMock()
        app.driver.execute_cdp_cmd.return_value = {'success': True}
        self.assertTrue(app.login())
        app.driver.execute_cdp_cmd.assert_not_called()
        app._ensure_browser_session()
        self.assertEqual(app.driver.execute_cdp_cmd.call_args.args[0], 'Network.setCookie')
        app._ensure_browser_session()
        app.driver.execute_cdp_cmd.assert_called_once()

    def test_exact_count_and_empty(self):
        parse = main.HuYaAuto._parse_inventory_response
        def response(items): return {'status': 200, 'data': {'package': items}}
        self.assertEqual(parse(response([])), 0)
        self.assertEqual(parse(response([{'cName': '虎粮', 'num': '25'}, {'cName': '超粉虎粮', 'num': 90}])), 25)
        for bad in (None, '', True, False, -1, 1.5, '１２', '2e3'):
            with self.subTest(bad=bad), self.assertRaises(main.HuyaError):
                parse(response([{'cName': '虎粮', 'num': bad}]))
        for bad in ({}, {'status': 401}, response([None]), response([{}])):
            with self.subTest(bad=bad), self.assertRaises(main.HuyaError): parse(bad)

    def test_api_only_uses_readonly_endpoints(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app.cookie = 'test_cookie=fixture'
        first = MagicMock(status_code=200)
        first.json.return_value = {'status': 200, 'data': {'time': 123, 'sign': 'fixture-not-real'}}
        second = MagicMock(status_code=200)
        second.json.return_value = {'status': 200, 'data': {'package': [{'cName': '虎粮', 'num': 25}]}}
        with patch.object(main.requests, 'Session') as cls:
            session = cls.return_value.__enter__.return_value
            session.get.side_effect = [first, second]
            self.assertEqual(app._get_hl_count_api(), 25)
            calls = session.get.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual([c.kwargs['params']['do'] for c in calls], ['getTimeSign', 'listTotal'])
            for call in calls:
                self.assertEqual(call.args[0], 'https://q.huya.com/index.php')
                self.assertFalse(call.kwargs['allow_redirects'])
            session.post.assert_not_called()

    def test_api_success_skips_browser_fallback(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app._get_hl_count_api = MagicMock(return_value=0)
        app._get_hl_count_page = MagicMock()
        self.assertEqual(app.get_hl_count(), 0)
        app._get_hl_count_page.assert_not_called()

    def test_api_failure_falls_back_bounded(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app._get_hl_count_api = MagicMock(side_effect=main.requests.Timeout())
        app._get_hl_count_page = MagicMock(return_value=25)
        with patch.object(main.time, 'sleep'):
            self.assertEqual(app.get_hl_count(), 25)
        self.assertEqual(app._get_hl_count_api.call_count, 2)
        app._get_hl_count_page.assert_called_once()
