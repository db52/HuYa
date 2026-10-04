#!/usr/bin/env python3
"""Allowlisted Actions summary and Telegram incident-transition notices."""
import argparse
import json
import os
import re
import io
import zipfile
from pathlib import Path
import urllib.request
import urllib.error

OUTCOMES = {
    'DIAGNOSE_OK', 'EMPTY_STOCK', 'SENT_INVENTORY_VERIFIED',
    'LOGIN_REQUIRED', 'LOGIN_UNCONFIRMED', 'LOGIN_PAGE_UNAVAILABLE',
    'INVENTORY_QUERY_FAILED', 'INVENTORY_RESPONSE_INVALID', 'INVENTORY_PAGE_UNAVAILABLE',
    'INVENTORY_HTTP_FAILED', 'INVENTORY_API_REJECTED', 'INVENTORY_NETWORK_TIMEOUT',
    'INVENTORY_TLS_FAILED', 'INVENTORY_NETWORK_FAILED', 'CONFIG_ERROR', 'BROWSER_START_FAILED',
    'SEND_FAILED_OR_UNKNOWN', 'UNEXPECTED_ERROR', 'NOT_STARTED',
}
GOOD = {'DIAGNOSE_OK', 'EMPTY_STOCK', 'SENT_INVENTORY_VERIFIED'}

def safe_result(path):
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict): raise ValueError()
    except (OSError, ValueError):
        return {'mode': 'unknown', 'outcome': 'WORKFLOW_FAILED', 'ordinary_huliang': None, 'submitted': None}
    return {
        'mode': value.get('mode') if value.get('mode') in ('send', 'diagnose') else 'unknown',
        'outcome': value.get('outcome') if value.get('outcome') in OUTCOMES else 'WORKFLOW_FAILED',
        'ordinary_huliang': value.get('ordinary_huliang') if type(value.get('ordinary_huliang')) is int and 0 <= value['ordinary_huliang'] <= 2**53-1 else None,
        'submitted': value.get('submitted') if type(value.get('submitted')) is int and 0 <= value['submitted'] <= 2**53-1 else None,
    }

def notice_kind(previous, current):
    healthy = current['outcome'] in GOOD
    if not previous:
        return None if healthy else 'failure'
    if not healthy and previous.get('healthy', True): return 'failure'
    if not healthy and previous.get('outcome') != current['outcome']: return 'changed_failure'
    if healthy and not previous.get('healthy', True): return 'recovery'
    return None

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # GitHub artifact downloads redirect to signed object storage.
            # Never forward the GitHub credential to a different host.
            from urllib.parse import urlsplit
            if urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
                redirected.remove_header('Authorization')
        return redirected

def github(path):
    repo = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo): raise ValueError()
    req = urllib.request.Request('https://api.github.com/repos/'+repo+path, headers={
        'Authorization': 'Bearer '+os.environ['GITHUB_TOKEN'],
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    opener = urllib.request.build_opener(SafeRedirect())
    with opener.open(req, timeout=20) as response: return response.read()

def previous_state():
    data = json.loads(github('/actions/artifacts?name=huya-notification-state&per_page=100'))
    branch = os.environ.get('GITHUB_REF_NAME', 'master')
    run_id = int(os.environ['GITHUB_RUN_ID'])
    # Never consume a state artifact from another branch or this run.
    candidates = sorted([a for a in data.get('artifacts', []) if not a.get('expired')
        and a.get('workflow_run', {}).get('head_branch') == branch
        and a.get('workflow_run', {}).get('id') != run_id], key=lambda a: a['id'], reverse=True)
    if not candidates: return None
    raw = github('/actions/artifacts/'+str(candidates[0]['id'])+'/zip')
    if len(raw) > 100000: raise ValueError()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        raw = archive.read('state.json')
        if len(raw) > 4096: raise ValueError()
        value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get('healthy')) is not bool:
        raise ValueError()
    return value

def send_notice(text):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    chat = int(os.environ['TELEGRAM_CHAT_ID'])
    thread = int(os.environ['TELEGRAM_THREAD_ID'])
    body = {'chat_id': chat, 'message_thread_id': thread, 'text': text,
            'disable_web_page_preview': True}
    req = urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',
        data=json.dumps(body).encode(), headers={'Content-Type':'application/json'}, method='POST')
    # Do NOT retry an uncertain send. Never print URL/exception/body/token.
    with urllib.request.urlopen(req, timeout=25) as response: result=json.load(response)
    msg = result.get('result', {})
    if not result.get('ok') or msg.get('chat', {}).get('id') != chat or msg.get('message_thread_id') != thread or msg.get('text') != text:
        raise ValueError('Notice target/readback mismatch')
    print('NOTICE_CONFIRMED message_id='+str(msg['message_id'])+' thread='+str(thread))

def summarize(current, run_url):
    text = '\n'.join(['## 虎牙任务结果',
        '- 模式：`'+current['mode']+'`', '- 结果：`'+current['outcome']+'`',
        '- 普通虎粮库存：'+str(current['ordinary_huliang']),
        '- 已确认送出数量：'+str(current['submitted']),
        '- diagnose 不送礼；结果未知时不要盲目重跑。',
        '[运行记录]('+run_url+')', ''])
    path=os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path,'a') as out: out.write(text)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--result',default='debug_artifacts/result.json')
    parser.add_argument('--job-status',default='success', choices=['success','failure','cancelled','skipped'])
    parser.add_argument('--test-notice',action='store_true');args=parser.parse_args()
    repo=os.environ.get('GITHUB_REPOSITORY','db52/HuYa')
    run=os.environ.get('GITHUB_RUN_ID','0')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or not run.isdigit():return 1
    url='https://github.com/'+repo+'/actions/runs/'+run
    current=safe_result(args.result)
    if args.job_status != 'success' and current['outcome'] in GOOD:
        current['outcome']='WORKFLOW_FAILED'
    summarize(current,url)
    if args.test_notice:
        send_notice('虎牙 Action 通知接入测试：后续仅首次失败、原因变化及恢复提醒；正常运行静默。不发送礼物。\n'+url)
        return 0
    try:previous=previous_state()
    except Exception as exc:
        print('NOTICE_STATE_LOOKUP_FAILED category='+type(exc).__name__)
        # No fail-open alert storm or false recovery on history retrieval failure.
        return 1
    kind=notice_kind(previous,current)
    directory=Path('notification_state');directory.mkdir(exist_ok=True)
    state={'healthy':current['outcome'] in GOOD,'outcome':current['outcome'],'run_id':int(run)}
    if kind:
        titles={'failure':'虎牙任务失败','changed_failure':'虎牙失败原因变化','recovery':'虎牙任务恢复'}
        message=titles[kind]+'\n结果：'+current['outcome']+'\n模式：'+current['mode']+'\n已确认送出数量：'+str(current['submitted'])+'\n'+url
        if current['outcome']=='DIAGNOSE_OK':message+='\n登录和库存查询恢复，未验证实际送礼。'
        if current['outcome']=='SEND_FAILED_OR_UNKNOWN':message+='\n可能存在未确认提交，请勿直接重跑送礼。'
        try:send_notice(message)
        except Exception as exc:
            # Record this transition as attempted; no automatic uncertain-send retry.
            state['notice_delivery']='unconfirmed'
            print('NOTICE_UNCONFIRMED category='+type(exc).__name__)
            (directory/'state.json').write_text(json.dumps(state))
            return 1
        state['notice_delivery']='confirmed'
    else:print('NOTICE_SILENT unchanged or healthy')
    (directory/'state.json').write_text(json.dumps(state))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print('REPORT_FAILED category='+type(exc).__name__)
        raise SystemExit(1)
