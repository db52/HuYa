#!/usr/bin/env python3
"""Huya inventory diagnostics and explicitly enabled gift delivery."""
import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

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

        def page_ready(driver):
            current = urlsplit(driver.current_url)
            if current.hostname not in hosts or current.path != expected.path:
                return False
            if not driver.execute_script('return Boolean(document.body)'):
                return False
            return ready(driver) if ready else True

        for attempt in range(1, attempts + 1):
            try:
                try:
                    self.driver.get(url)
                except TimeoutException:
                    # A load timeout is recoverable only if the target becomes usable.
                    print(f'[NAV] {label}: load event timeout, checking readiness')
                WebDriverWait(self.driver, timeout, poll_frequency=0.5).until(page_ready)
                print(f'[NAV] {label}: ready attempt={attempt}')
                return True
            except WebDriverException as exc:
                print(f'[NAV] {label}: not ready attempt={attempt} error={type(exc).__name__}')
                self._debug_capture(label)
                if attempt < attempts:
                    try:
                        self.driver.execute_script('window.stop()')
                    except WebDriverException:
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

    def get_hl_count(self):
        print('[INVENTORY] querying ordinary 虎粮')
        for attempt in range(1, 3):
            try:
                def ready(driver):
                    tab = EC.element_to_be_clickable((By.ID, cfg.PAY_PAGE['pack_tab']))(driver)
                    # The page binds navigation handlers in mainv2.js. A static
                    # packTab existing in HTML alone does not prove JS is ready.
                    handlers = driver.execute_script("""
                        const jq = window.jQuery;
                        if (!jq || !jq._data) return false;
                        return [document, document.body, document.getElementById('nav')]
                            .some(x => x && (jq._data(x, 'events') || {}).click);
                    """)
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
            except (WebDriverException, HuyaError) as exc:
                print(f'[INVENTORY] failed attempt={attempt} error={type(exc).__name__}')
                self._debug_capture('inventory_query')
                if attempt < 2:
                    time.sleep(2)
        raise HuyaError('INVENTORY_QUERY_FAILED', 'inventory could not be verified; NOT zero stock')

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
            self.wait.until(EC.element_to_be_clickable((By.CLASS_NAME, cfg.GIFT['send_class']))).click()
            # No send/confirmation click is retried, even if response is unknown.
            self.wait.until(EC.element_to_be_clickable((By.CLASS_NAME, cfg.GIFT['confirm_class']))).click()
            print(f'[SEND] confirmation submitted count={count}; verifying later')
            return count
        except (WebDriverException, HuyaError) as exc:
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
            self._record_result()
            try:
                self.driver.quit()
            except WebDriverException:
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
