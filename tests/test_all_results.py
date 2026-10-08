import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import report


class AllResultsTests(unittest.TestCase):
    def call(self, outcome, previous=None, raises=None, job='success'):
        with tempfile.TemporaryDirectory() as d:
            original=os.getcwd()
            try:
                os.chdir(d)
                Path('result.json').write_text(json.dumps({'mode':'send','outcome':outcome,'ordinary_huliang':0,'submitted':0,'super_fans_huliang':0,'super_fans_submitted':0}))
                with patch.dict(os.environ, {'GITHUB_REPOSITORY':'db52/HuYa','GITHUB_RUN_ID':'42'}), patch.object(report,'previous_state',return_value=previous), patch.object(report,'send_notice',side_effect=raises) as send, patch('sys.argv',['report.py','--all-results','--result','result.json','--job-status',job]):
                    code=report.main()
                    state=json.loads(Path('notification_state/state.json').read_text()) if Path('notification_state/state.json').exists() else None
                    return code,send.call_count,send.call_args,state
            finally: os.chdir(original)

    def test_all_send_outcomes_notify_once(self):
        for outcome in ['EMPTY_STOCK','SENT_INVENTORY_VERIFIED','LOGIN_REQUIRED','SEND_FAILED_OR_UNKNOWN']:
            with self.subTest(outcome=outcome):
                code,n,args,state=self.call(outcome,{'healthy':True,'outcome':outcome,'run_id':41})
                self.assertEqual((code,n),(0,1))
                assert state is not None
                self.assertEqual(state['notice_delivery'],'confirmed')
                self.assertIn('超粉虎粮',args.args[0])
                self.assertIn('998',args.args[0])

    def test_unknown_notice_not_retried(self):
        code,n,args,state=self.call('EMPTY_STOCK',raises=TimeoutError())
        self.assertEqual((code,n),(1,1))
        assert state is not None
        self.assertEqual(state['notice_delivery'],'unconfirmed')

    def test_same_run_already_attempted_silent(self):
        for delivery in ['confirmed','unconfirmed']:
            code,n,args,state=self.call('EMPTY_STOCK',{'healthy':True,'outcome':'EMPTY_STOCK','run_id':42,'notice_delivery':delivery})
            self.assertEqual((code,n),(0,0))

    def test_failure_before_result_still_reports(self):
        code,n,args,state=self.call('EMPTY_STOCK',job='failure')
        self.assertEqual((code,n),(0,1))
        self.assertIn('WORKFLOW_FAILED',args.args[0])

    def test_room_metadata_validated(self):
        current=report.safe_result('/nonexistent/result.json')
        with patch.dict(os.environ,{'HUYA_REPORT_ROOMS':'123,998'}):
            self.assertIn('赠送目标：123,998',report.result_message(current,'https://example.com'))
        with patch.dict(os.environ,{'HUYA_REPORT_ROOMS':'cookie=secret'}):
            self.assertNotIn('secret',report.result_message(current,'https://example.com'))

if __name__=='__main__': unittest.main()
