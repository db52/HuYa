#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
虎牙虎粮自动发放 - 精简稳定版
环境变量配置，核心逻辑极简
"""

import os
import sys
import time
import traceback
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

import config as cfg


class HuYaAuto:
    """精简稳定版虎牙虎粮自动发放"""

    def __init__(self):

        self.debug = ""

        if self.debug :
            print("从文件获取 HUYA_COOKIE")
            try:
                with open("cookie", "r", encoding="utf-8") as f:
                    self.cookie = f.read().strip()
            except FileNotFoundError:
                self.cookie = ""  # 文件不存在时为空
            self.rooms = ["998"]
        else :
            print("从环境变量获取 HUYA_COOKIE")
            self.cookie = os.getenv('HUYA_COOKIE', '').strip()
            self.rooms = self._parse_rooms(os.getenv('HUYA_ROOMS', ''))

        if not self.cookie:
            print("[ERROR] 未设置 HUYA_COOKIE")
            sys.exit(1)

        if not self.rooms:
            print("[WARN] 未设置房间号，使用默认房间")
            self.rooms = [518512, 518511]

        self.driver = self._init_browser()
        self.wait = WebDriverWait(self.driver, 5)
        self.debug_dir = Path(__file__).resolve().parent / 'debug_artifacts'
        self.debug_dir.mkdir(exist_ok=True)

    def _parse_rooms(self, rooms_str):
        rooms = []
        for s in rooms_str.split(','):
            s = s.strip()
            if s:
                try:
                    rooms.append(int(s))
                except ValueError:
                    print(f"[WARN] 跳过无效房间号: {s}")
        return rooms

    def _init_browser(self):
        chrome_options = Options()
        chrome_options.page_load_strategy = 'eager'

        if not self.debug:
            chrome_options.add_argument('--headless=new')

        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')

        chrome_options.add_argument('--disable-images')
        chrome_options.add_argument('--disable-javascript=false')

        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_argument('--window-size=1920,1080')

        print("[START] 启动浏览器")
        chromedriver_path = '/usr/bin/chromedriver'
        browser_binary = '/usr/bin/chromium'

        if os.path.exists(browser_binary):
            chrome_options.binary_location = browser_binary

        if os.path.exists(chromedriver_path):
            print(f"[DRIVER] 使用系统 chromedriver: {chromedriver_path}")
            service = Service(chromedriver_path)
        else:
            fallback_driver = ChromeDriverManager().install()
            print(f"[DRIVER] 使用 webdriver_manager: {fallback_driver}")
            service = Service(fallback_driver)

        driver = webdriver.Chrome(
            service=service,
            options=chrome_options
        )
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return driver

    def _debug_capture(self, label):
        ts = time.strftime('%Y%m%d_%H%M%S')
        base = self.debug_dir / f"{ts}_{label}"
        try:
            self.driver.save_screenshot(str(base.with_suffix('.png')))
        except Exception as e:
            print(f"[DEBUG] 截图失败({label}): {e}")

        try:
            html = self.driver.page_source
            base.with_suffix('.html').write_text(html, encoding='utf-8')
        except Exception as e:
            print(f"[DEBUG] 保存 HTML 失败({label}): {e}")

        try:
            meta = [
                f"label={label}",
                f"url={self.driver.current_url}",
                f"title={self.driver.title}",
            ]
            try:
                meta.append(f"readyState={self.driver.execute_script('return document.readyState')}")
            except Exception as e:
                meta.append(f"readyState_error={e}")
            base.with_suffix('.txt').write_text("\n".join(meta), encoding='utf-8')
            print(f"[DEBUG] 已保存现场: {base}")
        except Exception as e:
            print(f"[DEBUG] 保存元信息失败({label}): {e}")

    def _safe_get(self, url, label, timeout=30):
        old_timeout = None
        try:
            old_timeout = self.driver.timeouts.page_load
        except Exception:
            old_timeout = None

        try:
            self.driver.set_page_load_timeout(timeout)
            self.driver.get(url)
            print(f"[DEBUG] 页面导航成功({label}): {self.driver.current_url}")
            return True
        except Exception as e:
            current_url = ''
            title = ''
            try:
                current_url = self.driver.current_url
            except Exception:
                pass
            try:
                title = self.driver.title
            except Exception:
                pass
            print(f"[WARN] 页面导航异常({label}): {e}")
            print(f"[WARN] 当前 URL: {current_url}")
            print(f"[WARN] 当前标题: {title}")
            self._debug_capture(f"{label}_navigation_exception")
            if current_url and current_url.startswith('https://www.huya.com/'):
                print(f"[WARN] 虽然导航超时，但页面已到达虎牙房间，继续后续流程({label})")
                return True
            return False
        finally:
            try:
                if old_timeout is not None:
                    self.driver.set_page_load_timeout(old_timeout)
                else:
                    self.driver.set_page_load_timeout(300)
            except Exception:
                pass

    def login(self):
        print("[LOGIN] 登录中")
        if not self._safe_get(cfg.URLS["user_index"], 'login_user_index', timeout=20):
            print("[ERROR] 打开用户首页失败")
            return False
        time.sleep(cfg.TIMING["implicit_wait"])

        cnt = 0
        for line in self.cookie.split(';'):
            line = line.strip()
            if '=' not in line:
                continue
            name, val = line.split('=', 1)
            try:
                self.driver.add_cookie({
                    'name': name.strip(),
                    'value': val.strip(),
                    'domain': '.huya.com',
                    'path': '/'
                })
                cnt += 1
            except Exception:
                continue

        print(f"[COOKIE] 已添加 {cnt} 个Cookie")
        self.driver.refresh()
        time.sleep(cfg.TIMING["page_load_wait"])

        try:
            elem = self.wait.until(
                EC.presence_of_element_located((By.ID, cfg.LOGIN["huya_num"]))
            )
            username = elem.text.strip()
            print(f"[SUCCESS] 登录成功: {username}")
            return True
        except Exception:
            print("[ERROR] 登录失败")
            return False

    def get_hl_count(self):
        print("[SEARCH] 查询虎粮数量")
        if not self._safe_get(cfg.URLS["pay_index"], 'pay_index', timeout=20):
            print("[ERROR] 打开充值页面失败")
            return 0

        # 强制等待页面完全加载（GitHub Action 必须加长）
        time.sleep(3)

        try:
            # 等待并点击【背包】标签（最关键：必须等可点击）
            pack_tab = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.ID, cfg.PAY_PAGE["pack_tab"]))
            )
            pack_tab.click()
            time.sleep(1.5)  # 点击后必须等面板渲染

        except Exception:
            print("[WARN] 点击背包标签失败")
            return 0

        # 强化版 JS 获取虎粮（容错更强，支持异步加载）
        n = self.driver.execute_script('''
            let n = 0;
            let maxWait = 20;
            function findHuliang() {
                const items = document.querySelectorAll('li[data-num]');
                for (let item of items) {
                    let title = item.title || item.innerText || '';
                    if (title.includes('虎粮')) {
                        return item.getAttribute('data-num');
                    }
                }
                return null;
            }
            // 轮询查找（解决异步加载）
            while(maxWait-- > 0) {
                let res = findHuliang();
                if(res) return res;
                await new Promise(r => setTimeout(r, 200));
            }
            return 0;
        ''')

        hl = int(n) if n and str(n).isdigit() else 0
        print(f"[COUNT] 虎粮数量: {hl}")
        return hl

    def send_to_room(self, room_id, count):
        print(f"[GIFT] 房间 {room_id} 发送 {count} 个")
        if count <= 0:
            return 0

        try:
            room_url = cfg.URLS["room_base"].format(room_id)
            if not self._safe_get(room_url, f'room_{room_id}', timeout=25):
                print("[ERROR] 打开房间页失败")
                return 0
            time.sleep(cfg.TIMING["room_enter_wait"])
            print(f"[DEBUG] 已进入房间页: {self.driver.current_url}")
            self._debug_capture(f"room_{room_id}_after_enter")

            lp = self.driver.execute_script('return document.body.getAttribute("data-lp")')
            gid = self.driver.execute_script('return document.body.getAttribute("data-gid")')
            print(f"[DEBUG] 房间参数 lp={lp}, gid={gid}")

            if not lp or not gid:
                print("[ERROR] 获取房间参数失败")
                self._debug_capture(f"room_{room_id}_missing_lp_gid")
                return 0

            gift_url = cfg.URLS["gift_tab"].format(lp=lp, gid=gid)
            if not self._safe_get(gift_url, f'gift_tab_{room_id}', timeout=20):
                print("[ERROR] 打开送礼页失败")
                return 0
            time.sleep(cfg.TIMING["page_load_wait"])
            print(f"[DEBUG] 已进入送礼页: {self.driver.current_url}")
            self._debug_capture(f"room_{room_id}_gift_page_loaded")

            # 查找虎粮项
            items = self.wait.until(
                EC.presence_of_all_elements_located((By.CLASS_NAME, cfg.GIFT["item_class"]))
            )
            print(f"[DEBUG] 礼物项数量: {len(items)}")
            hu_liang = None
            for idx, item in enumerate(items):
                try:
                    txt = item.text.strip().replace('\n', ' ')
                except Exception:
                    txt = '<read-failed>'
                print(f"[DEBUG] 礼物项[{idx}]: {txt[:80]}")
                if "虎粮" in txt:
                    hu_liang = item
                    break
            if not hu_liang:
                print("[ERROR] 未找到虎粮")
                self._debug_capture(f"room_{room_id}_gift_not_found")
                return 0

            print("[DEBUG] 已定位到虎粮项，准备悬停")
            self._debug_capture(f"room_{room_id}_before_hover")

            # 悬停
            ActionChains(self.driver)\
                .move_to_element(hu_liang)\
                .pause(1)\
                .perform()
            time.sleep(1)
            self._debug_capture(f"room_{room_id}_after_hover")

            # 自定义数量
            inp = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, cfg.GIFT["input_css"]))
            )
            print("[DEBUG] 已找到自定义输入框")
            inp.click()
            inp.clear()
            inp.send_keys(str(count))
            try:
                print(f"[DEBUG] 输入框当前值: {inp.get_attribute('value')}")
            except Exception as e:
                print(f"[DEBUG] 读取输入框值失败: {e}")
            self._debug_capture(f"room_{room_id}_after_input")

            # 赠送
            send_btn = self.wait.until(
                EC.element_to_be_clickable((By.CLASS_NAME, cfg.GIFT["send_class"]))
            )
            print(f"[DEBUG] 已找到发送按钮: text={send_btn.text!r}")
            self._debug_capture(f"room_{room_id}_before_send_click")
            send_btn.click()
            print("[DEBUG] 已点击发送按钮")
            time.sleep(1)
            self._debug_capture(f"room_{room_id}_after_send_click")

            confirm_btn = self.wait.until(
                EC.element_to_be_clickable((By.CLASS_NAME, cfg.GIFT["confirm_class"]))
            )
            print(f"[DEBUG] 已找到确认按钮: text={confirm_btn.text!r}")
            self._debug_capture(f"room_{room_id}_before_confirm_click")
            confirm_btn.click()
            print("[DEBUG] 已点击确认按钮")
            time.sleep(1)
            self._debug_capture(f"room_{room_id}_after_confirm_click")

            print(f"[SUCCESS] 赠送成功: {count} 个")
            return count

        except Exception as e:
            print(f"[CRASH] 赠送失败: {e}")
            print(traceback.format_exc())
            self._debug_capture(f"room_{room_id}_exception")
            return 0

    def run(self):
        success = False
        try:
            print("=" * 40)
            print("[HUYA] 虎牙虎粮自动发放")
            print("=" * 40)
            print(f"房间列表: {self.rooms}")

            if not self.login():
                return False

            total = self.get_hl_count()
            if total <= 0:
                print("❌ 暂无虎粮")
                return False

            print(f"[TOTAL] 虎粮总数: {total}")

            # 分配
            n = len(self.rooms)
            per = total // n
            rem = total % n
            plan = []
            for i, rid in enumerate(self.rooms):
                c = per + 1 if i < rem else per
                plan.append((rid, c))

            print("\n[PLAN] 分配方案:")
            for rid, c in plan:
                print(f"  {rid}: {c}个")

            print("\n[SEND] 开始发送...")
            sent = 0
            for rid, c in plan:
                sent += self.send_to_room(rid, c)

            print(f"\n[DONE] 完成！已发送 {sent}/{total}")
            success = sent > 0
            return success

        except Exception as e:
            print(f"\n[CRASH] 程序异常: {e}")
            return False

        finally:
            # 必关浏览器，修复进程泄漏
            if hasattr(self, 'driver'):
                self.driver.quit()
                print("[EXIT] 浏览器已关闭")

            return success


def main():
    huya = HuYaAuto()
    res = huya.run()
    sys.exit(0 if res else 1)


if __name__ == '__main__':
    main()