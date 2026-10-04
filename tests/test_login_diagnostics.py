"""Offline fixtures: no account credentials and no real gift calls."""
import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

import main


class LoginDiagnosticTests(unittest.TestCase):
    def make_app(self):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app.cookie = 'fixture_cookie=DO_NOT_EXPORT'
        app.mode = 'diagnose'
        app.driver = MagicMock()
        app._safe_get = MagicMock(return_value=True)
        return app

    def response(self, http=200, payload=None):
        response = MagicMock(status_code=http)
        response.json.return_value = payload
        return response

    def query_error(self, response, expected_code, expected_detail):
        app = self.make_app()
        with patch.object(main.requests, 'Session') as cls:
            session = cls.return_value.__enter__.return_value
            session.get.return_value = response
            with self.assertRaises(main.HuyaError) as caught:
                app._get_hl_count_api()
            self.assertEqual(caught.exception.code, expected_code)
            self.assertEqual(str(caught.exception), expected_detail)
            session.get.assert_called_once()
            self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
            session.post.assert_not_called()
        self.assertNotIn('DO_NOT_EXPORT', str(caught.exception))
        return caught.exception

    def test_business_501_is_login_required_not_http_501(self):
        self.query_error(
            self.response(payload={'status': 501, 'data': [], 'msg': 'DO_NOT_EXPORT'}),
            'LOGIN_REQUIRED', 'getTimeSign: HTTP 200; business_status=501; login required')
        self.query_error(
            self.response(http=501), 'INVENTORY_HTTP_FAILED',
            'getTimeSign: HTTP 501')

    def test_http_failures_never_parse_or_log_response(self):
        for code in [302, 401, 403, 429, 503]:
            response = self.response(http=code)
            response.url = 'https://q.huya.com/?sign=DO_NOT_EXPORT'
            response.text = 'DO_NOT_EXPORT'
            with self.subTest(http=code):
                self.query_error(response, 'INVENTORY_HTTP_FAILED', f'getTimeSign: HTTP {code}')
            response.json.assert_not_called()

    def test_nonjson_and_invalid_business_codes(self):
        response = self.response()
        response.json.side_effect = ValueError('DO_NOT_EXPORT')
        self.query_error(response, 'INVENTORY_RESPONSE_INVALID', 'getTimeSign: HTTP 200; non-JSON response')
        for payload in [[], None, {'status': True}, {'status': '501'}, {'status': 'DO_NOT_EXPORT'}, {'status': 2**60}]:
            with self.subTest(payload=payload):
                self.query_error(self.response(payload=payload), 'INVENTORY_RESPONSE_INVALID',
                                 'getTimeSign: HTTP 200; invalid business status')

    def test_invalid_http_status_never_exported_or_coerced(self):
        for status in [True, None, '501', 'DO_NOT_EXPORT', 200.0, 99, 600, 2**60]:
            with self.subTest(status=status):
                response = self.response(http=status)
                self.query_error(response, 'INVENTORY_RESPONSE_INVALID',
                                 'getTimeSign: invalid HTTP status')
                response.json.assert_not_called()

    def test_list_total_http_business_and_schema_categories(self):
        cases = [
            (self.response(http=403), 'INVENTORY_HTTP_FAILED', 'listTotal: HTTP 403'),
            (self.response(payload={'status': 502}), 'INVENTORY_API_REJECTED',
             'listTotal: HTTP 200; business_status=502'),
            (self.response(payload={'status': '501'}), 'INVENTORY_RESPONSE_INVALID',
             'listTotal: HTTP 200; invalid business status'),
            (self.response(payload={'status': 200, 'data': {}}), 'INVENTORY_RESPONSE_INVALID',
             'listTotal: HTTP 200; inventory response invalid'),
        ]
        for response, code, detail in cases:
            with self.subTest(code=code), patch.object(main.requests, 'Session') as cls:
                first = self.response(payload={'status': 200, 'data': {'time': 123, 'sign': 'DO_NOT_EXPORT'}})
                session = cls.return_value.__enter__.return_value
                session.get.side_effect = [first, response]
                with self.assertRaises(main.HuyaError) as caught:
                    self.make_app()._get_hl_count_api()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(str(caught.exception), detail)
                self.assertEqual(session.get.call_count, 2)
                session.post.assert_not_called()

    def test_non_readonly_action_rejected_before_request(self):
        session = MagicMock()
        with self.assertRaises(main.HuyaError) as caught:
            main.HuYaAuto._package_api_get(session, 'DO_NOT_EXPORT')
        self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
        session.get.assert_not_called()
        self.assertNotIn('DO_NOT_EXPORT', str(caught.exception))

    def test_other_business_rejection_is_not_assumed_expired_cookie(self):
        self.query_error(self.response(payload={'status': 502, 'msg': 'DO_NOT_EXPORT'}),
                         'INVENTORY_API_REJECTED', 'getTimeSign: HTTP 200; business_status=502')

    def test_network_error_categories_do_not_export_urls(self):
        cases = [
            (main.requests.Timeout('DO_NOT_EXPORT'), 'INVENTORY_NETWORK_TIMEOUT'),
            (main.requests.exceptions.SSLError('DO_NOT_EXPORT'), 'INVENTORY_TLS_FAILED'),
            (main.requests.ConnectionError('DO_NOT_EXPORT'), 'INVENTORY_NETWORK_FAILED'),
        ]
        for error, code in cases:
            with self.subTest(code=code), patch.object(main.requests, 'Session') as cls:
                session = cls.return_value.__enter__.return_value
                session.get.side_effect = error
                with self.assertRaises(main.HuyaError) as caught:
                    self.make_app()._get_hl_count_api()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(str(caught.exception), 'getTimeSign: request failed')
                self.assertIsNone(caught.exception.__cause__)

    def test_sign_success_then_inventory_login_rejection(self):
        app = self.make_app()
        first = self.response(payload={'status': 200, 'data': {'time': 123, 'sign': 'DO_NOT_EXPORT'}})
        second = self.response(payload={'status': 501, 'data': [], 'msg': 'DO_NOT_EXPORT'})
        with patch.object(main.requests, 'Session') as cls:
            session = cls.return_value.__enter__.return_value
            session.get.side_effect = [first, second]
            with self.assertRaises(main.HuyaError) as caught:
                app._get_hl_count_api()
            self.assertEqual(caught.exception.code, 'LOGIN_REQUIRED')
            self.assertEqual(str(caught.exception), 'listTotal: HTTP 200; business_status=501; login required')
            self.assertEqual(session.get.call_count, 2)

    def test_confirmed_login_rejection_stops_without_retry_or_browser(self):
        app = self.make_app()
        app._get_hl_count_api = MagicMock(side_effect=main.HuyaError(
            'LOGIN_REQUIRED', 'getTimeSign: HTTP 200; business_status=501; login required'))
        with contextlib.redirect_stdout(io.StringIO()) as out, patch.object(main.time, 'sleep') as sleep:
            with self.assertRaises(main.HuyaError) as caught:
                app.login()
        self.assertEqual(caught.exception.code, 'LOGIN_REQUIRED')
        app._get_hl_count_api.assert_called_once()
        app._safe_get.assert_not_called()
        app.driver.execute_cdp_cmd.assert_not_called()
        sleep.assert_not_called()
        self.assertIn('LOGIN_REQUIRED', out.getvalue())
        self.assertIn('business_status=501', out.getvalue())
        self.assertNotIn('DO_NOT_EXPORT', out.getvalue())

    def test_expired_login_during_inventory_does_not_fallback(self):
        app = self.make_app()
        app._get_hl_count_api = MagicMock(side_effect=main.HuyaError('LOGIN_REQUIRED', 'listTotal: login required'))
        app._get_hl_count_page = MagicMock()
        with self.assertRaises(main.HuyaError):
            app.get_hl_count()
        app._get_hl_count_api.assert_called_once()
        app._get_hl_count_page.assert_not_called()

    def test_send_run_with_rejected_login_never_queries_or_sends(self):
        app = self.make_app()
        app.mode, app.rooms = 'send', [123]
        app.inventory, app.submitted, app.outcome = None, 0, 'NOT_STARTED'
        app._get_hl_count_api = MagicMock(side_effect=main.HuyaError('LOGIN_REQUIRED', 'login required'))
        app.get_hl_count = MagicMock()
        app.send_to_room = MagicMock()
        app._record_result = MagicMock()
        self.assertFalse(app.run())
        self.assertEqual(app.outcome, 'LOGIN_REQUIRED')
        self.assertIsNone(app.inventory)
        self.assertEqual(app.submitted, 0)
        app.get_hl_count.assert_not_called()
        app.send_to_room.assert_not_called()
        app.driver.quit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
