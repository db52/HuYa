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
GIFTS = {'ordinary': '虎粮', 'super_fans': '超粉虎粮'}
MAX_COUNT = 2**53 - 1

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
        const result = {loaded: true, valid: false, counts: null};
        if (response && Number.isInteger(response.status) && response.status >= 0 && response.status <= 9999)
            result.business_status = response.status;
        if (result.business_status === 200 && response.data &&
            Array.isArray(response.data.package) &&
            response.data.package.every(x => x && typeof x.cName === 'string' && x.cName.trim())) {
            const counts = {ordinary: 0, super_fans: 0};
            result.valid = response.data.package.every(x => {
                const n = (typeof x.num === 'number' ||
                    (typeof x.num === 'string' && /^[0-9]+$/.test(x.num))) ? Number(x.num) : NaN;
                if (!Number.isSafeInteger(n) || n < 0) return false;
                const key = x.cName === '虎粮' ? 'ordinary' : x.cName === '超粉虎粮' ? 'super_fans' : null;
                if (key) counts[key] += n;
                return Object.values(counts).every(Number.isSafeInteger);
            });
            if (result.valid) result.counts = counts;
        }
        window.__huyaInventory = result;
    });
    xhr.fail(function(xhr) {
        const result = {loaded: true, valid: false, count: null, network_failed: true};
        if (xhr && Number.isInteger(xhr.status) && xhr.status >= 100 && xhr.status <= 599)
            result.http_status = xhr.status;
        window.__huyaInventory = result;
    });
});
return true;
"""


class HuYaAuto:
    def __init__(self, mode='diagnose'):
        # Establish artifact state before validation. Neither diagnostics nor
        # verified zero stock requires a browser/driver download.
        self.mode = mode if mode in ('diagnose', 'send') else 'diagnose'
        self.outcome = 'NOT_STARTED'
        self.inventory = None
        self.submitted = 0
        self.super_fans_inventory = None
        self.super_fans_submitted = 0
        self.driver = None
        self.wait = None
        self._browser_authenticated = False
        self.debug_dir = Path(__file__).resolve().parent / 'debug_artifacts'
        self.debug_dir.mkdir(exist_ok=True, mode=0o700)
        try:
            if mode not in ('diagnose', 'send'):
                raise HuyaError('CONFIG_ERROR', 'unknown mode')
            self.cookie = os.getenv('HUYA_COOKIE', '').strip()
            self.rooms = self._parse_rooms(os.getenv('HUYA_ROOMS', ''))
            if not self.cookie:
                raise HuyaError('CONFIG_ERROR', 'HUYA_COOKIE is required')
            if mode == 'send' and not self.rooms:
                raise HuyaError('CONFIG_ERROR', 'HUYA_ROOMS is required for sending')
        except HuyaError as exc:
            self.outcome = exc.code
            self._try_record_result()
            raise

    def _ensure_browser(self):
        if getattr(self, 'driver', None) is not None:
            return
        try:
            self.driver = self._init_browser()
            self.wait = WebDriverWait(self.driver, 15)
        except Exception:
            # Driver installation/start errors may contain private paths/URLs.
            raise HuyaError('BROWSER_START_FAILED', 'browser could not be started') from None

    def _ensure_browser_session(self):
        self._ensure_browser()
        if getattr(self, '_browser_authenticated', False):
            return
        injected = 0
        for part in self.cookie.split(';'):
            if '=' not in part:
                continue
            name, value = part.strip().split('=', 1)
            result = self.driver.execute_cdp_cmd('Network.setCookie', {
                'name': name.strip(), 'value': value.strip(),
                'domain': '.huya.com', 'path': '/', 'secure': True,
            })
            injected += bool(result.get('success'))
        if not injected:
            raise HuyaError('LOGIN_UNCONFIRMED', 'browser cookie injection failed')
        self._browser_authenticated = True
        print(f'[COOKIE] browser injected_count={injected}')

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
        self.driver = driver  # Keep a partial startup available for run() cleanup.
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
        print('[LOGIN] checking authenticated read-only inventory first')
        for attempt in range(1, 3):
            try:
                self._authenticated_inventory = self._validate_inventory(self._get_inventory_api())
                print('[LOGIN] authenticated inventory API confirmed')
                return True
            except (requests.RequestException, ValueError, HuyaError, *DRIVER_ERRORS) as exc:
                detail = f'{exc.code}: {exc}' if isinstance(exc, HuyaError) else type(exc).__name__
                print(f'[LOGIN] API check attempt={attempt} error={detail}')
                if isinstance(exc, HuyaError) and exc.code == 'LOGIN_REQUIRED':
                    raise  # Retrying or a browser cannot fix explicit rejection.
                if attempt < 2:
                    time.sleep(2)
        print('[LOGIN] API unverified; checking browser login')
        self._ensure_browser()
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
        self._browser_authenticated = True
        return True

    @staticmethod
    def _parse_inventory_response(response):
        if (not isinstance(response, dict) or type(response.get('status')) is not int or
                response['status'] != 200 or
                not isinstance(response.get('data'), dict) or
                not isinstance(response['data'].get('package'), list)):
            raise HuyaError('INVENTORY_RESPONSE_INVALID', 'listTotal: HTTP 200; inventory response invalid')
        totals = dict.fromkeys(GIFTS, 0)
        for item in response['data']['package']:
            if not isinstance(item, dict) or not isinstance(item.get('cName'), str) or not item['cName'].strip():
                raise HuyaError('INVENTORY_RESPONSE_INVALID', 'listTotal: HTTP 200; inventory item malformed')
            value = item.get('num')
            if isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
                value = int(value)
            if type(value) is not int or value < 0 or value > 2**53 - 1:
                raise HuyaError('INVENTORY_RESPONSE_INVALID', 'listTotal: HTTP 200; inventory count malformed')
            for key, name in GIFTS.items():
                if item['cName'] == name:
                    totals[key] += value
                    if totals[key] > MAX_COUNT:
                        raise HuyaError('INVENTORY_RESPONSE_INVALID', 'listTotal: HTTP 200; inventory count out of range')
        return totals

    def _get_inventory_api(self):
        # Same two GET endpoints used by pay/js/mainv2.js handlePackage().
        # No payment/gift endpoint; redirects rejected; cookies scoped to Huya.
        with requests.Session() as session:
            for part in self.cookie.split(';'):
                if '=' in part:
                    name, value = part.strip().split('=', 1)
                    session.cookies.set(name.strip(), value.strip(), domain='.huya.com', path='/')
            session.headers.update({'Referer': cfg.URLS['pay_index'],
                                    'User-Agent': 'Mozilla/5.0 Chrome/152.0.0.0 Safari/537.36'})
            signed = self._package_api_get(session, 'getTimeSign')
            if (not isinstance(signed.get('data'), dict) or
                    not signed['data'].get('time') or not signed['data'].get('sign')):
                raise HuyaError('INVENTORY_RESPONSE_INVALID',
                                'getTimeSign: HTTP 200; signing fields invalid')
            response = self._package_api_get(session, 'listTotal', {
                'time': signed['data']['time'], 'sign': signed['data']['sign']})
            return self._parse_inventory_response(response)

    @staticmethod
    def _package_api_get(session, action, extra=None):
        # Local constants/numeric statuses only: never upstream messages, bodies,
        # signed URLs, credentials or transport exception text.
        if action not in ('getTimeSign', 'listTotal'):
            raise HuyaError('CONFIG_ERROR', 'read-only package action required')
        params = dict(extra or {})
        params.update({'m': 'PackageApi', 'do': action})
        try:
            response = session.get('https://q.huya.com/index.php', params=params,
                                   timeout=(8, 20), allow_redirects=False)
        except requests.Timeout:
            raise HuyaError('INVENTORY_NETWORK_TIMEOUT', f'{action}: request failed') from None
        except requests.exceptions.SSLError:
            raise HuyaError('INVENTORY_TLS_FAILED', f'{action}: request failed') from None
        except requests.RequestException:
            raise HuyaError('INVENTORY_NETWORK_FAILED', f'{action}: request failed') from None
        http = response.status_code
        if type(http) is not int or not 100 <= http <= 599:
            raise HuyaError('INVENTORY_RESPONSE_INVALID', f'{action}: invalid HTTP status')
        if http != 200:
            raise HuyaError('INVENTORY_HTTP_FAILED', f'{action}: HTTP {http}')
        try:
            payload = response.json()
        except ValueError:
            raise HuyaError('INVENTORY_RESPONSE_INVALID',
                            f'{action}: HTTP 200; non-JSON response') from None
        status = payload.get('status') if isinstance(payload, dict) else None
        if type(status) is not int or not 0 <= status <= 9999:
            raise HuyaError('INVENTORY_RESPONSE_INVALID',
                            f'{action}: HTTP 200; invalid business status')
        if status == 501:
            # Only JSON 501 at HTTP 200 on these endpoints means not logged in.
            raise HuyaError('LOGIN_REQUIRED',
                            f'{action}: HTTP 200; business_status=501; login required')
        if status != 200:
            raise HuyaError('INVENTORY_API_REJECTED',
                            f'{action}: HTTP 200; business_status={status}')
        return payload

    @staticmethod
    def _validate_inventory(counts):
        if (not isinstance(counts, dict) or set(counts) != set(GIFTS) or
                any(type(n) is not int or not 0 <= n <= MAX_COUNT for n in counts.values())):
            raise HuyaError('INVENTORY_RESPONSE_INVALID', 'inventory snapshot invalid')
        return dict(counts)

    def _get_hl_count_api(self):
        """Legacy ordinary-only projection of a single full API snapshot."""
        return self._get_inventory_api()['ordinary']

    def get_hl_count(self):
        """Legacy ordinary-only projection; delivery always uses get_inventory."""
        return self.get_inventory()['ordinary']

    def get_inventory(self):
        # Login's authenticated snapshot is also the initial stock read, not a
        # second request that could disagree. Every later read is independent.
        if hasattr(self, '_authenticated_inventory'):
            counts = self._authenticated_inventory
            del self._authenticated_inventory
            return self._validate_inventory(counts)
        for attempt in range(1, 3):
            try:
                counts = self._validate_inventory(self._get_inventory_api())
                print(f'[INVENTORY] read-only API confirmed; ordinary_huliang={counts["ordinary"]}; super_fans_huliang={counts["super_fans"]}')
                return counts
            except (requests.RequestException, ValueError, HuyaError) as exc:
                detail = f'{exc.code}: {exc}' if isinstance(exc, HuyaError) else type(exc).__name__
                print(f'[INVENTORY] read-only API attempt={attempt} error={detail}')
                if isinstance(exc, HuyaError) and exc.code == 'LOGIN_REQUIRED':
                    raise
                if attempt < 2:
                    time.sleep(2)
        print('[INVENTORY] API unverified; trying browser inventory as fallback')
        return self._get_inventory_page()

    def _get_hl_count_page(self):
        return self._get_inventory_page()['ordinary']

    def _get_inventory_page(self):
        self._ensure_browser_session()
        print('[INVENTORY] querying 虎粮 and 超粉虎粮 in one snapshot')
        failure = HuyaError('INVENTORY_PAGE_UNAVAILABLE', 'inventory page did not become ready')
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
                status = result.get('business_status')
                if type(status) is int and 0 <= status <= 9999:
                    if status == 501:
                        raise HuyaError('LOGIN_REQUIRED', 'listTotal: browser business_status=501; login required')
                    if status != 200:
                        raise HuyaError('INVENTORY_API_REJECTED', f'listTotal: browser business_status={status}')
                if result.get('network_failed'):
                    http = result.get('http_status')
                    if type(http) is int and 100 <= http <= 599:
                        raise HuyaError('INVENTORY_HTTP_FAILED', f'listTotal: browser HTTP {http}')
                    raise HuyaError('INVENTORY_NETWORK_FAILED', 'listTotal: browser request failed')
                if (type(status) is not int or status != 200 or result.get('valid') is not True):
                    raise HuyaError('INVENTORY_RESPONSE_INVALID', 'listTotal: browser inventory response invalid')
                counts = self._validate_inventory(result.get('counts'))
                print(f'[INVENTORY] query confirmed; ordinary_huliang={counts["ordinary"]}; super_fans_huliang={counts["super_fans"]}')
                return counts
            except (*DRIVER_ERRORS, HuyaError) as exc:
                if isinstance(exc, HuyaError):
                    failure = exc
                    detail = f'{exc.code}: {exc}'
                else:
                    failure = HuyaError('INVENTORY_PAGE_UNAVAILABLE', 'inventory page/response did not become ready')
                    detail = type(exc).__name__
                print(f'[INVENTORY] failed attempt={attempt} error={detail}')
                if failure.code == 'LOGIN_REQUIRED':
                    raise failure
                self._debug_capture('inventory_query')
                if attempt < 2:
                    time.sleep(2)
        raise failure

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

    def send_to_room(self, room_id, count, *, gift_name):
        if self.mode != 'send':
            raise HuyaError('SEND_DISABLED', 'diagnose mode cannot call gift delivery')
        if gift_name not in GIFTS.values() or type(count) is not int or not 0 <= count <= MAX_COUNT:
            raise HuyaError('CONFIG_ERROR', 'unsupported gift or invalid count')
        if count <= 0:
            return 0
        self._ensure_browser_session()
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
            pattern = r'\A' + re.escape(gift_name) + r'\Z'
            selected = []
            for item in items:
                names = item.find_elements(By.CSS_SELECTOR, ':scope > p')
                if len(names) == 1 and re.fullmatch(pattern, names[0].text):
                    selected.append(item)
            if len(selected) != 1:
                raise HuyaError('GIFT_AMBIGUOUS', 'requested gift missing or ambiguous')
            ActionChains(self.driver).move_to_element(selected[0]).pause(1).perform()
            def selected_input(driver):
                # WebPackageV2 displays the active item's name in the hover
                # popup. Exclude its child price span, not part of the name.
                names = driver.execute_script("""
                    return Array.from(document.querySelectorAll('.g-present-content .present-info > .c-name'))
                        .map(p => Array.from(p.childNodes).filter(n => n.nodeType === Node.TEXT_NODE)
                            .map(n => n.textContent).join(''));
                """)
                if names != [gift_name]:
                    return False
                return EC.element_to_be_clickable((By.CSS_SELECTOR,
                    '.g-present-content ' + cfg.GIFT['input_css']))(driver)
            inp = self.wait.until(selected_input)
            inp.click()
            inp.clear()
            inp.send_keys(str(count))
            if inp.get_attribute('value') != str(count):
                raise HuyaError('SEND_PREPARATION_FAILED', 'gift count did not match requested value')
            if not selected_input(self.driver):
                raise HuyaError('SEND_PREPARATION_FAILED', 'selected gift identity changed')
            self._submit_gift()
            print(f'[SEND] confirmed gift={gift_name}; count={count}')
            return count
        except Exception as exc:
            self._debug_capture('send_failed_or_unknown')
            raise HuyaError('SEND_FAILED_OR_UNKNOWN', type(exc).__name__) from None

    def _record_result(self):
        data = {'mode': self.mode, 'outcome': self.outcome,
                'ordinary_huliang': self.inventory, 'submitted': self.submitted,
                'super_fans_huliang': self.super_fans_inventory,
                'super_fans_submitted': self.super_fans_submitted}
        (self.debug_dir / 'result.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print('[RESULT] ' + json.dumps(data, ensure_ascii=False))

    def _try_record_result(self):
        try:
            self._record_result()
        except Exception as exc:
            print('[RESULT] record failed: ' + type(exc).__name__)

    def run(self):
        ok = False
        try:
            self.login()
            stock = self._validate_inventory(self.get_inventory())
            self.inventory = stock['ordinary']
            self.super_fans_inventory = stock['super_fans']
            self.super_fans_submitted = 0
            if self.mode == 'diagnose':
                self.outcome = 'DIAGNOSE_OK'
                print('[DIAGNOSE] login and inventory verified; gift delivery disabled')
                ok = True
            elif not any(stock.values()):
                self.outcome = 'EMPTY_STOCK'
                print('[INVENTORY] verified empty; nothing to send')
                ok = True
            else:
                self._ensure_browser_session()
                expected = dict(stock)
                remaining = dict(stock)
                for key, gift_name in GIFTS.items():
                    if stock[key] == 0:
                        continue
                    per, remainder = divmod(stock[key], len(self.rooms))
                    field = 'submitted' if key == 'ordinary' else 'super_fans_submitted'
                    for index, room in enumerate(self.rooms):
                        count = per + int(index < remainder)
                        if not count:
                            continue
                        confirmed = self.send_to_room(room, count, gift_name=gift_name)
                        if type(confirmed) is not int or confirmed != count:
                            raise HuyaError('SEND_FAILED_OR_UNKNOWN', 'confirmed count mismatch; stop delivery')
                        setattr(self, field, getattr(self, field) + confirmed)
                    expected[key] = 0
                    remaining = self._validate_inventory(self.get_inventory())
                    if getattr(self, field) != stock[key] or remaining != expected:
                        raise HuyaError('SEND_FAILED_OR_UNKNOWN', 'inventory postcondition mismatch; stop delivery')
                ok = (self.submitted == stock['ordinary'] and
                      self.super_fans_submitted == stock['super_fans'] and not any(remaining.values()))
                self.outcome = 'SENT_INVENTORY_VERIFIED' if ok else 'SEND_FAILED_OR_UNKNOWN'
        except HuyaError as exc:
            self.outcome = exc.code
            print(f'[ERROR] {exc.code}: {exc}')
        except Exception as exc:
            self.outcome = 'UNEXPECTED_ERROR'
            print('[ERROR] unexpected ' + type(exc).__name__)
        finally:
            self._try_record_result()
            if getattr(self, 'driver', None) is not None:
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
