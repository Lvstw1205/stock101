import threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from stock101.crypto.paper_books import PaperBooks
from stock101.crypto.exchange import ExchangeError
import test_paper_recovery as recovery_fixtures
from stock101.crypto import planner,service


def book(pid,stamp):
    return {'product_id':pid,'timestamp':stamp,'received_at':stamp,'mid':'100','spread_bps':'10',
            'bids':[{'price':'99.95','size':'10'}],'asks':[{'price':'100.05','size':'10'}]}


class BookCacheTests(unittest.TestCase):
    def make(self,loader,**kw):
        cache=PaperBooks(loader,**kw);self.addCleanup(cache.close);return cache

    def test_concurrent_readers_share_request_and_cannot_mutate_cached_depth(self):
        started=threading.Event();release=threading.Event();calls=[]
        def load(pid):
            calls.append(pid);started.set();release.wait(2);return book(pid,time.time())
        cache=self.make(load)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(cache.get,'BTC-USD');self.assertTrue(started.wait(1))
            second=pool.submit(cache.get,'BTC-USD');release.set()
            a=first.result();b=second.result()
        self.assertEqual(calls,['BTC-USD']);a['bids'][0]['price']='1'
        self.assertEqual(b['bids'][0]['price'],'99.95')
        self.assertEqual(cache.get('BTC-USD')['bids'][0]['price'],'99.95')

    def test_expired_book_is_refetched_without_changing_exchange_timestamp(self):
        now=[100.];calls=[]
        def load(pid):calls.append(pid);return book(pid,now[0]-1)
        cache=self.make(load,clock=lambda:now[0])
        self.assertEqual(cache.get('BTC-USD')['timestamp'],99)
        now[0]=109;self.assertEqual(cache.get('BTC-USD')['timestamp'],99)
        now[0]=111;self.assertEqual(cache.get('BTC-USD')['timestamp'],110)
        self.assertEqual(len(calls),2)

    def test_stale_response_never_replaces_a_valid_execution_quote(self):
        now=[100.];stale=[False]
        cache=self.make(lambda p:book(p,now[0]-(30 if stale[0] else 0)),clock=lambda:now[0])
        cache.get('BTC-USD');now[0]=111;stale[0]=True
        with self.assertRaises(ExchangeError):cache.get('BTC-USD')
        now[0]=120;stale[0]=False
        self.assertEqual(cache.get('BTC-USD')['timestamp'],120)

    def test_future_dated_quote_is_rejected(self):
        cache=self.make(lambda p:book(p,110),clock=lambda:100)
        with self.assertRaises(ExchangeError):cache.get('BTC-USD')

    def test_a_slow_asset_does_not_block_unrelated_book_read(self):
        release=threading.Event();started=threading.Event()
        def load(pid):
            if pid=='SLOW-USD':started.set();release.wait(2)
            return book(pid,time.time())
        cache=self.make(load,timeout=.02)
        try:
            with self.assertRaisesRegex(ExchangeError,'MARKET_DATA_REFRESH_PENDING'):cache.get_many(['SLOW-USD','FAST-USD'])
            self.assertTrue(started.is_set());self.assertEqual(cache.get('FAST-USD')['product_id'],'FAST-USD')
        finally:release.set()

    def test_many_holdings_use_a_bounded_pool_and_warm_reads_do_not_refetch(self):
        barrier=threading.Barrier(8);calls=[];lock=threading.Lock();active=[0];peak=[0]
        def load(pid):
            with lock:calls.append(pid);active[0]+=1;peak[0]=max(peak[0],active[0])
            barrier.wait(timeout=2)
            with lock:active[0]-=1
            return book(pid,time.time())
        cache=self.make(load)
        products=[f'C{i}-USD' for i in range(24)]
        self.assertEqual(len(cache.get_many(products)),24)
        self.assertEqual(len(cache.get_many(products)),24)
        self.assertEqual(len(calls),24);self.assertLessEqual(peak[0],8)

    def test_transient_failures_back_off_then_retry_instead_of_reusing_old_price(self):
        now=[100.];calls=[]
        def load(pid):
            calls.append(pid)
            if len(calls)==1:raise ExchangeError('EXCHANGE_UNAVAILABLE')
            return book(pid,now[0])
        cache=self.make(load,clock=lambda:now[0])
        for _ in range(2):
            with self.assertRaisesRegex(ExchangeError,'EXCHANGE_UNAVAILABLE') as raised:cache.get('BTC-USD')
            self.assertEqual(raised.exception.products,['BTC-USD'])
        self.assertEqual(len(calls),1);now[0]=103
        self.assertEqual(cache.get('BTC-USD')['timestamp'],103)

    def test_programming_errors_remain_visible(self):
        def broken(pid):raise TypeError('bad book implementation')
        cache=self.make(broken)
        with self.assertRaises(TypeError):cache.get('BTC-USD')


class HeldMarketRetryTests(unittest.TestCase):
    # Reuse only the fixture, not the suite's test methods.
    setUp=recovery_fixtures.RecoveryTests.setUp
    tearDown=recovery_fixtures.RecoveryTests.tearDown

    def test_held_asset_network_outage_retries_without_disarming_or_duplicating(self):
        self.e.decide('AAA-USD',planner.VERSION);before=self.e.plan_state()['trade_count']
        with patch.object(self.adapter,'get_orderbook',side_effect=ExchangeError('EXCHANGE_UNAVAILABLE')):
            result=self.e.decide('AAA-USD',planner.VERSION)
        self.assertEqual(result['status'],'MARKET_DATA_UNAVAILABLE')
        self.assertTrue(self.e.plan_state()['running']);self.assertFalse(self.e._fatal)
        self.assertEqual(self.e.plan_state()['trade_count'],before)
        self.assertEqual(self.e.plan_state()['activity']['market_wait']['reason'],'EXCHANGE_UNAVAILABLE')
        self.market.factor=1.1
        result=self.e.decide('AAA-USD',planner.VERSION)
        self.assertEqual(result['execution']['side'],'SELL')
        with self.e.repo.db() as c:self.e.reconcile_ledger(c)

    def test_slow_holdings_refresh_does_not_stop_worker_or_create_fill(self):
        self.e.decide('AAA-USD',planner.VERSION);before=self.e.plan_state()['trade_count']
        with patch.object(self.e,'books_for',side_effect=ExchangeError('MARKET_DATA_REFRESH_PENDING')):
            service._paper_cycle(self.e)
        after=self.e.plan_state()
        self.assertTrue(after['running']);self.assertEqual(after['trade_count'],before)
        self.assertIsNotNone(after['activity']['last_progress_at'])

    def test_transient_retry_does_not_mask_failure_after_execution_started(self):
        with patch.object(self.e,'evaluate_signal',side_effect=ExchangeError('EXCHANGE_UNAVAILABLE')):
            with self.assertRaises(ExchangeError):self.e.decide('AAA-USD',planner.VERSION)
        self.assertTrue(self.e._fatal);self.assertFalse(self.e.plan_state()['running'])

    def test_ui_fallback_is_labeled_stale_and_never_used_to_execute(self):
        self.e.decide('AAA-USD',planner.VERSION);before=self.e.plan_state()['trade_count']
        fresh=self.e.portfolio()['portfolios'][0];self.assertEqual(fresh['mark_status'],'FRESH')
        with self.e.repo.db() as c:snapshots=len(self.e.repo.rows(c,'portfolio_snapshots',10000))
        with patch.object(self.adapter,'get_orderbook',side_effect=ExchangeError('EXCHANGE_UNAVAILABLE')):
            stale=self.e.portfolio()['portfolios'][0]
            result=self.e.decide('AAA-USD',planner.VERSION)
        self.assertEqual(stale['mark_status'],'STALE');self.assertLessEqual(stale['mark_as_of'],time.time())
        self.assertEqual(stale['equity'],fresh['equity']);self.assertEqual(result['status'],'MARKET_DATA_UNAVAILABLE')
        self.assertEqual(self.e.plan_state()['trade_count'],before);self.assertTrue(self.e.plan_state()['running'])
        with self.e.repo.db() as c:
            self.assertEqual(len(self.e.repo.rows(c,'portfolio_snapshots',10000)),snapshots)
            self.e.reconcile_ledger(c)

if __name__=='__main__':unittest.main()
