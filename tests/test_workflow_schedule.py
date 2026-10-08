import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
import re


class ScheduledWorkflowTests(unittest.TestCase):
    def test_beijing_0040_and_fixed_room_998(self):
        text = Path('.github/workflows/auto.yml').read_text()
        self.assertEqual(re.findall(r"- cron: '([^']+)'", text), ['40 16 * * *'])
        local = datetime(2026, 10, 8, 16, 40, tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
        self.assertEqual((local.hour, local.minute), (0, 40))
        self.assertIn("HUYA_ROOMS: ${{ github.event_name == 'schedule' && '998' || inputs.rooms || '998' }}", text)
        self.assertRegex(text, r"default: '998'")
        self.assertRegex(text, r"default: diagnose")
        self.assertIn('group: huya-inventory-and-gifts', text)
        self.assertIn('cancel-in-progress: false', text)
        self.assertNotIn('pull_request:', text)

    def test_pr_ci_has_no_huya_credential_or_send(self):
        text = Path('.github/workflows/test.yml').read_text()
        self.assertNotIn('secrets.', text)
        self.assertNotIn('main.py', text)
        self.assertIn('unittest discover', text)


if __name__ == '__main__':
    unittest.main()
