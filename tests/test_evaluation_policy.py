import tempfile
import unittest
from pathlib import Path
from stock101.crypto.repository import Repository
from stock101.crypto import evaluation_policy as policy, performance_bonus as bonus


class EvaluationPolicyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.repo = Repository(Path(folder.name)/'test.db')
        self.now = 1800000000.
        with self.repo.db() as c:
            c.execute("INSERT INTO paper_accounts VALUES('CAD','1000','1000')")
            self.repo.set(c, 'halted', False)
            self.repo.set(c, 'multi_running', True)
    def observe(self, assets, stamp=None, mode='paper'):
        with self.repo.db() as c:
            bonus.observe(self.repo, c, mode, self.now, assets, self.now if stamp is None else stamp)
            return bonus.state(self.repo, c, mode, self.now)['evaluation']
    def test_ten_percent_inclusive_after_costs(self):
        self.assertFalse(self.observe('900.00001')['breached'])
        e = self.observe('900')
        self.assertTrue(e['breached']); self.assertEqual(e['loss_ratio'], '0.1')
        self.assertEqual(e['net_profit_cad'], '-100')
    def test_unknown_or_stale_marks_cannot_prove_breach(self):
        self.assertEqual(self.observe(None)['status'], 'VALUATION_PENDING')
        e = self.observe('800', self.now-16)
        self.assertFalse(e['breached']); self.assertFalse(e['fresh'])
        self.assertTrue(self.observe('800')['breached'])
    def test_late_read_labels_snapshot_stale(self):
        self.observe('990'); self.now += 16
        with self.repo.db() as c:
            e = policy.state(self.repo, c, 'paper', self.now)
        self.assertEqual(e['status'], 'VALUATION_PENDING')
        self.assertEqual(e['net_profit_cad'], '-10')
    def test_reward_does_not_hide_trading_loss(self):
        with self.repo.db() as c:self.repo.set(c, bonus.KEY, {'earned_at': self.now-5})
        e = self.observe('920')
        self.assertEqual(e['excluded_reward_cad'], '20')
        self.assertEqual(e['net_profit_cad'], '-100'); self.assertTrue(e['breached'])
    def test_first_reward_uses_pre_credit_mark_once(self):
        with self.repo.db() as c:self.repo.put(c, 'fills', 'sale', {'realized':'200'})
        e = self.observe('1200')
        self.assertEqual(e['net_profit_cad'], '200')
        self.assertEqual(self.observe('1220')['net_profit_cad'], '200')
    def test_breach_is_latched_once_but_never_stops_paper_or_deletes_ledger(self):
        first = self.observe('899')['first_breach']
        self.now += 10
        e = self.observe('1010')
        self.assertTrue(e['breached']); self.assertEqual(e['first_breach'], first)
        with self.repo.db() as c:
            self.assertFalse(self.repo.get(c, 'halted'))
            self.assertTrue(self.repo.get(c, 'multi_running'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM system_events WHERE id=?', (policy.BREACH_KEY,)).fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT cash FROM paper_accounts').fetchone()[0], '1000')
    def test_live_has_same_benchmark_without_activation(self):
        e = self.observe('900', mode='live')
        self.assertTrue(e['breached']); self.assertFalse(e['automatic_deletion'])
        with self.repo.db() as c:self.assertFalse(self.repo.get(c, 'live_running', False))


if __name__ == '__main__': unittest.main()
