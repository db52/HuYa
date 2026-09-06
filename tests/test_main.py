import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import TimeoutException
from urllib3.exceptions import ReadTimeoutError
import main


class CoreTests(unittest.TestCase):
    def make_app(self, mode='diagnose', count=25):
        app = main.HuYaAuto.__new__(main.HuYaAuto)
        app.mode, app.rooms = mode, [123, 456]
        app.inventory, app.submitted, app.outcome = None, 0, 'NOT_STARTED'
        app.driver = MagicMock()
        app.login = MagicMock(return_value=True)
        app.get_hl_count = MagicMock(return_value=count)
        app.send_to_room = MagicMock(return_value=0)
        app._record_result = MagicMock()
        return app

    def test_diagnosis_never_sends(self):
        app = self.make_app()
        self.assertTrue(app.run())
        app.send_to_room.assert_not_called()
        self.assertEqual(app.outcome, 'DIAGNOSE_OK')
        app.driver.quit.assert_called_once()

    def test_zero_is_verified_success(self):
        app = self.make_app(mode='send', count=0)
        self.assertTrue(app.run())
        self.assertEqual(app.outcome, 'EMPTY_STOCK')
        app.send_to_room.assert_not_called()

    def test_query_error_is_not_zero(self):
        app = self.make_app()
        app.get_hl_count.side_effect = main.HuyaError('INVENTORY_QUERY_FAILED', 'NOT zero stock')
        self.assertFalse(app.run())
        self.assertIsNone(app.inventory)
        self.assertEqual(app.outcome, 'INVENTORY_QUERY_FAILED')
        app.send_to_room.assert_not_called()

    def test_login_failure_stops_inventory(self):
        app = self.make_app()
        app.login.side_effect = main.HuyaError('LOGIN_UNCONFIRMED', 'missing')
        self.assertFalse(app.run())
        app.get_hl_count.assert_not_called()

    def test_direct_send_guard_before_navigation(self):
        app = self.make_app()
        with self.assertRaises(main.HuyaError):
            main.HuYaAuto.send_to_room(app, 123, 1)
        app.driver.get.assert_not_called()

    def test_direct_submit_guard(self):
        with self.assertRaises(main.HuyaError):
            self.make_app()._submit_gift()

    def test_send_error_not_retried(self):
        app = self.make_app(mode='send')
        app.send_to_room.side_effect = main.HuyaError('SEND_FAILED_OR_UNKNOWN', 'timeout')
        self.assertFalse(app.run())
        app.send_to_room.assert_called_once()
        self.assertEqual(app.outcome, 'SEND_FAILED_OR_UNKNOWN')

    def test_send_requires_inventory_postcondition(self):
        app = self.make_app(mode='send')
        app.send_to_room.side_effect = [13, 12]
        app.get_hl_count.side_effect = [25, 25]
        self.assertFalse(app.run())
        self.assertEqual(app.outcome, 'SEND_FAILED_OR_UNKNOWN')

    def test_parse_rooms_no_fallback_or_duplicates(self):
        self.assertEqual(main.HuYaAuto._parse_rooms(''), [])
        self.assertEqual(main.HuYaAuto._parse_rooms('123, 123,456'), [123, 456])
        for invalid in ('abc', '-1', '0', '123; echo bad'):
            with self.subTest(invalid=invalid), self.assertRaises(main.HuyaError):
                main.HuYaAuto._parse_rooms(invalid)

    def test_cli_defaults_to_diagnosis(self):
        with patch('sys.argv', ['main.py']), patch.object(main, 'HuYaAuto') as cls:
            cls.return_value.run.return_value = True
            self.assertEqual(main.main(), 0)
            cls.assert_called_once_with(mode='diagnose')

    def test_navigation_timeout_requires_readiness(self):
        app = self.make_app()
        app._debug_capture = MagicMock()
        app.driver.current_url = 'https://hd.huya.com/pay/index.html?private=DO_NOT_LOG'
        app.driver.get.side_effect = TimeoutException('DO_NOT_LOG')
        with patch.object(main, 'WebDriverWait') as wait:
            wait.return_value.until.return_value = True
            self.assertTrue(app._safe_get('https://hd.huya.com/pay/index.html', 'inventory'))
        with patch.object(main, 'WebDriverWait') as wait, patch.object(main.time, 'sleep'):
            wait.return_value.until.side_effect = TimeoutException('DO_NOT_LOG')
            self.assertFalse(app._safe_get('https://hd.huya.com/pay/index.html', 'inventory'))
            self.assertEqual(wait.return_value.until.call_count, 2)

    def test_inventory_response_errors_never_default_to_zero(self):
        app = self.make_app()
        app._safe_get = MagicMock(return_value=False)
        app._debug_capture = MagicMock()
        with patch.object(main.time, 'sleep'), self.assertRaises(main.HuyaError) as exc:
            main.HuYaAuto.get_hl_count(app)
        self.assertEqual(exc.exception.code, 'INVENTORY_QUERY_FAILED')
        self.assertEqual(app._safe_get.call_count, 2)

    def test_transport_timeout_retries_readonly_navigation(self):
        app = self.make_app()
        app._debug_capture = MagicMock()
        app.driver.get.side_effect = ReadTimeoutError(None, '', 'private detail')
        with patch.object(main.time, 'sleep'):
            self.assertFalse(app._safe_get('https://i.huya.com/', 'login'))
        self.assertEqual(app.driver.get.call_count, 2)

    def test_cleanup_error_does_not_override_result(self):
        app = self.make_app()
        app.driver.quit.side_effect = ReadTimeoutError(None, '', 'private detail')
        self.assertTrue(app.run())
        self.assertEqual(app.outcome, 'DIAGNOSE_OK')

    def test_unknown_submission_stops_without_retry(self):
        app = self.make_app(mode='send')
        app.driver.execute_script.return_value = True
        app.wait = MagicMock()
        send_button = MagicMock()
        app.wait.until.side_effect = [send_button, ReadTimeoutError(None, '', 'private detail')]
        with self.assertRaises(ReadTimeoutError):
            app._submit_gift()
        send_button.click.assert_called_once()

    def test_old_document_and_wrong_query_not_ready(self):
        app = self.make_app()
        app._debug_capture = MagicMock()
        marker = {}
        def execute(script, *args):
            if script.startswith('window.__huyaNavigationMarker ='):
                marker['value'] = args[0]
            elif '__huyaNavigationMarker' in script:
                return marker.get('value')
            return True
        app.driver.execute_script.side_effect = execute
        class OnePoll:
            def __init__(self, driver, *args, **kwargs): self.driver = driver
            def until(self, predicate):
                if not predicate(self.driver): raise TimeoutException()
                return True
        with patch.object(main, 'WebDriverWait', OnePoll):
            app.driver.current_url = 'https://hd.huya.com/gift?lp=1&gid=2'
            self.assertFalse(app._safe_get(app.driver.current_url, 'gift', attempts=1))
            app.driver.get.side_effect = lambda _: marker.clear()
            self.assertFalse(app._safe_get('https://hd.huya.com/gift?lp=3&gid=2', 'gift', attempts=1))

    def test_artifact_no_private_html_screenshot_or_url(self):
        app = self.make_app()
        app.driver.current_url = 'https://i.huya.com/?secret=NEVER_EXPORT'
        app.driver.execute_script.return_value = {'ready_state': 'complete', 'body_present': True}
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out:
            app.debug_dir = Path(tmp)
            app._debug_capture('login_check')
            files = list(Path(tmp).iterdir())
            self.assertEqual(len(files), 1)
            self.assertNotIn('NEVER_EXPORT', files[0].read_text() + out.getvalue())
            self.assertEqual(files[0].suffix, '.json')
        app.driver.save_screenshot.assert_not_called()


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS') == '1', 'opt-in local browser fixtures')
class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        cls.response = {'status': 200, 'data': {'package': [{'cName': '虎粮', 'num': 25}]}}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                html = '''<!doctype html><body><button id="packTab">包裹</button><div id="myWrap"></div>
                <script>
                const hooks = [];
                window.jQuery = {ajaxPrefilter: f => hooks.push(f), _data: () => ({click: [{selector:'#nav li'}]})};
                document.getElementById('packTab').onclick = () => {
                    hooks.forEach(f => f({url:'https://q.huya.com/index.php?m=PackageApi&do=listTotal'}, {}, {
                        done: cb => setTimeout(() => cb(RESPONSE), 200), fail: () => {}
                    }));
                };
                </script>'''.replace('RESPONSE', json.dumps(cls.response))
                data = html.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/inventory'
        cls.app = main.HuYaAuto.__new__(main.HuYaAuto)
        cls.app.mode = 'diagnose'
        cls.app.driver = cls.app._init_browser()
        cls.app._debug_capture = MagicMock()

    @classmethod
    def tearDownClass(cls):
        cls.app.driver.quit()
        cls.server.shutdown()
        cls.server.server_close()

    def test_real_browser_inventory_observer_and_exact_gift(self):
        cases = [
            ([{'cName': '虎粮', 'num': 25}], 25),
            ([{'cName': '超粉虎粮', 'num': 99}, {'cName': '虎粮', 'num': 5}], 5),
            ([{'cName': '虎粮', 'num': 0}], 0),
            ([], 0),
        ]
        for package, expected in cases:
            with self.subTest(package=package), patch.dict(main.cfg.URLS, pay_index=self.url):
                type(self).response = {'status': 200, 'data': {'package': package}}
                self.assertEqual(self.app.get_hl_count(), expected)

    def test_real_browser_invalid_response_is_error(self):
        type(self).response = {'status': 401, 'data': {}}
        with patch.dict(main.cfg.URLS, pay_index=self.url), patch.object(main.time, 'sleep'):
            with self.assertRaises(main.HuyaError):
                self.app.get_hl_count()

    def test_real_browser_bad_numeric_stock_is_error(self):
        for value in (-1, None, '', True, 'not-a-count'):
            type(self).response = {'status': 200, 'data': {'package': [{'cName': '虎粮', 'num': value}]}}
            with self.subTest(value=value), patch.dict(main.cfg.URLS, pay_index=self.url), patch.object(main.time, 'sleep'):
                with self.assertRaises(main.HuyaError):
                    self.app.get_hl_count()

    def test_real_browser_malformed_package_is_not_empty(self):
        type(self).response = {'status': 200, 'data': {'package': [{'unexpected': 25}]}}
        with patch.dict(main.cfg.URLS, pay_index=self.url), patch.object(main.time, 'sleep'):
            with self.assertRaises(main.HuyaError):
                self.app.get_hl_count()

    def test_real_browser_unrelated_handler_not_ready(self):
        self.app._safe_get(self.url, 'fixture', ready=lambda d: d.execute_script('return Boolean(window.jQuery)'))
        self.app.driver.execute_script("window.jQuery._data = () => ({click:[{selector:'.unrelated'}]})")
        self.assertFalse(self.app.driver.execute_script(main.PACK_READY))

    def test_real_browser_toast_success_failure_and_stale(self):
        self.app._safe_get(self.url, 'fixture')
        for toast, expected in [('送礼成功', 'success'), ('礼物数量不足', 'rejected')]:
            self.app.driver.execute_script("document.querySelectorAll('.g-tips').forEach(x=>x.remove())")
            self.assertTrue(self.app.driver.execute_script(main.GIFT_RESULT_OBSERVER))
            self.app.driver.execute_script("const div=document.createElement('div');div.className='g-tips';const p=document.createElement('p');p.textContent=arguments[0];div.appendChild(p);document.body.appendChild(div)", toast)
            result = main.WebDriverWait(self.app.driver, 2).until(lambda d: d.execute_script("return window.__huyaGiftResult.status !== 'pending' && window.__huyaGiftResult.status"))
            self.assertEqual(result, expected)
            self.assertFalse(self.app.driver.execute_script(main.GIFT_RESULT_OBSERVER))

    def test_real_browser_submit_waits_for_business_success(self):
        self.app._safe_get(self.url, 'fixture')
        self.app.driver.execute_script("""
            document.body.innerHTML='<button class="c-send">赠送</button><button class="btn-success" style="display:none">立即送出</button>';
            window.sentClicks=0;window.confirmClicks=0;
            document.querySelector('.c-send').onclick=()=>{window.sentClicks++;document.querySelector('.btn-success').style.display='block'};
            document.querySelector('.btn-success').onclick=()=>{window.confirmClicks++;setTimeout(()=>{
                const div=document.createElement('div');div.className='g-tips';div.innerHTML='<p>送礼成功</p>';document.body.appendChild(div);
            },350)};
        """)
        self.app.mode = 'send'
        self.app.wait = main.WebDriverWait(self.app.driver, 2)
        try:
            self.app._submit_gift()
            self.assertEqual(self.app.driver.execute_script('return [window.sentClicks, window.confirmClicks, window.__huyaGiftResult.status]'), [1, 1, 'success'])
        finally:
            self.app.mode = 'diagnose'


if __name__ == '__main__':
    unittest.main()
