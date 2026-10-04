"""Offline lazy-browser and artifact fixtures; never access a Huya account."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import main


class LazyBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Point artifact paths at a scratch fixture, not the repository.
        self.path_patch = patch.object(main, '__file__', str(Path(self.tmp.name) / 'main.py'))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.env_patch = patch.dict(os.environ, {
            'HUYA_COOKIE': 'fixture=DO_NOT_EXPORT', 'HUYA_ROOMS': '123,456'}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.api_patch = patch.object(main.HuYaAuto, '_get_hl_count_api', return_value=0)
        self.api = self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.browser_patch = patch.object(main.HuYaAuto, '_init_browser')
        self.browser = self.browser_patch.start()
        self.addCleanup(self.browser_patch.stop)

    def artifact(self):
        return json.loads((Path(self.tmp.name) / 'debug_artifacts/result.json').read_text())

    def test_constructor_does_not_start_browser(self):
        app = main.HuYaAuto()
        self.assertIsNone(app.driver)
        self.browser.assert_not_called()
        self.api.assert_not_called()

    def test_diagnose_and_zero_send_work_without_browser(self):
        for mode, outcome in [('diagnose', 'DIAGNOSE_OK'), ('send', 'EMPTY_STOCK')]:
            with self.subTest(mode=mode), patch.object(main.HuYaAuto, 'send_to_room') as send:
                app = main.HuYaAuto(mode)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertTrue(app.run())
                self.assertEqual(self.artifact(), {
                    'mode': mode, 'outcome': outcome, 'ordinary_huliang': 0, 'submitted': 0})
                self.assertNotIn('cleanup failed', out.getvalue())
                self.assertIsNone(app.driver)
                send.assert_not_called()
        self.browser.assert_not_called()

    def test_login_rejection_has_artifact_no_browser_retry_send(self):
        self.api.side_effect = main.HuyaError('LOGIN_REQUIRED', 'getTimeSign: HTTP 200; business_status=501; login required')
        app = main.HuYaAuto('send')
        with patch.object(main.time, 'sleep') as sleep, patch.object(app, 'send_to_room') as send:
            self.assertFalse(app.run())
            sleep.assert_not_called()
            send.assert_not_called()
        self.api.assert_called_once()
        self.browser.assert_not_called()
        self.assertEqual(self.artifact()['outcome'], 'LOGIN_REQUIRED')
        self.assertIsNone(self.artifact()['ordinary_huliang'])

    def test_nonzero_send_starts_once_and_distribution_unchanged(self):
        self.api.side_effect = [25, 25, 0]
        driver = self.browser.return_value
        driver.execute_cdp_cmd.return_value = {'success': True}
        app = main.HuYaAuto('send')
        with patch.object(app, 'send_to_room', side_effect=[13, 12]) as send:
            self.assertTrue(app.run())
            self.assertEqual(send.call_args_list, [call(123, 13), call(456, 12)])
        self.browser.assert_called_once()
        driver.execute_cdp_cmd.assert_called_once()
        driver.quit.assert_called_once()
        self.assertEqual(self.artifact()['outcome'], 'SENT_INVENTORY_VERIFIED')

    def test_browser_start_failure_records_artifact_without_send(self):
        self.api.return_value = 25
        self.browser.side_effect = RuntimeError('DO_NOT_EXPORT')
        app = main.HuYaAuto('send')
        with contextlib.redirect_stdout(io.StringIO()) as out, patch.object(app, 'send_to_room') as send:
            self.assertFalse(app.run())
            send.assert_not_called()
        self.assertEqual(self.artifact()['outcome'], 'BROWSER_START_FAILED')
        self.assertEqual(self.artifact()['ordinary_huliang'], 25)
        self.assertNotIn('DO_NOT_EXPORT', out.getvalue())

    def test_partial_browser_start_cleanup(self):
        app = main.HuYaAuto('send')
        driver = MagicMock()
        def partial_start():
            app.driver = driver
            raise RuntimeError('DO_NOT_EXPORT')
        self.browser.side_effect = partial_start
        self.api.return_value = 25
        self.assertFalse(app.run())
        driver.quit.assert_called_once()
        self.assertEqual(self.artifact()['outcome'], 'BROWSER_START_FAILED')

    def test_config_errors_record_allowlisted_result(self):
        cases = [{ 'HUYA_COOKIE': ''}, {'HUYA_ROOMS': 'DO_NOT_EXPORT'}, {'HUYA_ROOMS': ''}]
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env):
                with self.assertRaises(main.HuyaError) as caught:
                    main.HuYaAuto('send')
                self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
                result = self.artifact()
                self.assertEqual(result['outcome'], 'CONFIG_ERROR')
                self.assertIsNone(result['ordinary_huliang'])
                self.assertEqual(set(result), {'mode', 'outcome', 'ordinary_huliang', 'submitted'})
                self.assertNotIn('DO_NOT_EXPORT', json.dumps(result))
        self.browser.assert_not_called()
        self.api.assert_not_called()

    def test_readonly_failure_bounded_before_lazy_browser_fallback(self):
        self.api.side_effect = main.HuyaError('INVENTORY_NETWORK_TIMEOUT', 'getTimeSign: request failed')
        app = main.HuYaAuto()
        app._safe_get = MagicMock(return_value=True)
        with patch.object(main.time, 'sleep') as sleep:
            self.assertTrue(app.login())
        self.assertEqual(self.api.call_count, 2)
        self.browser.assert_called_once()
        self.assertEqual(app._safe_get.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertTrue(app._browser_authenticated)

    def test_page_login_rejection_not_retried_or_confused_with_empty(self):
        app = main.HuYaAuto()
        app.driver = MagicMock()
        app._browser_authenticated = True
        app._safe_get = MagicMock(return_value=True)
        app._debug_capture = MagicMock()
        with patch.object(main, 'WebDriverWait') as wait, patch.object(main.time, 'sleep') as sleep:
            wait.return_value.until.return_value = {
                'loaded': True, 'valid': False, 'business_status': 501, 'count': None}
            with self.assertRaises(main.HuyaError) as caught:
                app._get_hl_count_page()
            self.assertEqual(caught.exception.code, 'LOGIN_REQUIRED')
            app._safe_get.assert_called_once()
            sleep.assert_not_called()

    def test_page_network_schema_and_business_failures_stay_distinct(self):
        cases = [
            ({'business_status': 502}, 'INVENTORY_API_REJECTED'),
            ({'network_failed': True, 'http_status': 503}, 'INVENTORY_HTTP_FAILED'),
            ({'network_failed': True, 'http_status': 'DO_NOT_EXPORT'}, 'INVENTORY_NETWORK_FAILED'),
            ({'business_status': '501'}, 'INVENTORY_RESPONSE_INVALID'),
            ({'business_status': 200, 'valid': True, 'count': True}, 'INVENTORY_RESPONSE_INVALID'),
        ]
        for result, code in cases:
            with self.subTest(code=code):
                app = main.HuYaAuto()
                app.driver = MagicMock()
                app._browser_authenticated = True
                app._safe_get = MagicMock(return_value=True)
                app._debug_capture = MagicMock()
                with patch.object(main, 'WebDriverWait') as wait, patch.object(main.time, 'sleep'):
                    wait.return_value.until.return_value = dict(loaded=True, **result)
                    with self.assertRaises(main.HuyaError) as caught:
                        app._get_hl_count_page()
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn('DO_NOT_EXPORT', str(caught.exception))
                    self.assertEqual(app._safe_get.call_count, 2)


if __name__ == '__main__':
    unittest.main()
