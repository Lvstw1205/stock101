import copy
import hashlib
import json
import math
import sqlite3
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from stock101.crypto import learning, execution_strategies
from stock101.crypto.paper_books import PaperBooks


def fill(key, side, qty, notional, fee, realized, at, sid='range'):
    return dict(id=key,product='AAA-USD',currency='CAD',side=side,quantity=str(qty),notional=str(notional),
                fee=str(fee),realized=str(realized),created=at,strategy_id=sid,reason_codes=['TEST'],
                execution_version=execution_strategies.VERSION)


def history(now, scale=1):
    end=int(now)//3600*3600
    return dict(fx_rate=scale,bars=[dict(start=end-(350-i)*3600,close=scale*(100+i*.06+math.sin(i/3))) for i in range(350)])


def book(now):
    return dict(product_id='AAA-USD',timestamp=now,mid='121',fx_rate='1',bids=[dict(price='120.9',size='100')],asks=[dict(price='121.1',size='100')])


class LearningMath(unittest.TestCase):
    def test_learning_page_asset_and_read_only_endpoint_are_registered(self):
        from stock101 import http
        from stock101.crypto import service
        self.assertEqual(http.ASSETS['/stock101/learning.js'],'learning.js')
        engine=SimpleNamespace(learning=SimpleNamespace(state=lambda:dict(status='RUNNING')))
        with patch.object(service,'engine',return_value=engine):
            self.assertEqual(service.get(None,'learning',{}),dict(status='RUNNING'))

    def test_news_keeps_provenance_and_filters_marked_ads(self):
        import email.utils
        from stock101.crypto import news
        now=time.time();stamp=email.utils.formatdate(now,usegmt=True)
        def rss(title,url):return f'<rss><channel><item><title>{title}</title><link>{url}</link><pubDate>{stamp}</pubDate></item></channel></rss>'.encode()
        self.assertEqual(news.parse(rss('Sponsored: BTC miracle','https://example.com'),'Google News search',now),[])
        row=news.parse(rss('BTC trading suspended','https://coinbase.statuspage.io/incidents/123'),'Coinbase Status',now)[0]
        self.assertEqual(row['source_class'],'OFFICIAL_STATUS');self.assertFalse(row['claim_verified'])
        row=news.parse(rss('BTC trading suspended','https://example.com'),'Google News search',now)[0]
        self.assertEqual(row['source_class'],'AGGREGATED_UNVERIFIED')

    def test_partial_exits_are_one_trial_and_include_all_costs(self):
        rows=[fill('1','BUY',2,20,.2,0,1),fill('2','SELL',1,11,.11,.79,2)]
        closed, opened=learning.round_trips(rows)
        self.assertEqual(closed,[]);self.assertEqual(opened,1)
        rows.append(fill('3','SELL',1,9,.09,-1.19,3))
        closed,opened=learning.round_trips(rows)
        self.assertEqual(len(closed),1);self.assertEqual(opened,0)
        self.assertEqual(closed[0]['net_cad'],'-0.40')
        self.assertEqual(closed[0]['fees_cad'],'0.40')
        self.assertAlmostEqual(closed[0]['net_return'],-.4/20.2)
        self.assertEqual(learning.performance(closed)['wins'],0)

    def test_mixed_strategy_episode_does_not_invent_attribution(self):
        rows=[fill('1','BUY',1,10,.1,0,1),fill('2','BUY',1,10,.1,0,2,'breakout'),fill('3','SELL',2,22,.2,1.6,3)]
        self.assertEqual(learning.round_trips(rows)[0][0]['strategy_id'],'MIXED')
        with self.assertRaisesRegex(ValueError,'UNMATCHED_SALE'):
            learning.round_trips([rows[-1]])

    def test_kelly_needs_new_complete_samples_and_both_outcomes(self):
        row=lambda r:dict(net_return=r)
        self.assertEqual(learning.kelly([row(.1)]*40)['status'],'WARMING')
        self.assertEqual(learning.kelly([row(.1)]*10+[row(-.1)]*9)['status'],'WARMING')
        measured=learning.kelly([row(.2)]*15+[row(-.02)]*5)
        self.assertEqual(measured['status'],'MEASURED');self.assertEqual(measured['cap_fraction'],.06)
        losing=learning.kelly([row(.01)]*5+[row(-.1)]*15)
        self.assertEqual(losing['cap_fraction'],0)
        self.assertLess(measured['conservative_win_rate'],measured['win_rate'])

    def test_forecast_is_frozen_before_outcome_and_fx_rebasing_is_neutral(self):
        now=1800000000;h=history(now);b=book(now)
        row=learning.forecast_record('AAA-USD',h,b,now);original=copy.deepcopy(row)
        self.assertTrue(0<row['probability']<1)
        self.assertTrue(6 <= row['horizon_hours'] <= 7)
        future=dict(fx_rate=2,bars=[dict(start=row['due_at']-3600,close=260)])
        self.assertIsNone(learning.resolve_forecast(row,future,row['due_at']-1))
        resolved=learning.resolve_forecast(row,future,row['due_at'])
        self.assertEqual(resolved['outcome'],1)
        self.assertAlmostEqual(resolved['brier'],(1-row['probability'])**2)
        self.assertEqual(row,original)
        rebased=learning.forecast_record('AAA-USD',history(now,2),b,now)
        self.assertEqual(row['probability'],rebased['probability'])
        self.assertIsNone(learning.resolve_forecast(row,dict(bars=[]),row['due_at']))

    def test_forecast_rejects_stale_future_and_indicative_inputs(self):
        now=1800000000;h=history(now)
        for invalid in [{**book(now),'timestamp':now-16},{**book(now),'executable':False}]:
            with self.assertRaisesRegex(ValueError,'QUOTE_STALE'):
                learning.forecast_record('AAA-USD',h,invalid,now)
        h['bars'][-1]['start']+=3600
        with self.assertRaisesRegex(ValueError,'HISTORY_STALE'):
            learning.forecast_record('AAA-USD',h,book(now),now)

    def test_liquidity_checks_total_holding_and_rejects_deep_price_impact(self):
        b=book(time.time());b['bids']=[dict(price='120.9',size='.2'),dict(price='110',size='100')]
        self.assertFalse(learning.exit_liquidity(b,0,100)['passed'])
        self.assertFalse(learning.exit_liquidity(b,.3,1)['passed'])
        b['bids'][1]['price']='120.8'
        result=learning.exit_liquidity(b,.3,100)
        self.assertTrue(result['passed']);self.assertGreater(float(result['required_quantity']),.3)
        self.assertLess(float(result['estimated_exit_cad']),float(result['required_quantity'])*120.9)

    def test_peek_never_fetches_or_renews_timestamp(self):
        cache=PaperBooks(lambda pid: (_ for _ in ()).throw(AssertionError('network')),clock=lambda:100)
        self.addCleanup(cache.close)
        self.assertIsNone(cache.peek('AAA-USD'))
        cache.cache['AAA-USD']=(book(100),100)
        b=cache.peek('AAA-USD');b['timestamp']=200
        self.assertEqual(cache.peek('AAA-USD')['timestamp'],100)
        cache.clock=lambda:120
        self.assertIsNone(cache.peek('AAA-USD'));self.assertFalse(cache.pending)


class LearningPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'paper.sqlite3'
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE fills(id TEXT PRIMARY KEY,created REAL,payload TEXT)')
        self.engine=SimpleNamespace(repo=SimpleNamespace(path=self.path),intelligence=None,execution_stopped=lambda:False)
        self.l=learning.Learning(self.engine)

    def add(self,rows):
        with sqlite3.connect(self.path) as c:
            for r in rows:c.execute('INSERT INTO fills VALUES(?,?,?)',(r['id'],r['created'],json.dumps(r)))

    def test_restart_is_idempotent_does_not_modify_source_or_retrofit_forecasts(self):
        now=time.time();self.l.started_at=now
        self.add([fill('1','BUY',1,10,.1,0,now-10),fill('2','SELL',1,11,.1,.8,now+1)])
        checksum=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.l.cycle(now+2);self.l.cycle(now+3)
        state=self.l.state()
        self.assertEqual(state['imported_fills'],2);self.assertEqual(state['historical']['closed_trips'],1)
        self.assertEqual(state['forward']['closed_trips'],0);self.assertEqual(state['forecasts']['total'],0)
        resumed=learning.Learning(self.engine);resumed.cycle(now+4)
        self.assertEqual(resumed.state()['historical'],state['historical'])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),checksum)
        with self.l.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM reviews WHERE id LIKE 'closed:%'").fetchone()[0],1)
            previous=None
            for row in c.execute('SELECT * FROM reviews ORDER BY seq'):
                self.assertEqual(row['previous_hash'],previous)
                self.assertEqual(row['hash'],learning.digest(dict(id=row['id'],created=row['created'],payload=json.loads(row['payload']),previous_hash=previous)))
                previous=row['hash']

    def test_sizing_only_reduces_buys_and_stale_evidence_cannot_remove_a_cap(self):
        now=time.time();self.l.cache=dict(updated_at=now,allocations={'range':dict(cap_fraction=.01,status='MEASURED')})
        signal=dict(decision='BUY',strategy_id='range',allocation=.08,plan={},sizing=dict(fraction=.08))
        applied=self.l.apply(copy.deepcopy(signal),now)
        self.assertEqual(applied['allocation'],.01)
        self.assertEqual(applied['plan']['allocation'],.01)
        self.assertEqual(applied['plan']['sizing']['fraction'],.01)
        self.assertEqual(applied['sizing']['before_learning_fraction'],.08)
        self.assertEqual(self.l.apply(copy.deepcopy(signal),now+4000)['allocation'],.01)
        signal['strategy_id']='unknown'
        self.assertEqual(self.l.apply(signal,now)['allocation'],.06)
        sell=dict(decision='SELL',allocation=.2)
        self.assertEqual(self.l.apply(sell),sell)
        self.l.cache['allocations']['range']['cap_fraction']=0
        self.assertEqual(self.l.apply(dict(decision='BUY',strategy_id='range',allocation=.05))['decision'],'WAIT')

    def test_import_pagination_handles_identical_timestamps(self):
        rows=[fill(str(i).zfill(5),'BUY',1,10,.1,0,10) for i in range(5003)]
        self.add(rows)
        with self.l.db() as c:self.l.ingest(c)
        with self.l.db() as c:
            self.l.ingest(c)
            self.assertEqual(c.execute('SELECT count(*) FROM fills').fetchone()[0],5003)

    def test_proposals_are_deduplicated_and_never_auto_deployed(self):
        made=[]
        self.l.proposal_sink=lambda spec,key:made.append((spec,key)) or dict(id='candidate')
        rows=[dict(id=str(i),strategy_id='range',product='AAA-USD',net_cad='-1',cost_cad='10',fees_cad='.1',net_return=-.1) for i in range(20)]
        self.l.propose(rows,time.time());self.l.propose(rows,time.time())
        self.assertEqual(len(made),1)
        self.assertEqual(made[0][0]['parameters'],dict(allocation_scale=.5,minimum_net_return=.01))
        with self.l.db() as c:
            review=json.loads(c.execute("SELECT payload FROM reviews WHERE id LIKE 'proposal:%'").fetchone()[0])
            self.assertEqual(review['status'],'PROPOSED_NOT_DEPLOYED')

    def test_retry_after_failure_does_not_require_browser_or_touch_orders(self):
        with patch.object(self.l,'ingest',side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):self.l.cycle()
        self.assertEqual(self.l.state()['status'],'RETRYING')
        self.l.cycle();self.assertEqual(self.l.state()['status'],'RUNNING')

    def test_background_forecasts_survive_restart_and_resolve_once_without_network(self):
        now=time.time();h=history(now);b=book(now)
        cache=PaperBooks(lambda pid: (_ for _ in ()).throw(AssertionError('network')),clock=lambda:now)
        self.addCleanup(cache.close);cache.cache['AAA-USD']=(b,now)
        self.engine.intelligence=SimpleNamespace(lock=threading.RLock(),history={'AAA-USD':h})
        self.engine.adapter=SimpleNamespace(_books=cache,_level2=None,_fx={'rate':'1'},_fx_at=now,fx_bps=25)
        self.l.cycle(now)
        s=self.l.state();self.assertEqual(s['forecasts']['pending'],1)
        original=s['forecasts']['recent'][0]['prediction'];due=original['due_at']
        self.l.cycle(now+1);self.assertEqual(self.l.state()['forecasts']['total'],1)
        self.engine.intelligence.history['AAA-USD']=dict(fx_rate=1,bars=[dict(start=due-3600,close=140)])
        self.engine.execution_stopped=lambda:True
        resumed=learning.Learning(self.engine);resumed.cycle(due+1);resumed.cycle(due+2)
        f=resumed.state()['forecasts'];self.assertEqual(f['resolved'],1);self.assertEqual(f['pending'],0)
        self.assertEqual(f['recent'][0]['prediction'],original)
        self.assertFalse(cache.pending)

    def test_old_mixed_or_version_changed_trades_cannot_train_kelly(self):
        now=time.time();self.l.started_at=now-1000
        rows=[]
        for i in range(24):
            bought=fill(f'{i:03}-a','BUY',1,10,.1,0,now-900+i*2)
            sold=fill(f'{i:03}-b','SELL',1,11,.1,.8,now-899+i*2)
            if i<20:bought.pop('execution_version')
            rows.extend([bought,sold])
        self.add(rows);self.l.cycle(now)
        self.assertEqual(self.l.state()['historical']['closed_trips'],24)
        self.assertEqual(self.l.state()['forward']['closed_trips'],4)
        self.assertEqual(self.l.state()['allocations']['range']['status'],'WARMING')

    def test_entry_liquidity_guard_is_on_paper_fill_path_before_any_ledger_write(self):
        from stock101.crypto.multi import MultiEngine
        engine=object.__new__(MultiEngine);engine.learning=self.l;engine.suspended_products={}
        b=book(time.time());b['bids'][0]['size']='0.00001'
        signal=dict(plan={})
        with self.assertRaisesRegex(ValueError,'EXIT_DEPTH_INSUFFICIENT'):
            engine.fill(None,'test',dict(product_id='AAA-USD',quote_currency_id='CAD'),b,None,'BUY',dict(approved_notional='10'),{},signal)
        self.assertFalse(signal['plan']['exit_liquidity']['passed'])


if __name__=='__main__':unittest.main()
