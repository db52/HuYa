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
class SuperFansBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == '/room/998':
                    html = '<!doctype html><body data-lp="fixture-lp-998" data-gid="fixture-gid"></body>'
                elif path == '/inventory':
                    package = [{'cName': name, 'num': count} for name, count in cls.counts.items()]
                    if cls.bad_package is not None:
                        package = cls.bad_package
                    payload = json.dumps({'status': 200, 'data': {'package': package}})
                    html = '''<!doctype html><body><button id="packTab">包裹</button><script>
                    const hooks=[];window.jQuery={ajaxPrefilter:f=>hooks.push(f),_data:()=>({click:[{selector:'#nav li'}]})};
                    window.inventoryEvents=0;
                    document.getElementById('packTab').onclick=()=>{window.inventoryEvents++;
                        hooks.forEach(f=>f({url:'/index.php?m=PackageApi&do=listTotal'}, {}, {
                            done:cb=>setTimeout(()=>cb(PAYLOAD),20),fail:()=>{}
                        }));};</script>'''.replace('PAYLOAD', payload)
                elif path == '/gift':
                    items = ''.join('<div class="m-gift-item" style="display:inline-block;margin:20px;padding:15px"><i class="c-count">99</i><p>'+name+'</p></div>' for name in cls.names)
                    html = '''<!doctype html><body>ITEMS<script>
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
        cls.submits, cls.popup_name, cls.receipt, cls.bad_package = [], None, True, None
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
        urls = patch.dict(main.cfg.URLS, room_base=self.base+'/room/{}',
                          gift_tab=self.base+'/gift?lp={lp}&gid={gid}', pay_index=self.base+'/inventory')
        urls.start()
        self.addCleanup(urls.stop)

    def test_selected_super_name_real_hover_input_submit_and_stock(self):
        before = self.app._get_inventory_page()
        self.assertEqual(before, {'ordinary': 35, 'super_fans': 10})
        self.assertEqual(self.app.driver.execute_script('return window.inventoryEvents'), 1)
        self.assertEqual(self.app.send_to_room(998, 10, gift_name='超粉虎粮'), 10)
        self.assertEqual(type(self).submits, [{'name': '超粉虎粮', 'count': 10}])
        self.assertEqual(self.app.driver.execute_script('return [window.sendClicks,window.confirmClicks,window.__huyaGiftResult.status]'), [1, 1, 'success'])
        self.assertEqual(self.app._get_inventory_page(), {'ordinary': 35, 'super_fans': 0})
        self.assertEqual(self.app.driver.execute_script('return window.inventoryEvents'), 1)

    def test_both_types_run_real_local_business_receipts_and_readbacks(self):
        # Only login is bypassed: no account or production cookie exists. All
        # inventory observation, selection, hover, clicks and receipts are real
        # DOM and localhost HTTP operations, NOT evidence of a Huya live send.
        with patch.object(self.app, 'login', return_value=True), patch.object(self.app, 'get_inventory', side_effect=self.app._get_inventory_page):
            self.assertTrue(self.app.run())
        self.assertEqual(type(self).submits, [{'name': '虎粮', 'count': 35}, {'name': '超粉虎粮', 'count': 10}])
        data = json.loads((self.app.debug_dir / 'result.json').read_text())
        self.assertEqual(data['outcome'], 'SENT_INVENTORY_VERIFIED')
        self.assertEqual((data['submitted'], data['super_fans_submitted']), (35, 10))
        self.assertEqual(type(self).counts, {'虎粮': 0, '超粉虎粮': 0})

    def test_ambiguous_or_partial_names_never_click(self):
        for names in [['虎粮礼包', '超级超粉虎粮'], ['超粉虎粮', '超粉虎粮'], ['超粉虎粮 虎粮', '虎粮']]:
            type(self).names = names
            with self.subTest(names=names), self.assertRaises(main.HuyaError):
                self.app.send_to_room(998, 10, gift_name='超粉虎粮')
            self.assertEqual(type(self).submits, [])
            self.assertEqual(self.app.driver.execute_script('return window.sendClicks'), 0)

    def test_wrong_active_popup_prevents_submit(self):
        type(self).popup_name = '虎粮'
        with self.assertRaises(main.HuyaError):
            self.app.send_to_room(998, 10, gift_name='超粉虎粮')
        self.assertEqual(type(self).submits, [])
        self.assertEqual(self.app.driver.execute_script('return window.sendClicks'), 0)

    def test_unknown_super_receipt_never_retries(self):
        type(self).receipt = False
        real_wait = main.WebDriverWait
        with patch.object(main, 'WebDriverWait', side_effect=lambda driver, timeout, **kw: real_wait(driver, min(timeout, 1.5), **kw)):
            with self.assertRaises(main.HuyaError) as caught:
                self.app.send_to_room(998, 10, gift_name='超粉虎粮')
        self.assertEqual(caught.exception.code, 'SEND_FAILED_OR_UNKNOWN')
        self.assertEqual(type(self).submits, [{'name': '超粉虎粮', 'count': 10}])
        self.assertEqual(self.app.driver.execute_script('return [window.sendClicks,window.confirmClicks]'), [1, 1])

    def test_malformed_super_or_unrelated_count_invalidates_entire_event(self):
        for name in ['超粉虎粮', '其他礼物']:
            type(self).bad_package = [{'cName': '虎粮', 'num': 0}, {'cName': name, 'num': True}]
            with self.subTest(name=name), patch.object(main.time, 'sleep'), self.assertRaises(main.HuyaError):
                self.app._get_inventory_page()
            self.assertEqual(type(self).submits, [])


if __name__ == '__main__':
    unittest.main()
