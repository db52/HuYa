import ast
import unittest
from pathlib import Path
from diagnose_inventory import parse_gift_rows

class ReadOnlyTests(unittest.TestCase):
    def test_exact_two_gifts(self):
        self.assertEqual(parse_gift_rows([{'name':'虎粮','count':'35'},{'name':'超粉虎粮','count':'10'},{'name':'虎粮礼包','count':'50'}]),{'ordinary':35,'super_fans':10})
    def test_empty_not_zero(self):
        with self.assertRaises(ValueError):parse_gift_rows([])
    def test_ambiguous_not_zero(self):
        with self.assertRaises(ValueError):parse_gift_rows([{'name':'虎粮','count':'35'},{'name':'虎粮','count':'1'}])
    def test_unknown_not_zero(self):
        for count in ['999+','-1','',True]:
            with self.assertRaises(ValueError):parse_gift_rows([{'name':'虎粮','count':count}])
    def test_diagnostic_has_no_submission_or_click_calls(self):
        tree=ast.parse(Path('diagnose_inventory.py').read_text())
        calls=[n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)]
        self.assertFalse(set(calls)&{'click','send_keys','send_to_room','_submit_gift','run'})
        text=Path('.github/workflows/auto.yml').read_text()
        self.assertNotIn('schedule:',text)
        self.assertNotIn('main.py --mode',text)
        self.assertNotIn('TELEGRAM_BOT_TOKEN',text)
if __name__=='__main__':unittest.main()
