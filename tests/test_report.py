import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('report',Path(__file__).resolve().parents[1]/'report.py')
assert spec is not None and spec.loader is not None
report=importlib.util.module_from_spec(spec);spec.loader.exec_module(report)

class ReportTests(unittest.TestCase):
    def test_incident_transitions(self):
        good={'outcome':'DIAGNOSE_OK'};bad={'outcome':'LOGIN_REQUIRED'}
        self.assertIsNone(report.notice_kind(None,good))
        self.assertEqual(report.notice_kind(None,bad),'failure')
        failed={'healthy':False,'outcome':'LOGIN_REQUIRED'}
        self.assertIsNone(report.notice_kind(failed,bad))
        self.assertEqual(report.notice_kind(failed,good),'recovery')
        self.assertEqual(report.notice_kind(failed,{'outcome':'LOGIN_PAGE_UNAVAILABLE'}),'changed_failure')
        self.assertEqual(report.notice_kind({'healthy':True,'outcome':'EMPTY_STOCK'},bad),'failure')

    def test_result_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'result.json';p.write_text(json.dumps({'mode':'SECRET_COOKIE','outcome':'COOKIE=secret','ordinary_huliang':True,'submitted':-1,'cookie':'secret'}))
            r=report.safe_result(p)
            self.assertEqual(r,{'mode':'unknown','outcome':'WORKFLOW_FAILED','ordinary_huliang':None,'submitted':None})
            self.assertNotIn('secret',json.dumps(r))
            p.write_text('[]');self.assertEqual(report.safe_result(p)['outcome'],'WORKFLOW_FAILED')

    def test_missing_result(self):
        self.assertEqual(report.safe_result('/nonexistent/result.json')['outcome'],'WORKFLOW_FAILED')

    def test_history_scoped_to_exact_branch(self):
        a=[{'id':4,'expired':False,'workflow_run':{'head_branch':'master','id':4}},
           {'id':6,'expired':False,'workflow_run':{'head_branch':'feature','id':6}},
           {'id':5,'expired':False,'workflow_run':{'head_branch':'master','id':5}}]
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:z.writestr('state.json',json.dumps({'healthy':False,'outcome':'LOGIN_REQUIRED'}))
        with patch.dict(os.environ,{'GITHUB_REF_NAME':'master','GITHUB_RUN_ID':'5'}),patch.object(report,'github',side_effect=[json.dumps({'artifacts':a}).encode(),b.getvalue()]) as get:
            self.assertFalse(report.previous_state()['healthy'])
            self.assertEqual(get.call_args.args[0],'/actions/artifacts/4/zip')

    def test_no_fail_open_when_history_unavailable(self):
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'db52/HuYa','GITHUB_RUN_ID':'1'}),patch.object(report,'previous_state',side_effect=RuntimeError()),patch.object(report,'send_notice') as send,patch('sys.argv',['report.py']):
            self.assertEqual(report.main(),1);send.assert_not_called()

    def test_target_and_body_validated(self):
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        data={'ok':True,'result':{'message_id':10,'chat':{'id':-123},'message_thread_id':7,'text':'check'}}
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'-123','TELEGRAM_THREAD_ID':'7'}):
            with patch('urllib.request.urlopen',return_value=Response(json.dumps(data).encode())):
                report.send_notice('check')
            data['result']['message_thread_id']=8
            with patch('urllib.request.urlopen',return_value=Response(json.dumps(data).encode())):
                with self.assertRaises(ValueError):report.send_notice('check')

if __name__=='__main__':unittest.main()
