import copy,datetime as dt,io,json,math,tempfile,time,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from stock101.crypto import execution_strategies as bundle,market_strategies as market,sizing,risk,fx_reserve,validation,validation_job
from stock101.crypto.multi import MultiEngine
from stock101.crypto.live import LiveEngine
from stock101.crypto.paper_fx import CadPaperAdapter
from test_execution_strategies import NOW,book,candles,held,entry_fill
from test_reference_strategies import series,Market,History
from test_live import Broker,FX

def scenario(sid,now=NOW):
    prices=[100+i*.3 for i in range(100)]+[136] if sid=='volume_breakout' else [100+math.sin(i*.7)*3 for i in range(100)]+[80,91]
    rows=series(prices,3600,now);rows[-1]['volume']=2000
    return rows,book(prices[-1],now)

def signal(sid,**kw):
    h,b=scenario(sid);return bundle.evaluate(h,None,b,daily=[],only_strategy=sid,**kw)

def fx_history(now=NOW):
    return dict(rows=[dict(date=dt.datetime.fromtimestamp(now-(70-i)*86400,dt.timezone.utc).date().isoformat(),rate=1.40 if i<69 else 1.32,available_at=now-(69-i)*86400) for i in range(70)])

def assets():
    h=candles(NOW,3600,300);d=candles(NOW,86400,350)
    return {'BTC-USD':dict(hourly=h,daily=d,foreign=True,reference_fx=1,base_increment='.00000001',base_min_size='.00000001',base_max_size='100000000',quote_min_size='.01',quote_max_size='100000000')}

class NewRuleTests(unittest.TestCase):
    def test_adx_flat_and_monotonic_with_prefix_invariance(self):
        self.assertEqual(market.adx(candles(NOW,3600,70)),0)
        self.assertAlmostEqual(market.adx(series(list(range(100,200)),3600)),100)
        self.assertIsNone(market.adx(candles(NOW,3600,10)))
        low,mean,high=market.bands(list(range(1,21)));self.assertEqual(mean,10.5);self.assertAlmostEqual(mean-low,high-mean)
    def test_both_nonduplicate_entries_have_cost_gate_and_durable_token(self):
        for sid in market.IDS:
            s=signal(sid);self.assertEqual(s['decision'],'BUY');self.assertEqual(s['strategy_id'],sid)
            self.assertLess(s['plan']['invalidation'],s['plan']['current']);self.assertGreater(s['plan']['target'],s['plan']['current'])
            self.assertEqual(signal(sid,fills=[entry_fill(s)])['decision'],'WAIT')
            self.assertEqual(signal(sid,fee_bps=1000)['decision'],'WAIT')
    def test_volume_is_required_and_current_unclosed_volume_does_not_authorize(self):
        h,b=scenario('volume_breakout');h[-1]['volume']=None
        out=bundle.evaluate(h,None,b,daily=[],only_strategy='volume_breakout');self.assertEqual(out['reason_codes'],['VOLUME_HISTORY_REQUIRED'])
        h,b=scenario('volume_breakout');h[-1]['volume']=100
        future=dict(h[-1],start=NOW//3600*3600,volume=100000,close=150,high=160)
        self.assertEqual(bundle.evaluate(h,None,b,daily=[],only_strategy='volume_breakout'),bundle.evaluate(h+[future],None,b,daily=[],only_strategy='volume_breakout'))
    def test_band_touch_is_not_reentry_and_trend_veto_works(self):
        h,b=scenario('bollinger_reentry')
        with patch.object(market,'adx',return_value=30):self.assertFalse(market.evaluate(h,NOW,91)[1]['entry'])
        h[-1].update(close=80,open=80,low=78,high=82);self.assertFalse(market.evaluate(h,NOW,80)[1]['entry'])
    def test_new_owners_keep_original_stop_and_protective_exit(self):
        for sid in market.IDS:
            s=signal(sid);pos=held(s);h,b=scenario(sid)
            out=bundle.evaluate(h,pos,book(s['plan']['invalidation']-.1),daily=[])
            self.assertEqual(out['strategy_id'],sid);self.assertEqual(out['reason_codes'],['STRATEGY_STOP'])

class AllocationTests(unittest.TestCase):
    def test_continuous_size_costs_volatility_and_news_reduction(self):
        h,b=scenario('volume_breakout');s=signal('volume_breakout')
        self.assertGreater(s['allocation'],.02);self.assertLessEqual(s['allocation'],.1)
        bigger=copy.deepcopy(s);bigger['plan']['net_center']='.15';sizing.apply(bigger,h,b)
        self.assertGreater(bigger['allocation'],s['allocation'])
        lower=copy.deepcopy(s);lower['plan']['net_center']='.008';sizing.apply(lower,h,b)
        self.assertLess(lower['allocation'],s['allocation'])
        news=copy.deepcopy(s);news['plan']['news']['allocation_multiplier']=.5;sizing.apply(news,h,b)
        self.assertAlmostEqual(news['allocation'],s['allocation']/2)
        self.assertLessEqual(s['allocation']*s['sizing']['estimated_stop_loss'],.005+1e-12)
    def test_same_family_does_not_inflate_agreement(self):
        h,b=scenario('volume_breakout');s=signal('volume_breakout')
        for r in s['strategy_candidates']:
            if r['strategy_id'] in ('hull','ichimoku','supertrend'):r['decision']='BUY'
        sizing.apply(s,h,b);self.assertEqual(s['sizing']['factors']['agreement'],.5)
    def test_evidence_has_exact_product_version_and_cutoff(self):
        h,b=scenario('volume_breakout');base=signal('volume_breakout')
        e=dict(product=b['product_id'],strategy_id='volume_breakout',strategy_version=bundle.VERSION,data_end=NOW-100,status='ROBUST_SAMPLE')
        good=copy.deepcopy(base);sizing.apply(good,h,b,e);self.assertGreater(good['allocation'],base['allocation'])
        for change in (dict(product='OTHER-USD'),dict(data_end=NOW+1),dict(data_end=NOW-15*86400),dict(strategy_version='old')):
            out=copy.deepcopy(base);sizing.apply(out,h,b,{**e,**change});self.assertEqual(out['allocation'],base['allocation'])
        bad=copy.deepcopy(base);sizing.apply(bad,h,b,{**e,'status':'WEAK_SAMPLE'});self.assertLess(bad['allocation'],base['allocation'])
    def test_risk_enforces_40_reserve_60_invested_10_asset_after_sizes(self):
        c=dict(equity='100',cash='45',invested='55',asset_value='0',halted=False,age=0,spread_bps=2,daily_loss=0,drawdown=0,consecutive_losses=0,since_loss=999999,trades_hour=0,deployed_today=55)
        out=risk.assess(10,'BUY',c,sizing.LIMITS);self.assertLessEqual(Decimal(out['approved_notional']),5)
        self.assertGreaterEqual(Decimal(c['cash'])-Decimal(out['approved_notional'])*Decimal('1.006'),40)
        c.update(cash='80',invested='20',asset_value='9.5',deployed_today=20)
        self.assertEqual(Decimal(risk.assess(10,'BUY',c,sizing.LIMITS)['approved_notional']),Decimal('.5'))
        c.update(cash='40',invested='60',asset_value=0);self.assertEqual(risk.assess(10,'BUY',c,sizing.LIMITS)['status'],'BLOCK')
    def test_no_stop_cannot_expand_and_dca_stays_two(self):
        h,b=scenario('volume_breakout');s=signal('volume_breakout');s['plan']['invalidation']=None;sizing.apply(s,h,b);self.assertEqual(s['allocation'],.02)
        s=bundle.evaluate(candles(NOW,3600,100),None,book(pid='BTC-USD'),daily=[])
        self.assertEqual(s['requested_cash_cad'],'2');self.assertLessEqual(s['allocation'],.02)

class FxTests(unittest.TestCase):
    def test_loader_uses_date_range_and_rejects_stale_success_response(self):
        rows=[dict(d=r['date'],FXUSDCAD={'v':str(r['rate'])}) for r in fx_history()['rows']]
        def response(req,**kw):
            self.assertIn('start_date=',req.full_url);self.assertNotIn('recent=400',req.full_url)
            return io.StringIO(json.dumps(dict(observations=rows)))
        with patch('time.time',return_value=NOW),patch.object(fx_reserve.urllib.request,'urlopen',side_effect=response):
            self.assertTrue(fx_reserve.context(fx_reserve.load(),NOW))
            for r in rows:r['d']=(dt.date.fromisoformat(r['d'])-dt.timedelta(days=365)).isoformat()
            with self.assertRaisesRegex(ValueError,'FX_HISTORY_STALE_OR_INCOMPLETE'):fx_reserve.load()
    def test_rate_direction_availability_staleness_and_peg(self):
        data=fx_history();ctx=fx_reserve.context(data,NOW);self.assertLess(ctx['discount'],-.02)
        self.assertIsNone(fx_reserve.context(data,NOW+6*86400))
        future=dict(data['rows'][-1],available_at=NOW+86400,rate=.7)
        self.assertEqual(ctx,fx_reserve.context(dict(rows=data['rows']+[future]),NOW))
        b=book(1.32,pid='USDC-USD');b.update(valuation_currency='CAD',fx_rate='1.32')
        row=fx_reserve.evaluate(b,ctx);self.assertTrue(row['entry']);self.assertGreater(row['target'],float(b['mid']))
        b['mid']='1.25';self.assertEqual(fx_reserve.evaluate(b,ctx)['reason'],'USDC_PEG_DEVIATION')
    def test_usd_funding_is_not_cad_conversion_and_wrong_assets_cannot_use_it(self):
        ctx=fx_reserve.context(fx_history(),NOW)
        self.assertEqual(fx_reserve.evaluate(book(1,pid='USDC-USD'),ctx)['reason'],'CAD_FUNDING_ROUTE_REQUIRED')
        self.assertEqual(fx_reserve.evaluate(book(1.32,pid='ALT-CAD'),ctx)['reason'],'USDC_ONLY')
        self.assertEqual(fx_reserve.evaluate(book(1.32,pid='USDC-CAD'),ctx)['reason'],'USDC_PRICE_REFERENCE_REQUIRED')
        ctx['usdc_quote']=dict(price=1,checked_at=NOW)
        self.assertTrue(fx_reserve.evaluate(book(1.32,pid='USDC-CAD'),ctx)['entry'])
        ctx['usdc_quote']['checked_at']=NOW-121
        self.assertEqual(fx_reserve.evaluate(book(1.32,pid='USDC-CAD'),ctx)['reason'],'USDC_PRICE_REFERENCE_REQUIRED')
    def test_fx_bundle_costs_token_and_stop(self):
        ctx=fx_reserve.context(fx_history(),NOW);b=book(1.32,pid='USDC-USD');b.update(valuation_currency='CAD',fx_rate='1.32')
        h=candles(NOW,3600,100,1.32,.001)
        s=bundle.evaluate(h,None,b,daily=[],fx_context=ctx)
        self.assertEqual(s['strategy_id'],'fx_usdc');self.assertEqual(s['decision'],'BUY')
        f=dict(product='USDC-USD',side='BUY',quantity=1,strategy_token=s['plan']['entry_token'],created=NOW)
        self.assertEqual(bundle.evaluate(h,None,b,daily=[],fx_context=ctx,fills=[f])['decision'],'WAIT')
        self.assertEqual(bundle.evaluate(h,None,b,daily=[],fx_context=ctx,fee_bps=1000)['decision'],'WAIT')
        pos=held(s);b['mid']=str(s['plan']['invalidation']-.001)
        self.assertEqual(bundle.evaluate(h,pos,b,daily=[],fx_context=None)['reason_codes'],['STRATEGY_STOP'])

class ValidationTests(unittest.TestCase):
    def test_holdouts_are_disjoint_ordered_and_cannot_use_warmup_for_fills(self):
        a=assets();folds=validation.folds_for(a)
        self.assertEqual(len(folds),3);self.assertEqual(len(set(sum(folds,[]))),99)
        self.assertEqual(folds[0][0],a['BTC-USD']['hourly'][201]['start'])
        out=validation.simulate(a,folds[0],validation.SCENARIOS[0],strategy_id='dca_weekly',trace=True)
        self.assertTrue(out['trades']);self.assertTrue(all(t['created']>=folds[0][0] for t in out['trades']))
        self.assertEqual(out['closed_exits'],0);self.assertLess(out['return_ratio'],0);self.assertGreater(out['fees'],0)
    def test_higher_costs_reduce_flat_dca_net_valuation(self):
        a=assets();times=validation.folds_for(a)[0]
        basic=validation.simulate(a,times,validation.SCENARIOS[0],strategy_id='dca_weekly')
        costly=validation.simulate(a,times,{**validation.SCENARIOS[0],'fee_bps':80},strategy_id='dca_weekly')
        self.assertLess(costly['return_ratio'],basic['return_ratio'])
        gated=validation.simulate(a,times,validation.SCENARIOS[2],strategy_id='dca_weekly')
        self.assertEqual(gated['fills'],0);self.assertEqual(gated['return_ratio'],0)
    def test_future_candles_and_daily_future_are_not_read_by_decisions(self):
        a=assets();times=validation.folds_for(a)[0][:2];seen=[];original=bundle.evaluate
        def spy(h,p,b,**kw):
            self.assertTrue(all(r['start']+3600<=b['timestamp'] for r in h))
            self.assertTrue(all(r['start']+86400<=b['timestamp'] for r in kw['daily']))
            seen.append(b['timestamp']);return original(h,p,b,**kw)
        with patch.object(bundle,'evaluate',side_effect=spy):out=validation.simulate(a,times,validation.SCENARIOS[0],strategy_id='dca_weekly',trace=True)
        changed=copy.deepcopy(a)
        for r in changed['BTC-USD']['hourly']:
            if r['start']>=times[-1]+3600:r.update(open=1,high=10000,low=1,close=1)
        self.assertEqual(out,validation.simulate(changed,times,validation.SCENARIOS[0],strategy_id='dca_weekly',trace=True));self.assertEqual(seen,times)
    def test_missing_gap_bad_volume_and_short_history_fail_without_fabrication(self):
        for kind in ('gap','volume','price','short'):
            a=assets();rows=a['BTC-USD']['hourly']
            if kind=='gap':del rows[250]
            if kind=='volume':rows[250]['volume']=float('nan')
            if kind=='price':rows[250]['low']=200
            if kind=='short':a['BTC-USD']['hourly']=rows[:250]
            with self.assertRaises(ValueError):validation.folds_for(a)
    def test_block_bootstrap_reproducible_and_constant_loss(self):
        out=validation.monte_carlo([-.001]*100)
        self.assertEqual(out,validation.monte_carlo([-.001]*100));self.assertEqual(out['loss_fraction'],1)
        self.assertAlmostEqual(out['return_p05'],.999**100-1)
        self.assertEqual(validation.monte_carlo([0]*10)['status'],'INSUFFICIENT_SAMPLE')
    def test_stale_wrong_version_report_never_supplies_sizing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=dict(code_hash='old',strategy_version=bundle.VERSION,products=['BTC-USD'],entries=[])
            validation_job.atomic(Path(tmp)/'validation/report.json',r)
            self.assertEqual(validation_job.evidence_for(tmp,'BTC-USD'),{})
            self.assertTrue(validation_job.state(tmp)['result']['outdated'])

class ExtendedMarket(Market):
    def get_orderbook(self,pid):return book(self.price or float(scenario(self.sid,self.now)[1]['mid']),self.now,pid)
    def get_candles(self,pid):return scenario(self.sid,self.now)[0]
    def get_daily_candles(self,pid):return []

class ExecutionTests(unittest.TestCase):
    def test_fx_short_history_paper_and_cad_funded_fake_live(self):
        class FxMarket(Market):
            def get_products(self):return [self.get_product('USDC-CAD')]
            def get_product(self,pid):return {**super().get_product(pid),'quote_currency_id':'CAD','quote_min_size':'.01'}
            def get_orderbook(self,pid):return book(self.price or 1.32,self.now,pid)
            def get_candles(self,pid):return candles(self.now,3600,7,1.32,.001)
            def get_daily_candles(self,pid):return candles(self.now,86400,217,1.32,.001)
        class CadBroker(Broker):
            def place_order(self,payload):
                old=Decimal(self.accounts['USD']['available']);r=super().place_order(payload)
                delta=Decimal(self.accounts['USD']['available'])-old
                self.accounts['CAD']['available']=str(Decimal(self.accounts['CAD']['available'])+delta)
                self.accounts['USD']['available']=str(old);return r
        for mode in ('paper','live'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp,patch('time.time',return_value=NOW):
                m=FxMarket('fx_usdc');br=CadBroker(m);br.accounts['CAD']=dict(available='1000',held='0')
                adapter=CadPaperAdapter(m)
                if mode=='paper':e=MultiEngine(Path(tmp)/'p.db',adapter,strategy_version=bundle.VERSION);e.start_multi()
                else:
                    e=LiveEngine(Path(tmp)/'l.db',br,m,FX(),clock=lambda:m.now,strategy_version=bundle.VERSION)
                    e.configure(dict(quote='CAD',fx_cost_bps=25,costs_confirmed=True,enable_live=True));self.assertEqual(e.config()['fx_cost_bps'],'0')
                    e.activate(dict(confirmation='실제 돈으로 자동매매 시작',confirmed=True))
                e.intelligence=History(adapter if mode=='paper' else m)
                e.intelligence.fx_context=lambda now:{**fx_reserve.context(fx_history(now),now),'usdc_quote':dict(price=1,checked_at=now)}
                e.cycle()
                if mode=='live':self.assertTrue(e.reconcile())
                with e.repo.db() as c:
                    fills=e.repo.rows(c,'fills');self.assertEqual(len(fills),1,e.repo.rows(c,'trade_decisions'));self.assertEqual(fills[0]['strategy_id'],'fx_usdc')
                    if mode=='paper':e.reconcile_ledger(c)
                if mode=='live':self.assertTrue(e.audit_ledger())
    def test_new_rules_paper_and_fake_live_execute_with_flexible_size_and_ledger(self):
        for sid in market.IDS:
            for mode in ('paper','live'):
                with self.subTest(sid=sid,mode=mode),tempfile.TemporaryDirectory() as tmp,patch('time.time',return_value=NOW):
                    m=ExtendedMarket(sid);adapter=CadPaperAdapter(m,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()));broker=Broker(m)
                    if mode=='paper':
                        e=MultiEngine(Path(tmp)/'p.db',adapter,strategy_version=bundle.VERSION);e.intelligence=History(adapter);e.start_multi();e.cycle()
                    else:
                        e=LiveEngine(Path(tmp)/'l.db',broker,m,FX(),clock=lambda:m.now,strategy_version=bundle.VERSION);e.intelligence=History(m)
                        e.configure(dict(quote='USD',costs_confirmed=True,enable_live=True));e.activate(dict(confirmation='실제 돈으로 자동매매 시작',confirmed=True));e.cycle();self.assertTrue(e.reconcile())
                    with e.repo.db() as c:
                        fills=e.repo.rows(c,'fills');self.assertEqual(len(fills),1,e.repo.rows(c,'trade_decisions'));self.assertEqual(fills[0]['strategy_id'],sid)
                        pos=e.repo.one(c,'positions','ALT-USD');self.assertIn('sizing',pos['entry_plan'])
                        if mode=='paper':e.reconcile_ledger(c)
                    if mode=='live':self.assertTrue(e.audit_ledger())
    def test_custom_live_limits_only_tighten_new_user_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=ExtendedMarket('volume_breakout');e=LiveEngine(Path(tmp)/'l.db',Broker(m),m,FX(),strategy_version=bundle.VERSION)
            for values in ({'cash_reserve':'.39'},{'single_order':'.11'},{'daily_deployment':'.61'}):
                with self.assertRaisesRegex(ValueError,'RISK_LIMIT_CANNOT_BE_WEAKENED'):e.configure(dict(quote='USD',costs_confirmed=True,enable_live=True,risk_limits=values))
            e.configure(dict(quote='USD',costs_confirmed=True,enable_live=True,risk_limits={'cash_reserve':'.50','single_order':'.03'}))
            self.assertEqual(e.config()['risk_limits']['single_order'],'0.03');self.assertFalse(e.enabled())

if __name__=='__main__':unittest.main()
