import time, unittest, urllib.error
from unittest.mock import patch
from decimal import Decimal
import test_paper_recovery as recovery_fixtures
from stock101.crypto import planner, service
from stock101.crypto.exchange import CoinbaseAdapter, ExchangeError


class SuspendedMarkets(unittest.TestCase):
    def setUp(self):
        recovery_fixtures.RecoveryTests.setUp(self)
        for pid in ('AAA-USD', 'BBB-USD'):
            result = self.e.decide(pid, planner.VERSION)
            self.assertEqual(result['execution']['side'], 'BUY')
        self.e.portfolio()
        self.original_product = self.adapter.get_product
        self.listed = [p for p in self.adapter.get_products() if p['product_id'] != 'AAA-USD']
        self.e.products = self.listed; self.e._products_at = time.time()
        self.products_patch = patch.object(self.adapter, 'get_products', return_value=self.listed)
        self.products_patch.start()
        def product(pid):
            if pid == 'AAA-USD':
                raise ExchangeError('PRODUCT_NOT_FOUND')
            return self.original_product(pid)
        self.product_patch = patch.object(self.adapter, 'get_product', side_effect=product)
        self.product_patch.start()

    def tearDown(self):
        self.product_patch.stop(); self.products_patch.stop()
        recovery_fixtures.RecoveryTests.tearDown(self)

    def test_missing_market_preserves_holdings_and_marks_them_indicative(self):
        with self.e.repo.db() as c: before = self.e.repo.one(c, 'positions', 'AAA-USD')
        portfolio = self.e.portfolio()['portfolios'][0]
        self.assertEqual(portfolio['mark_status'], 'PARTIAL')
        self.assertEqual(portfolio['unavailable_products'], ['AAA-USD'])
        self.assertLessEqual(portfolio['mark_as_of'], portfolio['fresh_mark_as_of'])
        frozen = next(p for p in portfolio['positions'] if p['product'] == 'AAA-USD')
        self.assertFalse(frozen['executable']); self.assertEqual(frozen['mark_status'], 'STALE')
        with self.e.repo.db() as c:
            self.e.reconcile_ledger(c)
            self.assertEqual(before, self.e.repo.one(c, 'positions', 'AAA-USD'))
        self.assertFalse(self.e.plan_state()['entry_enabled'])

    def test_confirmed_removal_does_not_block_recovery_or_fresh_asset_sale(self):
        self.e.fail('PRODUCT_DISCOVERY_FAILED'); self.e.recovery['next_retry_at'] = 0
        self.e.recover_if_due()
        self.assertTrue(self.e.plan_state()['running'])
        self.market.factor = 1.1
        result = self.e.decide('BBB-USD', planner.VERSION)
        self.assertEqual(result['execution']['side'], 'SELL')
        self.assertEqual(result['portfolio_after']['mark_status'], 'PARTIAL')
        with self.e.repo.db() as c:
            self.e.reconcile_ledger(c)
            self.assertGreater(Decimal(self.e.repo.one(c, 'positions', 'AAA-USD')['quantity']), 0)

    def test_missing_price_cannot_fund_new_buy_or_execute_its_own_sale(self):
        self.e.books_for('CAD')
        with self.assertRaisesRegex(ValueError, 'SUSPENDED_ASSET_ENTRY_HOLD'):
            self.e.fill(None, None, {'product_id':'BBB-USD'}, {}, None, 'BUY', {}, {}, {})
        with self.assertRaisesRegex(ValueError, 'PRODUCT_NOT_FOUND'):
            self.e.fill(None, None, {'product_id':'AAA-USD'}, {}, None, 'SELL', {}, {}, {})

    def test_temporary_network_error_is_not_treated_as_delisting(self):
        with patch.object(self.adapter, 'get_product', side_effect=ExchangeError('EXCHANGE_UNAVAILABLE')):
            with self.assertRaisesRegex(ExchangeError, 'EXCHANGE_UNAVAILABLE'):
                self.e.books_for('CAD')
        self.assertFalse(self.e.suspended_products)

    def test_original_market_returning_clears_restriction_without_rewriting_fills(self):
        self.e.books_for('CAD')
        self.product_patch.stop(); self.products_patch.stop()
        self.e.products = self.adapter.get_products()
        portfolio = self.e.portfolio()['portfolios'][0]
        self.assertEqual(portfolio['mark_status'], 'FRESH')
        self.assertFalse(self.e.suspended_products)
        self.assertTrue(self.e.plan_state()['entry_enabled'])
        with self.e.repo.db() as c: self.e.reconcile_ledger(c)

    def test_restart_retains_original_mark_time_not_new_snapshot_time(self):
        first = self.e.portfolio()['portfolios'][0]
        original = self.e.suspended_products['AAA-USD']['last_mark_at']
        self.e.suspended_products.clear()
        self.e.portfolio()
        self.assertEqual(self.e.suspended_products['AAA-USD']['last_mark_at'], original)

    def test_absent_historical_mark_cannot_be_fabricated(self):
        with self.e.repo.db() as c: c.execute('DELETE FROM portfolio_snapshots')
        with self.assertRaisesRegex(ExchangeError, 'UNAVAILABLE_HOLDING_MARK_MISSING'):
            self.e.books_for('CAD')


class PublicNotFound(unittest.TestCase):
    def test_only_market_404_is_product_not_found(self):
        def transport(*args):
            raise urllib.error.HTTPError('https://api.coinbase.com', 404, 'Not Found', {}, None)
        adapter = CoinbaseAdapter(transport=transport)
        for path in ('/market/products/IP-USD', '/market/product_book'):
            with self.assertRaisesRegex(ExchangeError, 'PRODUCT_NOT_FOUND'):
                adapter.request(path)
        with self.assertRaisesRegex(ExchangeError, 'EXCHANGE_UNAVAILABLE'):
            adapter.request('/accounts')

