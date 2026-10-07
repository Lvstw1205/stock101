import copy,datetime as dt,tempfile,time,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from stock101.crypto.live import LiveEngine
from stock101.crypto.live_exchange import LiveAdapter
from stock101.crypto.exchange import ExchangeError,number
from test_paper_execution import Markets
class FX:
 def fx(self):return {'rate':'1.37','date':dt.datetime.now(dt.timezone.utc).date().isoformat()}
class Broker:
 def __init__(self,market):
  self.market=market;self.sent=[];self.orders={};self.accounts={'USD':{'available':'1000','held':'0'}};self.trade=True;self.preview_hook=None;self.timeout=False
 def permissions(self,require_trade=False):
  if require_trade and not self.trade:raise ValueError('TRADE_PERMISSION_REQUIRED')
  return {'view':True,'trade':self.trade,'transfer':False}
 def fees(self):return {'maker':'.0025','taker':'.005','checked_at':time.time()}
 def balances(self):return copy.deepcopy(self.accounts)
 def trading_products(self):return [self.trading_product(p['product_id']) for p in self.market.get_products()]
 def trading_product(self,pid):return {**self.market.get_product(pid),'price_increment':'.01','alias':pid}
 def preview(self,payload):
  if self.preview_hook:self.preview_hook()
  p=payload['order_configuration']['sor_limit_ioc'];qty=number(p['base_size']);v=qty*number(p['limit_price'])
  return {'errs':[],'warning':[],'base_size':str(qty),'commission_total':str(v*Decimal('.005')),'preview_id':'preview-test'}
 def place_order(self,payload):
  if not self.permit('submit'):raise ValueError('LOCAL_LIVE_ACTIVATION_REQUIRED')
  self.sent.append(copy.deepcopy(payload));p=payload['order_configuration']['sor_limit_ioc'];qty=number(p['base_size']);v=qty*number(p['limit_price']);fee=v*Decimal('.005');base=payload['product_id'].split('-')[0];side=payload['side']
  self.accounts.setdefault(base,{'available':'0','held':'0'})
  self.accounts[base]['available']=str(number(self.accounts[base]['available'])+(qty if side=='BUY' else -qty))
  self.accounts['USD']['available']=str(number(self.accounts['USD']['available'])+(-v-fee if side=='BUY' else v-fee))
  order={'order_id':payload['client_order_id'],'client_order_id':payload['client_order_id'],'product_id':payload['product_id'],'side':side,'status':'FILLED','settled':True,'filled_size':str(qty),'filled_value':str(v),'total_fees':str(fee)};self.orders[order['order_id']]=order
  if self.timeout:raise TimeoutError('ambiguous response')
  return {'success':True,'success_response':{'order_id':order['order_id']}}
 def get_order(self,oid):return self.orders.get(oid)
 def find_order(self,cid,start):return next((x for x in self.orders.values() if x['client_order_id']==cid),None)
 def cancel_order(self,oid):return {'results':[{'order_id':oid,'success':True}]}
class LiveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'live.db';self.market=Markets();self.broker=Broker(self.market);self.e=LiveEngine(self.path,self.broker,self.market,FX())
 def tearDown(self):self.tmp.cleanup()
 def configure(self):return self.e.configure({'quote':'USD','minimum_net':'.005','fx_cost_bps':'25','costs_confirmed':True,'enable_live':True})
 def arm(self):self.configure();return self.e.activate({'confirmation':'실제 돈으로 자동매매 시작','confirmed':True})
 def test_default_no_orders(self):
  self.e.cycle();self.assertEqual(self.broker.sent,[])
  with self.assertRaises(ValueError):self.e.activate({'confirmed':True})
 def test_view_only_or_no_funds_cannot_start(self):
  self.configure();self.broker.trade=False
  self.assertFalse(self.e.readiness()['ready'])
  with self.assertRaises(ValueError):self.e.activate({'confirmation':'실제 돈으로 자동매매 시작','confirmed':True})
  self.broker.trade=True;self.broker.accounts['USD']['available']='0';self.assertFalse(self.e.readiness()['ready'])
 def test_limit_order_reconciles_once_with_fees(self):
  self.arm();self.e.cycle();self.assertEqual(len(self.broker.sent),1)
  self.assertEqual(set(self.broker.sent[0]['order_configuration']),{'sor_limit_ioc'})
  self.e.reconcile();self.e.reconcile()
  with self.e.repo.db() as c:
   fills=self.e.repo.rows(c,'fills');self.assertEqual(len(fills),1);self.assertGreater(number(fills[0]['fee']),0)
   self.assertLessEqual((number(fills[0]['notional'])+number(fills[0]['fee']))*Decimal('1.37'),20)
  self.assertFalse(self.e.pending())
 def test_timeout_never_resubmits_and_restart_recovers(self):
  self.arm();self.broker.timeout=True;self.e.cycle();self.assertFalse(self.e.enabled());self.assertEqual(len(self.broker.sent),1)
  restarted=LiveEngine(self.path,self.broker,self.market,FX());restarted.cycle();restarted.cycle()
  self.assertFalse(restarted.enabled());self.assertEqual(len(self.broker.sent),1)
  with restarted.repo.db() as c:self.assertEqual(len(restarted.repo.rows(c,'fills')),1)
 def test_unknown_order_remains_reserved(self):
  self.arm();self.broker.timeout=True;self.e.cycle();self.broker.orders={};self.assertFalse(self.e.reconcile());self.assertTrue(self.e.pending())
  self.e.cycle();self.assertEqual(len(self.broker.sent),1)
  with self.assertRaises(ValueError):self.configure()
 def test_stop_during_preview_cannot_submit(self):
  self.arm();self.broker.preview_hook=self.e.stop;self.e.cycle();self.assertEqual(self.broker.sent,[]);self.assertFalse(self.e.enabled())
 def test_preview_fee_spike_blocks(self):
  self.arm();original=self.broker.preview
  def high(payload):return {**original(payload),'commission_total':'10'}
  with patch.object(self.broker,'preview',side_effect=high):self.e.cycle()
  self.assertEqual(self.broker.sent,[])
 def test_malformed_fill_halts_without_booking(self):
  self.arm();self.e.cycle();order=next(iter(self.broker.orders.values()));order['filled_size']='1000000'
  self.assertFalse(self.e.reconcile());self.assertFalse(self.e.enabled())
  with self.e.repo.db() as c:self.assertEqual(self.e.repo.rows(c,'fills'),[])
 def test_existing_external_assets_are_never_imported_or_sold(self):
  self.broker.accounts['AAA']={'available':'400','held':'0'};self.arm();self.e.cycle();self.e.reconcile()
  with self.e.repo.db() as c:pos=self.e.repo.rows(c,'positions')[0]
  self.assertLess(number(pos['quantity']),1)
 def test_sell_and_pnl_after_cost(self):
  self.arm()
  with self.e.repo.db() as c:self.e.repo.set(c,'live_anchor',self.e.clock()-4*604800)
  self.e.cycle();self.e.reconcile();self.market.hour=1;self.market.factor=1.2
  self.e.cycle();self.e.reconcile()
  with self.e.repo.db() as c:
   sells=[r for r in self.e.repo.rows(c,'fills') if r['side']=='SELL'];self.assertTrue(sells);self.assertGreater(number(sells[0]['realized_cad']),0)
 def test_future_week_never_fabricates_money(self):
  self.arm();anchor=self.e.clock()
  with patch.object(self.e,'clock',return_value=anchor+604800*4):self.assertEqual(self.e.cap(),1000)
  self.broker.accounts['USD']['available']='0';self.assertFalse(self.e.readiness()['ready'])
 def test_native_quote_products_only(self):
  self.e.configure({'quote':'USDC','minimum_net':'.005','fx_cost_bps':'25','costs_confirmed':True,'enable_live':True})
  with patch.object(self.e,'quote_rate',return_value=Decimal('1.37')):
   self.assertFalse(self.e.readiness()['ready'])
 def test_illegal_configuration_and_foreign_fields(self):
  with self.assertRaises(ValueError):self.e.configure({'quote':'USD','enable_live':True})
  with self.assertRaises(ExchangeError):LiveAdapter.validate_payload({'product_id':'AAA-USD','side':'BUY','leverage':5,'order_configuration':{'market_market_ioc':{'quote_size':'1'}}})
 def test_partial_ioc_is_booked_only_after_terminal_settlement(self):
  self.arm();self.e.cycle();order=next(iter(self.broker.orders.values()))
  for key in ('filled_size','filled_value','total_fees'):order[key]=str(number(order[key])/2)
  order.update(status='OPEN',settled=False)
  self.assertFalse(self.e.reconcile())
  with self.e.repo.db() as c:self.assertEqual(self.e.repo.rows(c,'fills'),[])
  order.update(status='CANCELLED',settled=True);self.assertTrue(self.e.reconcile());self.assertTrue(self.e.reconcile())
  with self.e.repo.db() as c:
   fills=self.e.repo.rows(c,'fills');self.assertEqual(len(fills),1);self.assertEqual(fills[0]['quantity'],order['filled_size'])
  self.assertTrue(self.e.audit_ledger())
 def test_mismatched_exchange_order_id_halts(self):
  self.arm();self.e.cycle();next(iter(self.broker.orders.values()))['order_id']='someone-elses-order'
  self.assertFalse(self.e.reconcile());self.assertFalse(self.e.enabled())
 def test_corrupt_cost_or_deleted_fill_cannot_rearm(self):
  self.arm();self.e.cycle();self.e.reconcile();self.e.stop()
  with self.e.repo.db() as c:
   pos=self.e.repo.rows(c,'positions')[0];pos['cost']='0';self.e.repo.put(c,'positions',pos['product'],pos,replace=True)
  with self.assertRaisesRegex(ValueError,'LIVE_LEDGER_POSITION_MISMATCH'):self.e.activate({'confirmation':'실제 돈으로 자동매매 시작','confirmed':True})
  self.assertFalse(self.e.enabled())
 def test_peak_is_persisted_between_hourly_decisions(self):
  self.arm();self.e.cycle();self.e.reconcile();self.market.factor=1.1;self.market.hour=1
  self.e.cycle()
  with self.e.repo.db() as c:p=self.e.repo.one(c,'positions','AAA-USD')
  self.assertGreater(number(p['peak']),Decimal('147'))
  self.assertTrue(self.e.audit_ledger())
 def test_loss_limit_checked_even_without_new_signal(self):
  self.arm()
  with patch.object(self.e,'snapshot',return_value={'daily_loss':'.031','drawdown':'0'}),patch.object(self.e,'evaluate',side_effect=AssertionError('must halt before evaluating')):self.e.cycle()
  self.assertFalse(self.e.enabled());self.assertEqual(self.e.state()['reason'],'LOSS_LIMIT');self.assertEqual(self.broker.sent,[])
 def test_cancellation_only_targets_own_intent(self):
  self.arm();self.e.cycle();order=next(iter(self.broker.orders.values()))
  with patch.object(self.broker,'cancel_order',wraps=self.broker.cancel_order) as cancel:
   self.e.stop();cancel.assert_called_once_with(order['order_id'])
  self.assertFalse(self.e.enabled());self.e.reconcile();self.assertEqual(len(self.broker.sent),1)
 def test_live_mode_blocks_paper_actions(self):
  from stock101.crypto import service
  self.arm()
  with patch.object(service,'live_engine',return_value=self.e),patch.object(service,'engine') as paper:
   for action in ('multi_start','mode','recover','evaluate','backtest'):
    with self.assertRaisesRegex(ValueError,'STOP_AND_RECONCILE_LIVE_FIRST'):service.action(None,{'action':'crypto_'+action,'mode':'PAPER'})
   self.assertEqual(paper.return_value.start_multi.call_count,0)
 def test_live_can_tighten_risk_but_cannot_raise_hard_ceiling(self):
  config={'quote':'USD','minimum_net':'.005','fx_cost_bps':'25','costs_confirmed':True,'enable_live':True}
  self.e.configure({**config,'risk_limits':{'single_order':'.01','cash_reserve':'.6'}})
  self.e.activate({'confirmation':'실제 돈으로 자동매매 시작','confirmed':True});self.e.cycle();self.e.reconcile()
  with self.e.repo.db() as c:
   for f in self.e.repo.rows(c,'fills'):self.assertLessEqual((number(f['notional'])+number(f['fee']))*number(f['cad_rate']),10)
  self.e.stop()
  with self.assertRaisesRegex(ValueError,'RISK_LIMIT_CANNOT_BE_WEAKENED'):self.e.configure({**config,'risk_limits':{'single_order':'.50'}})
 def test_paper_only_stops_after_live_requirements_pass(self):
  from unittest.mock import Mock
  callback=Mock();self.configure();self.broker.trade=False
  with self.assertRaises(ValueError):self.e.activate({'confirmation':'실제 돈으로 자동매매 시작','confirmed':True},before_start=callback)
  callback.assert_not_called();self.broker.trade=True;self.e.activate({'confirmation':'실제 돈으로 자동매매 시작','confirmed':True},before_start=callback);callback.assert_called_once()

class LiveAdapterTests(unittest.TestCase):
 def setUp(self):
  self.calls=[]
  def transport(method,path,params,body,headers):
   self.calls.append((method,path,params,body));return {'ok':True}
  self.a=LiveAdapter(transport);self.a._jwt=lambda path,method:'synthetic-token'
  self.order={'product_id':'AAA-USD','side':'BUY','client_order_id':'d3eea8b0-2a4e-4a85-9a1a-0ae2f5d52a30','order_configuration':{'sor_limit_ioc':{'base_size':'.01','limit_price':'100'}}}
 def test_preview_cannot_place_and_live_submit_requires_permit(self):
  self.a.preview(self.order);self.assertEqual(self.calls[0][1],'/api/v3/brokerage/orders/preview');self.assertNotIn('client_order_id',self.calls[0][3])
  with self.assertRaisesRegex(ExchangeError,'LOCAL_LIVE_ACTIVATION_REQUIRED'):self.a.place_order(self.order)
  self.assertEqual(len(self.calls),1);self.a.permit=lambda kind:True;self.a.place_order(self.order)
  self.assertEqual(self.calls[1][:2],('POST','/api/v3/brokerage/orders'))
 def test_invalid_client_id_and_price_tick_cannot_submit(self):
  self.a.permit=lambda kind:True
  with self.assertRaisesRegex(ExchangeError,'INVALID_CLIENT_ORDER_ID'):self.a.place_order({**self.order,'client_order_id':'-'*36})
  with self.assertRaisesRegex(ExchangeError,'INVALID_PRICE_INCREMENT'):self.a.live_product({**Markets().get_product('AAA-USD'),'price_increment':'0'})
  self.assertEqual(self.calls,[])
 def test_jwt_is_bound_to_http_method_without_query(self):
  import jwt
  from cryptography.hazmat.primitives.asymmetric import ec
  from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption
  key=ec.generate_private_key(ec.SECP256R1());pem=key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()).decode()
  a=LiveAdapter(credential_provider=lambda:('organizations/test/apiKeys/test',pem))
  for method in ('GET','POST'):
   claims=jwt.decode(a._jwt('/api/v3/brokerage/orders',method),key.public_key(),algorithms=['ES256'])
   self.assertEqual(claims['uri'],method+' api.coinbase.com/api/v3/brokerage/orders');self.assertEqual(claims['exp']-claims['nbf'],120)
 def test_missing_credentials_has_safe_error(self):
  a=LiveAdapter(credential_provider=lambda:None)
  with self.assertRaisesRegex(ExchangeError,'READ_ONLY_CREDENTIALS_NOT_CONFIGURED'):a.permissions()
 def test_account_pages_are_aggregated_without_account_identifiers(self):
  pages=[{'accounts':[{'uuid':'private-id','currency':'USD','available_balance':{'currency':'USD','value':'1.23'},'hold':{'currency':'USD','value':'.10'}}],'has_next':True,'cursor':'next'}, {'accounts':[{'currency':'USD','available_balance':{'currency':'USD','value':'2.34'},'hold':{'currency':'USD','value':'0'}}],'has_next':False}]
  with patch.object(self.a,'call',side_effect=pages):self.assertEqual(self.a.balances(),{'USD':{'available':'3.57','held':'0.10'}})
 def test_transfer_permission_rejected(self):
  with patch.object(self.a,'call',return_value={'can_view':True,'can_trade':True,'can_transfer':True}):
   with self.assertRaisesRegex(ExchangeError,'VIEW_WITHOUT_TRANSFER_REQUIRED'):self.a.permissions(True)
