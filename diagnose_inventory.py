#!/usr/bin/env python3
"""Read-only comparison. No gift clicks, account details or credential output."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from selenium.webdriver.support.ui import WebDriverWait
from main import HuYaAuto, HuyaError
import config as cfg


def parse_gift_rows(rows):
    counts={'ordinary':0,'super_fans':0}
    names={'虎粮':'ordinary','超粉虎粮':'super_fans'}
    seen=set()
    if not isinstance(rows,list) or not rows:
        raise ValueError('No proven inventory rows; not zero')
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('name'),str):raise ValueError('Invalid row')
        name=row['name'];value=row.get('count')
        if not isinstance(value,str) or not re.fullmatch(r'[0-9]+',value):raise ValueError('Non-exact count')
        count=int(value)
        if count>2**53-1:raise ValueError('Unsafe count')
        if name in names:
            if name in seen:raise ValueError('Ambiguous gift')
            seen.add(name);counts[names[name]]=count
    return counts


def main():
    out={'mode':'read_only_dual','room':998,'submitted':0,'super_fans_submitted':0,
         'package_api':None,'gift_page':None,'account_identity':'unconfirmed',
         'phone_account_identity':'not_available','outcome':'NOT_CONFIRMED'}
    bot=None
    try:
        bot=HuYaAuto('diagnose')
        out['package_api']=bot._get_inventory_api()
        # Compare default URL against cache-busted read-only endpoints.
        import requests
        from email.utils import parsedate_to_datetime
        cache_meta=[]
        with requests.Session() as session:
            for part in bot.cookie.split(';'):
                if '=' in part:
                    name,value=part.strip().split('=',1)
                    session.cookies.set(name.strip(),value.strip(),domain='.huya.com',path='/')
            session.headers.update({'Referer':cfg.URLS['pay_index'],'User-Agent':'Mozilla/5.0 Chrome/152.0.0.0 Safari/537.36'})
            def safe_metadata(response,*args,**kwargs):
                date=response.headers.get('Date')
                try: age_seconds=round(time.time()-parsedate_to_datetime(date).timestamp()) if date else None
                except Exception: age_seconds=None
                age=response.headers.get('Age','')
                cache_meta.append({'response_age_seconds':age_seconds,
                    'age_header_seconds':int(age) if age.isdigit() else None,
                    'cache_hit': 'HIT' in response.headers.get('X-Cache','').upper()})
            session.hooks['response']=[safe_metadata]
            signed=bot._package_api_get(session,'getTimeSign',{'_':str(time.time_ns())})
            response=bot._package_api_get(session,'listTotal',{'time':signed['data']['time'],
                'sign':signed['data']['sign'],'_':str(time.time_ns())})
            out['package_api_cache_busted']=bot._parse_inventory_response(response)
        out['read_only_cache_metadata']=cache_meta
        bot._ensure_browser_session()
        def room_ready(driver):
            return driver.execute_script('return Boolean(document.body && document.body.dataset.lp && document.body.dataset.gid)')
        if not bot._safe_get(cfg.URLS['room_base'].format(998),'read_only_room',ready=room_ready):raise ValueError('Room not ready')
        lp,gid=bot.driver.execute_script('return [document.body.dataset.lp,document.body.dataset.gid]')
        if not str(lp).isdigit() or not str(gid).isdigit():raise ValueError('Room params invalid')
        url=cfg.URLS['gift_tab'].format(lp=lp,gid=gid)
        if not bot._safe_get(url,'read_only_gift',ready=lambda d:d.execute_script('return Boolean(document.body)'),timeout=30):raise ValueError('Gift not ready')
        expected_uid=next((part.strip().split('=',1)[1] for part in bot.cookie.split(';') if part.strip().startswith('udb_uid=')),None)
        actual_uid=bot.driver.execute_script("return (document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('udb_uid='))||'').slice(8)")
        out['account_identity']='browser_cookie_matches_configured_uid' if expected_uid and actual_uid==expected_uid else 'unconfirmed'
        # No gift hover, count input, send or confirm interactions.
        # A missing/empty page is NOT accepted as zero inventory.
        def rows_ready(driver):
            rows=driver.execute_script("""return Array.from(document.querySelectorAll('.m-gift-item')).map(e=>({
                name:e.querySelector(':scope > p')?.textContent?.trim(),
                count:e.querySelector('.c-count')?.textContent?.trim()}));""")
            return rows if rows else False
        rows=WebDriverWait(bot.driver,45).until(rows_ready)
        out['gift_page']=parse_gift_rows(rows)
        out['outcome']='SOURCES_MATCH' if out['package_api']==out['gift_page'] else 'SOURCES_DISAGREE'
    except HuyaError as exc:
        out['error_category']=exc.code
    except Exception as exc:
        out['error_category']=type(exc).__name__
    finally:
        if bot and bot.driver:
            try:bot.driver.quit()
            except Exception:pass
        directory=Path('dual_diagnostics');directory.mkdir(exist_ok=True)
        (directory/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
        print(json.dumps(out,ensure_ascii=False))
    return 0 if out['outcome'] in ('SOURCES_MATCH','SOURCES_DISAGREE') else 1

if __name__=='__main__':raise SystemExit(main())
