"""Real execution paths with fake markets and temporary ledgers; no broker calls."""
import datetime as dt
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from stock101.crypto import risk, execution_strategies as bundle
from stock101.crypto.multi import MultiEngine
from stock101.crypto.paper_fx import CadPaperAdapter
import test_crypto
from test_execution_strategies import RuleMarket, NOW


class LossObservation(unittest.TestCase):
    def context(self):
        return {**test_crypto.RiskTests().context(), 'daily_loss': '.04', 'drawdown': '.12',
                'consecutive_losses': 3, 'since_loss': 1}

    def test_paper_continues_after_loss_and_records_warnings(self):
        out = risk.assess('100', 'BUY', self.context(), paper_observation=True)
        self.assertEqual(out['status'], 'PASS')
        self.assertEqual(set(out['warnings']), {'PAPER_LOSS_LIMIT_OBSERVED', 'PAPER_CONSECUTIVE_LOSSES_OBSERVED'})

    def test_live_loss_controls_are_unchanged(self):
        for context, reason in ((self.context(), 'LOSS_LIMIT'),
                                ({**self.context(), 'daily_loss':'0','drawdown':'0'}, 'CONSECUTIVE_LOSSES'),
                                ({**self.context(), 'daily_loss':'0','drawdown':'0','consecutive_losses':1}, 'LOSS_COOLDOWN')):
            # Even a context supplied with a PAPER-looking key does not opt in.
            out = risk.assess('100', 'BUY', {**context, 'paper_observation':True})
            self.assertEqual(out['reason'], reason)
            self.assertIn(out['status'], ('BLOCK', 'HALT'))

    def test_observation_still_enforces_funds_data_and_integrity(self):
        for field,value in [('halted',True),('age',30),('spread_bps','100'),
                            ('signal_deviation','.04'),('trades_hour',6),
                            ('deployed_today','1000'),('asset_value','1000'),
                            ('invested','5000'),('cash','0')]:
            with self.subTest(field=field):
                out=risk.assess('100','BUY',{**self.context(),field:value},paper_observation=True)
                self.assertIn(out['status'],('BLOCK','HALT'))
        self.assertEqual(risk.assess('100','BUY',{},paper_observation=True)['reason'],'RISK_CALCULATION_FAILED')

    def test_paper_executes_after_loss_without_resetting_recorded_losses(self):
        with tempfile.TemporaryDirectory() as tmp:
            market=RuleMarket('breakout');market.now=NOW
            adapter=CadPaperAdapter(market,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()))
            with patch('time.time',side_effect=lambda:market.now):
                e=MultiEngine(Path(tmp)/'p.db',adapter,strategy_version=bundle.VERSION);e.start_multi()
                original=e.context
                def context(*args):
                    return {**original(*args),'daily_loss':'.04','drawdown':'.12',
                            'consecutive_losses':3,'since_loss':1}
                with patch.object(e,'context',side_effect=context):
                    bought=e.decide(market.pid,bundle.VERSION)
                    self.assertEqual(bought['execution']['side'],'BUY')
                    self.assertEqual(len(bought['risk']['warnings']),2)
                    market.price=80;market.now+=10
                    sold=e.decide(market.pid,bundle.VERSION)
                self.assertEqual(sold['execution']['side'],'SELL')
                self.assertLess(Decimal(sold['execution']['realized']),0)
                self.assertTrue(e.plan_state()['running'])
                with e.repo.db() as c:
                    e.reconcile_ledger(c)
                    self.assertEqual(len(e.repo.rows(c,'fills')),2)
                    self.assertEqual(c.execute('SELECT initial FROM paper_accounts').fetchone()[0],'1000')
                    self.assertLess(Decimal(e.portfolio()['portfolios'][0]['realized']),0)


if __name__=='__main__':unittest.main()
