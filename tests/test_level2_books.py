import datetime as dt
import unittest
from stock101.crypto.level2_books import Level2Books, HeldLevel2Books
from stock101.crypto.exchange import ExchangeError
from stock101.crypto.paper_fx import CadPaperAdapter


class Level2Tests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000.
        self.stream = Level2Books(clock=lambda: self.now, autostart=False)
        self.products = ['BTC-USD', 'QUIET-USD']
        self.stream.set_products(self.products)
        self.stream.begin(self.products)
        self.seq = 0
        self.send('subscriptions', [])
        self.send('heartbeats', [{'heartbeat_counter': '50'}])
        for p in self.products:
            self.book(p, 'snapshot', [('bid', '99', '20'), ('offer', '101', '20')])

    def send(self, channel, events):
        msg = {'channel': channel, 'events': events, 'sequence_num': self.seq,
               'timestamp': dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat()}
        self.seq += 1
        self.stream.ingest(msg)

    def book(self, pid, kind, levels):
        self.send('l2_data', [{'product_id': pid, 'type': kind, 'updates': [
            dict(side=s, price_level=p, new_quantity=q) for s, p, q in levels]}])

    def test_absolute_quantities_zero_removal_and_sorted_depth(self):
        self.book('QUIET-USD', 'update', [('bid', '99', '3'), ('bid', '98', '8'), ('offer', '102', '4')])
        b = self.stream.get_many(self.products)['QUIET-USD']
        self.assertEqual(b['bids'], [{'price': '99', 'size': '3'}, {'price': '98', 'size': '8'}])
        self.book('QUIET-USD', 'update', [('bid', '99', '0')])
        self.assertEqual(self.stream.get_many(self.products)['QUIET-USD']['bids'][0]['price'], '98')

    def test_quiet_book_verified_by_contiguous_stream_preserves_actual_update_time(self):
        updated = self.now
        for n in range(1, 11):
            self.now += 1
            self.send('heartbeats', [{'heartbeat_counter': str(50+n)}])
            self.book('BTC-USD', 'update', [('bid', '99', str(n))])
        b = self.stream.get_many(self.products)['QUIET-USD']
        self.assertEqual(b['timestamp'], self.now)
        self.assertEqual(b['book_updated_at'], updated)
        self.assertEqual(b['source'], 'COINBASE_LEVEL2')

    def test_heartbeat_alone_cannot_keep_stale_books_executable(self):
        self.now += 6
        self.send('heartbeats', [{'heartbeat_counter': '51'}])
        self.assertEqual(self.stream.get_many(self.products), {})

    def test_sequence_gap_discards_all_books_and_requires_new_snapshots(self):
        self.seq += 1
        with self.assertRaisesRegex(ExchangeError, 'LEVEL2_SEQUENCE_GAP'):
            self.book('BTC-USD', 'update', [('bid', '99', '4')])
        self.assertEqual(self.stream.get_many(self.products), {})
        self.stream.begin(self.products); self.seq = 0
        with self.assertRaisesRegex(ExchangeError, 'LEVEL2_SNAPSHOT_REQUIRED'):
            self.book('BTC-USD', 'update', [('bid', '99', '4')])

    def test_heartbeat_gap_invalidates_even_while_quotes_arrive(self):
        with self.assertRaisesRegex(ExchangeError, 'LEVEL2_HEARTBEAT_GAP'):
            self.send('heartbeats', [{'heartbeat_counter': '52'}])
        self.assertEqual(self.stream.get_many(self.products), {})

    def test_disconnect_and_subscription_change_invalidate_books(self):
        self.stream.set_products(self.products)
        self.assertEqual(len(self.stream.get_many(self.products)), 2)
        self.stream.set_products(self.products + ['NEW-USD'])
        self.assertEqual(self.stream.get_many(self.products), {})
        self.assertFalse(self.stream.status()['connected'])

    def test_stale_or_future_message_invalidates_instead_of_retiming(self):
        for delta in [-6, 3]:
            self.stream.begin(self.products)
            with self.assertRaisesRegex(ExchangeError, 'LEVEL2_STALE_MESSAGE'):
                self.stream.ingest(dict(channel='subscriptions', events=[], sequence_num=0,
                    timestamp=dt.datetime.fromtimestamp(self.now+delta, dt.timezone.utc).isoformat()))
            self.assertEqual(self.stream.get_many(self.products), {})

    def test_crossed_or_empty_book_not_returned(self):
        self.book('QUIET-USD', 'update', [('bid', '103', '10')])
        self.assertNotIn('QUIET-USD', self.stream.get_many(self.products))
        self.assertIn('BTC-USD', self.stream.get_many(self.products))

    def test_returned_levels_are_copies(self):
        self.stream.get_many(self.products)['BTC-USD']['bids'][0]['size'] = '999'
        self.assertEqual(self.stream.get_many(self.products)['BTC-USD']['bids'][0]['size'], '20')

    def test_pre_disconnect_book_cannot_authorize_a_fill_after_reconnect(self):
        old=self.stream.get_many(self.products)['BTC-USD']
        self.assertTrue(self.stream.is_current(old))
        self.stream.begin(self.products);self.seq=0
        self.send('heartbeats',[{'heartbeat_counter': '100'}])
        self.book('BTC-USD','snapshot',[('bid','99','20'),('offer','101','20')])
        self.assertFalse(self.stream.is_current(old))
        a=CadPaperAdapter();a._level2=self.stream
        with self.assertRaisesRegex(ExchangeError,'STALE_MARKET_DATA'):a.validate_book(old)

    def test_stream_invalidated_during_rest_wait_cannot_return_old_books(self):
        stream=self.stream
        class Rest:
            def get_many(_, products):stream.disconnect();return {}
        a=CadPaperAdapter();a._level2=stream;a._books=Rest()
        with self.assertRaisesRegex(ExchangeError,'STALE_MARKET_DATA'):a.get_orderbooks(self.products+['NEW-USD'])

    def test_adapter_uses_stream_for_holdings_and_rest_only_for_missing(self):
        calls = []
        class Rest:
            def get_many(_, products):
                calls.extend(products)
                return {p: dict(product_id=p,timestamp=self.now,mid='100',spread_bps='200',
                    bids=[dict(price='99',size='20')],asks=[dict(price='101',size='20')]) for p in products}
        a = CadPaperAdapter(fx_loader=lambda: dict(rate='1.3',date=dt.datetime.now(dt.timezone.utc).date().isoformat()))
        a._level2 = self.stream; a._books = Rest()
        books = a.get_orderbooks(self.products + ['NEW-USD'])
        self.assertEqual(calls, ['NEW-USD'])
        self.assertEqual(books['QUIET-USD']['book_updated_at'], self.now)
        self.assertEqual(books['QUIET-USD']['bids'][0]['price'], '128.37825')
        self.assertEqual(books['QUIET-USD']['timestamp'], self.now)


class HeldStreamsTests(unittest.TestCase):
    def test_bounded_stable_groups_and_removal(self):
        class Feed:
            def __init__(self): self.products=set(); self.changes=0
            def set_products(self, products):
                if self.products != products:self.changes+=1
                self.products=set(products)
            def get_many(self, products):return {p:{'product_id':p} for p in products if p in self.products}
        pool=HeldLevel2Books(factory=Feed)
        products=[f'C{i}-USD' for i in range(35)]
        pool.set_products(products)
        self.assertEqual(len(pool.groups),5)
        self.assertTrue(all(len(g['products'])<=8 for g in pool.groups))
        self.assertEqual(len(pool.get_many(products)),35)
        counts=[g['stream'].changes for g in pool.groups]
        pool.set_products(products+['NEW-USD'])
        self.assertEqual(sum(g['stream'].changes-n for g,n in zip(pool.groups,counts)),1)
        pool.set_products(['NEW-USD'])
        self.assertEqual(set(pool.get_many(products+['NEW-USD'])),{'NEW-USD'})


if __name__ == '__main__': unittest.main()
