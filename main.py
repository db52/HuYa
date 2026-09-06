#!/usr/bin/env python3
"""Huya inventory diagnostics and explicitly enabled gift delivery."""
import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
import requests
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from urllib3.exceptions import HTTPError as TransportError

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

import config as cfg

DRIVER_ERRORS = (WebDriverException, TransportError, TimeoutError, ConnectionError)

PACK_READY = r"""
const jq = window.jQuery;
return Boolean(jq && jq._data && ((jq._data(document, 'events') || {}).click || [])
    .some(handler => handler.selector === '#nav li'));
"""

# The public gift app shows this toast only after iPayRespCode === 0.
# Observe a NEW toast in this document; never reuse a prior room's success.
GIFT_RESULT_OBSERVER = r"""
if (document.querySelector('.g-tips')) return false;
if (window.__huyaGiftObserver) window.__huyaGiftObserver.disconnect();
window.__huyaGiftResult = {status: 'pending'};
window.__huyaGiftObserver = new MutationObserver(function() {
    if (window.__huyaGiftResult.status !== 'pending') return;
    const toast = document.querySelector('.g-tips p');
    if (!toast || !toast.textContent.trim()) return;
    window.__huyaGiftResult = {
        status: toast.textContent.trim() === '送礼成功' ? 'success' : 'rejected'
    };
});
window.__huyaGiftObserver.observe(document.body, {childList: true, subtree: true, characterData: true});
return true;
"""


class HuyaError(RuntimeError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


# Observe only the inventory response already requested by the page. Never
# export response bodies, credentials, signed URLs or account information.
INVENTORY_OBSERVER = r"""
if (!window.jQuery || !window.jQuery.ajaxPrefilter) return false;
if (window.__huyaInventoryObserver) return true;
window.__huyaInventoryObserver = true;
window.__huyaInventory = {loaded: false};
window.jQuery.ajaxPrefilter(function(options, original, xhr) {
    let url;
    try { url = new URL(options.url, location.href); } catch (_) { return; }
    if (url.searchParams.get('m') !== 'PackageApi' ||
        url.searchParams.get('do') !== 'listTotal') return;
    xhr.done(function(response) {
        const result = {loaded: true, valid: false, count: null};
        if (response && Number(response.status) === 200 && response.data &&
            Array.isArray(response.data.package) &&
            response.data.package.every(x => x && typeof x.cName === 'string' && x.cName.trim())) {
            const items = response.data.package.filter(x => x.cName === '虎粮');
            const counts = items.map(x =>
                (typeof x.num === 'number' || (typeof x.num === 'string' && /^\d+$/.test(x.num)))
                    ? Number(x.num) : NaN);
            if (counts.every(x => Number.isSafeInteger(x) && x >= 0)) {
                result.valid = true;
                result.count = counts.reduce((a, b) => a + b, 0);
                if (!Number.isSafeInteger(result.count)) result.valid = false;
            }
        }
        window.__huyaInventory = result;
    });
    xhr.fail(function() {
        window.__huyaInventory = {loaded: true, valid: false, count: null};
    });
});
return true;
"""


class HuYaAuto:
    def __init__(self, mode='diagnose'):
        if mode not in ('diagnose', 'send'):
            raise HuyaError('CONFIG_ERROR', 'unknown mode')
        self.mode = mode
        self.cookie = os.getenv('HUYA_COOKIE', '').strip()
        self.rooms = self._parse_rooms(os.getenv('HUYA_ROOMS', ''))
        self.outcome = 'NOT_STARTED'
        self.inventory = None
        self.submitted = 0
        self.debug_dir = Path(__file__).resolve().parent / 'debug_artifacts'
        self.debug_dir.mkdir(exist_ok=True, mode=0o700)
        if not self.cookie:
            raise HuyaError('CONFIG_ERROR', 'HUYA_COOKIE is required')
        if mode == 'send' and not self.rooms:
            raise HuyaError('CONFIG_ERROR', 'HUYA_ROOMS is required for sending')
        self.driver = self._init_browser()
        self.wait = WebDriverWait(self.driver, 15)

    @staticmethod
    def _parse_rooms(value):
        rooms = []
        for raw in value.split(','):
            raw = raw.strip()
            if not raw:
                continue
            if not raw.isdecimal() or int(raw) <= 0:
                raise HuyaError('CONFIG_ERROR', 'invalid room identifier')
            room = int(raw)
            if room not in rooms:
                rooms.append(room)
        return rooms

    def _init_browser(self):
        options = Options()
        # Do not wait for ads/iframes/analytics or a stalled renderer load event.
        # Each navigation below waits for an explicit application readiness test.
        options.page_load_strategy = 'none'
        if os.getenv('HEADLESS', 'true').lower() != 'false':
            options.add_argument('--headless=new')
        for option in ('--no-sandbox', '--disable-dev-shm-usage', '--window-size=1920,1080'):
            options.add_argument(option)
        browser = os.getenv('CHROME_BINARY') or shutil.which('google-chrome') or shutil.which('chromium')
        if browser:
            options.binary_location = browser
        driver_path = os.getenv('CHROMEDRIVER') or shutil.which('chromedriver')
        service = Service(driver_path or ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(12)
        driver.set_script_timeout(8)
        # Bound the transport as well as explicit waits when the renderer wedges.
        driver.command_executor._client_config.timeout = 30
        print('[BROWSER] version=' + str(driver.capabilities.get('browserVersion', 'unknown')))
        print('[BROWSER] navigation=none; explicit readiness waits enabled')
        return driver

    def _debug_capture(self, label):
        """Allowlisted diagnostics only, safe for a public Actions artifact."""
        data = {'stage': re.sub(r'\d+', 'n', label), 'mode': self.mode}
        try:
            host = urlsplit(self.driver.current_url).hostname
            data['host'] = host if host in ('i.huya.com', 'hd.huya.com', 'www.huya.com', 'huya.com') else 'other'
            data.update(self.driver.execute_script("""
                return {
                    ready_state: document.readyState,
                    body_present: Boolean(document.body),
                    login_marker_present: Boolean(document.getElementById('huyaNum')),
                    pack_tab_present: Boolean(document.getElementById('packTab')),
                    jquery_present: Boolean(window.jQuery),
                    inventory_response_seen: Boolean(window.__huyaInventory && window.__huyaInventory.loaded),
                    inventory_items_rendered: document.querySelectorAll('#myWrap li[data-num]').length
                };
            """))
        except Exception as exc:
            data['capture_error'] = type(exc).__name__
        path = self.debug_dir / (re.sub(r'[^a-zA-Z_]', '_', data['stage']) + '.json')
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        print('[DIAGNOSTIC] ' + json.dumps(data, ensure_ascii=False))

    def _safe_get(self, url, label, ready=None, timeout=40, attempts=2):
        """Retry navigation only; never wraps clicks that submit gifts."""
        expected = urlsplit(url)
        hosts = {expected.hostname}
        if expected.hostname == 'huya.com':
            hosts.add('www.huya.com')

        marker = None

        def page_ready(driver):
            current = urlsplit(driver.current_url)
            if current.hostname not in hosts or current.path != expected.path or current.scheme != expected.scheme:
                return False
            actual_query = parse_qs(current.query)
            if any(actual_query.get(key) != value for key, value in parse_qs(expected.query).items()):
                return False
            if driver.execute_script('return window.__huyaNavigationMarker || null') == marker:
                return False
            if not driver.execute_script('return Boolean(document.body)'):
                return False
            return ready(driver) if ready else True

        for attempt in range(1, attempts + 1):
            try:
                marker = uuid.uuid4().hex
                # Fail this attempt if we cannot mark the old document: accepting
                # its DOM after an asynchronous same-URL navigation is unsafe.
                self.driver.execute_script('window.__huyaNavigationMarker = arguments[0]', marker)
                try:
                    self.driver.get(url)
                except TimeoutException:
                    # A load timeout is recoverable only if the target becomes usable.
                    print(f'[NAV] {label}: load event timeout, checking readiness')
                WebDriverWait(self.driver, timeout, poll_frequency=0.5).until(page_ready)
                print(f'[NAV] {label}: ready attempt={attempt}')
                return True
            except DRIVER_ERRORS as exc:
                print(f'[NAV] {label}: not ready attempt={attempt} error={type(exc).__name__}')
                self._debug_capture(label)
                if attempt < attempts:
                    try:
                        self.driver.execute_script('window.stop()')
                    except DRIVER_ERRORS:
                        pass
                    time.sleep(2)
        return False

    def login(self):
        print('[LOGIN] checking login')
        if not self._safe_get(cfg.URLS['user_index'], 'login_bootstrap'):
            raise HuyaError('LOGIN_PAGE_UNAVAILABLE', 'user page did not become ready')
        count = 0
        for part in self.cookie.split(';'):
            if '=' not in part:
                continue
            name, value = part.strip().split('=', 1)
            try:
                self.driver.add_cookie({'name': name.strip(), 'value': value.strip(),
                                        'domain': '.huya.com', 'path': '/'})
                count += 1
            except WebDriverException:
                pass
        print(f'[COOKIE] injected_count={count}')
        marker = EC.visibility_of_element_located((By.ID, cfg.LOGIN['huya_num']))
        if not self._safe_get(cfg.URLS['user_index'], 'login_check', ready=marker):
            raise HuyaError('LOGIN_UNCONFIRMED', 'login marker missing; expired cookie or page failure')
        print('[LOGIN] confirmed (username not logged)')
        return True

    @staticmethod
    def _parse_inventory_response(response):
        if (not isinstance(response, dict) or response.get('status') != 200 or
                not isinstance(response.get('data'), dict) or
                not isinstance(response['data'].get('package'), list)):
            raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory response invalid')
        total = 0
        for item in response['data']['package']:
            if not isinstance(item, dict) or not isinstance(item.get('cName'), str) or not item['cName'].strip():
                raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory item malformed')
            if item['cName'] != '虎粮':
                continue
            value = item.get('num')
            if isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
                value = int(value)
            if type(value) is not int or value < 0 or value > 2**53 - 1:
                raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory count malformed')
            total += value
        if total > 2**53 - 1:
            raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory count out of range')
        return total

    def _get_hl_count_api(self):
        # Same two GET endpoints used by pay/js/mainv2.js handlePackage().
        # No payment/gift endpoint; redirects rejected; cookies scoped to Huya.
        with requests.Session() as session:
            for part in self.cookie.split(';'):
                if '=' in part:
                    name, value = part.strip().split('=', 1)
                    session.cookies.set(name.strip(), value.strip(), domain='.huya.com', path='/')
            session.headers.update({'Referer': cfg.URLS['pay_index'],
                                    'User-Agent': 'Mozilla/5.0 Chrome/152.0.0.0 Safari/537.36'})
            url = 'https://q.huya.com/index.php'
            signed = session.get(url, params={'m': 'PackageApi', 'do': 'getTimeSign'},
                                 timeout=(8, 20), allow_redirects=False)
            if signed.status_code != 200:
                raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory signing HTTP failure')
            signed = signed.json()
            if (not isinstance(signed, dict) or signed.get('status') != 200 or
                    not isinstance(signed.get('data'), dict) or
                    not signed['data'].get('time') or not signed['data'].get('sign')):
                raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory signing rejected')
            response = session.get(url, params={'m': 'PackageApi', 'do': 'listTotal',
                'time': signed['data']['time'], 'sign': signed['data']['sign']},
                timeout=(8, 20), allow_redirects=False)
            if response.status_code != 200:
                raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory HTTP failure')
            return self._parse_inventory_response(response.json())

    def get_hl_count(self):
        for attempt in range(1, 3):
            try:
                count = self._get_hl_count_api()
                print(f'[INVENTORY] read-only API confirmed; ordinary_huliang={count}')
                return count
            except (requests.RequestException, ValueError, HuyaError) as exc:
                print(f'[INVENTORY] read-only API attempt={attempt} error={type(exc).__name__}')
                if attempt < 2:
                    time.sleep(2)
        print('[INVENTORY] API unverified; trying browser inventory as fallback')
        return self._get_hl_count_page()

    def _get_hl_count_page(self):
        print('[INVENTORY] querying ordinary 虎粮')
        for attempt in range(1, 3):
            try:
                def ready(driver):
                    tab = EC.element_to_be_clickable((By.ID, cfg.PAY_PAGE['pack_tab']))(driver)
                    # The page binds navigation handlers in mainv2.js. A static
                    # packTab existing in HTML alone does not prove JS is ready.
                    handlers = driver.execute_script(PACK_READY)
                    return tab if handlers else False
                if not self._safe_get(cfg.URLS['pay_index'], 'inventory_page', ready=ready,
                                      timeout=45, attempts=1):
                    raise TimeoutException()
                if not self.driver.execute_script(INVENTORY_OBSERVER):
                    raise TimeoutException()
                self.driver.find_element(By.ID, cfg.PAY_PAGE['pack_tab']).click()

                def inventory_loaded(driver):
                    result = driver.execute_script('return window.__huyaInventory || {loaded:false}')
                    return result if result.get('loaded') else False

                result = WebDriverWait(self.driver, 30, poll_frequency=0.5).until(inventory_loaded)
                count = result.get('count')
                if not result.get('valid') or type(count) is not int or count < 0:
                    raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory response invalid')
                print(f'[INVENTORY] query confirmed; ordinary_huliang={count}')
                return count
            except (*DRIVER_ERRORS, HuyaError) as exc:
                print(f'[INVENTORY] failed attempt={attempt} error={type(exc).__name__}')
                self._debug_capture('inventory_query')
                if attempt < 2:
                    time.sleep(2)
        raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory could not be verified; NOT zero stock')

    def _gift_result(self):
        result = self.driver.execute_script('return window.__huyaGiftResult || {status:"pending"}')
        status = result.get('status')
        if status == 'rejected':
            raise HuyaError('SEND_REJECTED', 'page reported a business failure; do not retry')
        return status == 'success'

    def _submit_gift(self):
        if self.mode != 'send':
            raise HuyaError('SEND_DISABLED', 'diagnose mode cannot submit a gift')
        if not self.driver.execute_script(GIFT_RESULT_OBSERVER):
            raise HuyaError('SEND_PREPARATION_FAILED', 'stale toast present')
        self.wait.until(EC.element_to_be_clickable((By.CLASS_NAME, cfg.GIFT['send_class']))).click()

        def confirmation_or_result(driver):
            if self._gift_result():
                return 'success'
            button = EC.element_to_be_clickable((By.CLASS_NAME, cfg.GIFT['confirm_class']))(driver)
            return button if button else False

        action = self.wait.until(confirmation_or_result)
        if action != 'success':
            action.click()  # Exactly once. Never retry either submission click.
            WebDriverWait(self.driver, 25, poll_frequency=0.2).until(lambda _: self._gift_result())
        print('[SEND] fresh business success confirmed')

    def send_to_room(self, room_id, count):
        if self.mode != 'send':
            raise HuyaError('SEND_DISABLED', 'diagnose mode cannot call gift delivery')
        if count <= 0:
            return 0
        try:
            room_ready = lambda driver: driver.execute_script(
                'return Boolean(document.body.dataset.lp && document.body.dataset.gid)')
            if not self._safe_get(cfg.URLS['room_base'].format(room_id), 'room', ready=room_ready):
                raise HuyaError('SEND_PREPARATION_FAILED', 'room parameters unavailable')
            lp, gid = self.driver.execute_script('return [document.body.dataset.lp, document.body.dataset.gid]')
            gift_ready = EC.presence_of_all_elements_located((By.CLASS_NAME, cfg.GIFT['item_class']))
            if not self._safe_get(cfg.URLS['gift_tab'].format(lp=lp, gid=gid), 'gift_page', ready=gift_ready):
                raise HuyaError('SEND_PREPARATION_FAILED', 'gift page unavailable')
            items = self.driver.find_elements(By.CLASS_NAME, cfg.GIFT['item_class'])
            ordinary = [item for item in items if re.search(r'(?:^|\s)虎粮(?:\s|$)', item.text)]
            if len(ordinary) != 1:
                raise HuyaError('GIFT_AMBIGUOUS', 'ordinary gift missing or ambiguous')
            ActionChains(self.driver).move_to_element(ordinary[0]).pause(1).perform()
            inp = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, cfg.GIFT['input_css'])))
            inp.click()
            inp.clear()
            inp.send_keys(str(count))
            if inp.get_attribute('value') != str(count):
                raise HuyaError('SEND_PREPARATION_FAILED', 'gift count did not match requested value')
            self._submit_gift()
            print(f'[SEND] confirmed count={count}')
            return count
        except Exception as exc:
            self._debug_capture('send_failed_or_unknown')
            raise HuyaError('SEND_FAILED_OR_UNKNOWN', type(exc).__name__) from None

    def _record_result(self):
        data = {'mode': self.mode, 'outcome': self.outcome,
                'ordinary_huliang': self.inventory, 'submitted': self.submitted}
        (self.debug_dir / 'result.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print('[RESULT] ' + json.dumps(data, ensure_ascii=False))

    def run(self):
        ok = False
        try:
            self.login()
            self.inventory = self.get_hl_count()
            if self.mode == 'diagnose':
                self.outcome = 'DIAGNOSE_OK'
                print('[DIAGNOSE] login and inventory verified; gift delivery disabled')
                ok = True
            elif self.inventory == 0:
                self.outcome = 'EMPTY_STOCK'
                print('[INVENTORY] verified empty; nothing to send')
                ok = True
            else:
                per, remainder = divmod(self.inventory, len(self.rooms))
                for index, room in enumerate(self.rooms):
                    self.submitted += self.send_to_room(room, per + (index < remainder))
                remaining = self.get_hl_count()
                ok = self.submitted == self.inventory and remaining == 0
                self.outcome = 'SENT_INVENTORY_VERIFIED' if ok else 'SEND_FAILED_OR_UNKNOWN'
        except HuyaError as exc:
            self.outcome = exc.code
            print(f'[ERROR] {exc.code}: {exc}')
        except Exception as exc:
            self.outcome = 'UNEXPECTED_ERROR'
            print('[ERROR] unexpected ' + type(exc).__name__)
        finally:
            try:
                self._record_result()
            except Exception as exc:
                print('[RESULT] record failed: ' + type(exc).__name__)
            try:
                self.driver.quit()
            except Exception:
                print('[EXIT] browser cleanup failed')
        return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('diagnose', 'send'), default='diagnose')
    args = parser.parse_args()
    try:
        app = HuYaAuto(mode=args.mode)
    except HuyaError as exc:
        print(f'[ERROR] {exc.code}: {exc}')
        return 2
    except Exception as exc:
        print('[ERROR] BROWSER_START_FAILED: ' + type(exc).__name__)
        return 2
    return 0 if app.run() else 1


if __name__ == '__main__':
    sys.exit(main())
