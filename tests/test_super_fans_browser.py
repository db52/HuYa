"""Real Chromium / localhost only: WebPackage item/hover/popup/send contract."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import main


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS') == '1', 'opt-in localhost browser fixtures')
class ActualBagBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == '/room/998':
                    html = '<!doctype html><body data-lp="998" data-gid="1"></body>'
                elif path == '/gift':
                    rows = cls.rows if cls.rows is not None else [(name, str(cls.counts.get(name, 99))) for name in cls.names if cls.counts.get(name, 99) > 0]
                    items = ''.join('<div class="m-gift-item" style="display:inline-block;margin:20px;padding:15px"><i class="c-count">'+count+'</i><p>'+name+'</p></div>' for name, count in rows)
                    if cls.behavior == 'empty':
                        items = ''
                    if cls.behavior == 'loading':
                        items = '<div aria-busy="true">loading</div>' + items
                    if cls.behavior == 'partial':
                        items += '<div class="m-gift-item"><p>未加载</p></div>'
                    if cls.behavior == 'failure':
                        items += '<div class="g-tips"><p>请求失败</p></div>'
                    if cls.behavior == 'changing':
                        # An early valid super-only sample must not be accepted
                        # before the later complete ordinary+super render.
                        items += '''<script>
                        const ordinary = Array.from(document.querySelectorAll('.m-gift-item')).find(x=>x.querySelector('p').textContent==='虎粮');
                        ordinary.remove();
                        setTimeout(()=>document.querySelector('.g-package-list').appendChild(ordinary), 600);
                        </script>'''
                    html = '''<!doctype html><body><div class="g-package-list">ITEMS</div><script>
                    const popupName=POPUPNAME;
                    window.sendClicks=0;window.confirmClicks=0;
                    document.querySelectorAll('.m-gift-item').forEach(item=>item.onmouseenter=()=>{
                        document.querySelectorAll('.g-present-content').forEach(p=>p.remove());
                        const selected=item.querySelector('p').textContent;
                        const popup=document.createElement('div');popup.className='g-present-content';
                        popup.style='position:absolute;top:160px;left:20px;padding:20px;background:#ddd';
                        popup.innerHTML='<div class="present-info"><p class="c-name"></p></div><input type="number" placeholder="自定义"><button class="c-send">赠送</button><button class="btn-success" style="display:none">立即送出</button>';
                        const name=popup.querySelector('.c-name');name.appendChild(document.createTextNode(popupName||selected));
                        name.insertAdjacentHTML('beforeend','<span>(0虎牙币)</span>');
                        document.body.appendChild(popup);
                        let customActive=false;
                        popup.querySelector('input').onclick=()=>{customActive=true;};
                        popup.querySelector('.c-send').onclick=()=>{window.sendClicks++;popup.querySelector('.btn-success').style.display='block';};
                        popup.querySelector('.btn-success').onclick=()=>{window.confirmClicks++;
                            fetch('/submit',{method:'POST',body:JSON.stringify({name:selected,count:customActive?Number(popup.querySelector('input').value):1})})
                                .then(r=>r.json()).then(data=>{if(!data.receipt)return;
                                    const toast=document.createElement('div');toast.className='g-tips';toast.innerHTML='<p>送礼成功</p>';document.body.appendChild(toast);});};
                    });</script>'''.replace('ITEMS', items).replace('POPUPNAME', json.dumps(cls.popup_name))
                else:
                    self.send_error(404)
                    return
                data = html.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.path != '/submit':
                    self.send_error(404)
                    return
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                cls.submits.append(data)
                cls.counts[data['name']] -= data['count']
                body = json.dumps({'receipt': cls.receipt}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        cls = type(self)
        cls.names = ['超粉虎粮', '虎粮', '虎粮礼包', '超级超粉虎粮']
        cls.counts = {'虎粮': 35, '超粉虎粮': 10}
        cls.submits, cls.popup_name, cls.receipt, cls.rows = [], None, True, None
        cls.behavior = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = main.HuYaAuto.__new__(main.HuYaAuto)
        self.app.mode, self.app.rooms = 'send', [998]
        self.app.inventory, self.app.super_fans_inventory = None, None
        self.app.submitted, self.app.super_fans_submitted, self.app.outcome = 0, 0, 'NOT_STARTED'
        self.app.debug_dir = Path(self.tmp.name)
        self.app.driver = self.app._init_browser()
        self.addCleanup(self.app.driver.quit)
        self.app.wait = main.WebDriverWait(self.app.driver, 1.5)
        self.app._browser_authenticated = True
        real_wait = main.WebDriverWait
        waits = patch.object(main, 'WebDriverWait', side_effect=lambda driver, timeout, **kw: real_wait(driver, min(timeout, 2.5), **kw))
        waits.start()
        self.addCleanup(waits.stop)
        urls = patch.dict(main.cfg.URLS, room_base=self.base+'/room/{}',
                          gift_tab=self.base+'/gift?lp={lp}&gid={gid}')
        urls.start()
        self.addCleanup(urls.stop)

    def test_ordinary_real_hover_submit_and_fresh_actual_bag(self):
        self.assertEqual(self.app.get_inventory(), {'ordinary': 35, 'super_fans': 10})
        self.assertEqual(self.app.send_to_room(998, 35, gift_name='虎粮'), 35)
        self.assertEqual(type(self).submits, [{'name': '虎粮', 'count': 35}])
        self.assertEqual(self.app.driver.execute_script('return [window.sendClicks,window.confirmClicks,window.__huyaGiftResult.status]'), [1, 1, 'success'])
        self.assertEqual(self.app.get_inventory(), {'ordinary': 0, 'super_fans': 10})

    def test_ordinary_only_run_local_receipt_and_actual_bag_postcondition(self):
        # Real localhost DOM/HTTP/browser operations, never a live Huya send.
        with patch.object(self.app, 'login', return_value=True):
            self.assertTrue(self.app.run())
        self.assertEqual(type(self).submits, [{'name': '虎粮', 'count': 35}])
        data = json.loads((self.app.debug_dir / 'result.json').read_text())
        self.assertEqual(data['outcome'], 'SENT_INVENTORY_VERIFIED')
        self.assertEqual((data['submitted'], data['super_fans_submitted']), (35, 0))
        self.assertEqual(type(self).counts, {'虎粮': 0, '超粉虎粮': 10})

    def test_super_even_explicit_call_forbidden_before_navigation(self):
        before = self.app.driver.current_url
        for count in (10, 0):
            with self.assertRaises(main.HuyaError):
                self.app.send_to_room(998, count, gift_name='超粉虎粮')
        self.assertEqual(self.app.driver.current_url, before)
        self.assertEqual(type(self).submits, [])

    def test_ordinary_absent_super10_proves_zero_and_no_send(self):
        type(self).counts['虎粮'] = 0
        self.assertEqual(self.app.get_inventory(), {'ordinary': 0, 'super_fans': 10})
        with patch.object(self.app, 'login', return_value=True):
            self.assertTrue(self.app.run())
        self.assertEqual(type(self).submits, [])
        data = json.loads((self.app.debug_dir / 'result.json').read_text())
        self.assertEqual(data['outcome'], 'EMPTY_STOCK')

    def test_diagnose_readonly_both_positive_never_gifts(self):
        self.app.mode = 'diagnose'
        with patch.object(self.app, 'login', return_value=True):
            self.assertTrue(self.app.run())
        self.assertEqual(type(self).submits, [])
        self.assertEqual(self.app.submitted, 0)

    def test_ambiguous_or_partial_names_never_click(self):
        for names in [['虎粮礼包', '超级超粉虎粮'], ['虎粮', '虎粮']]:
            type(self).names = names
            with self.subTest(names=names), self.assertRaises(main.HuyaError):
                self.app.send_to_room(998, 35, gift_name='虎粮')
            self.assertEqual(type(self).submits, [])
            self.assertEqual(self.app.driver.execute_script('return window.sendClicks'), 0)

    def test_wrong_active_popup_prevents_submit(self):
        type(self).popup_name = '超粉虎粮'
        with self.assertRaises(main.HuyaError):
            self.app.send_to_room(998, 35, gift_name='虎粮')
        self.assertEqual(type(self).submits, [])
        self.assertEqual(self.app.driver.execute_script('return window.sendClicks'), 0)

    def test_unknown_receipt_never_retries(self):
        type(self).receipt = False
        with self.assertRaises(main.HuyaError) as caught:
            self.app.send_to_room(998, 35, gift_name='虎粮')
        self.assertEqual(caught.exception.code, 'SEND_FAILED_OR_UNKNOWN')
        self.assertEqual(type(self).submits, [{'name': '虎粮', 'count': 35}])
        self.assertEqual(self.app.driver.execute_script('return [window.sendClicks,window.confirmClicks]'), [1, 1])

    def test_not_loaded_partial_and_empty_fail_closed(self):
        for behavior in ('empty', 'loading', 'partial', 'failure'):
            type(self).behavior = behavior
            with self.subTest(behavior=behavior), patch.object(main.time, 'sleep'), self.assertRaises(main.HuyaError) as caught:
                self.app.get_inventory()
            self.assertIn(caught.exception.code, ('INVENTORY_PAGE_UNAVAILABLE', 'INVENTORY_RESPONSE_INVALID'))
        self.assertEqual(type(self).submits, [])

    def test_malformed_ambiguous_and_truncated_counts_fail_closed(self):
        for rows in [[('虎粮', '999+'), ('超粉虎粮', '10')], [('虎粮', '35'), ('虎粮', '35')], [('虎粮', '35'), ('其他礼物', '-1')], [('虎粮礼包', '35'), ('超粉虎粮', '10')]]:
            type(self).rows = rows
            if rows[0][0] == '虎粮礼包':
                self.assertEqual(self.app.get_inventory(), {'ordinary': 0, 'super_fans': 10})
            else:
                with self.subTest(rows=rows), self.assertRaises(main.HuyaError):
                    self.app.get_inventory()
        self.assertEqual(type(self).submits, [])

    def test_changing_rows_wait_for_complete_stable_snapshot(self):
        type(self).behavior = 'changing'
        self.assertEqual(self.app.get_inventory(), {'ordinary': 35, 'super_fans': 10})
        self.assertEqual(type(self).submits, [])

    def test_network_failure_or_wrong_page_never_becomes_zero(self):
        with patch.dict(main.cfg.URLS, gift_tab=self.base+'/missing?lp={lp}&gid={gid}'), self.assertRaises(main.HuyaError) as caught:
            self.app.get_inventory()
        self.assertEqual(caught.exception.code, 'INVENTORY_PAGE_UNAVAILABLE')
        self.assertEqual(type(self).submits, [])


if __name__ == '__main__':
    unittest.main()
