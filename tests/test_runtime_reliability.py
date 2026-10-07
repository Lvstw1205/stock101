import base64,json,sqlite3,tempfile,threading,time,unittest,zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from stock101.crypto import runtime,service,ledger_codec
from stock101.crypto.repository import Repository

class BackgroundRuntimeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.runtime=runtime.Runtime(self.tmp.name);self.addCleanup(self.close)
 def close(self):
  self.runtime.stop.set()
  for row in self.runtime.workers.values():row['thread'].join(1)
  if self.runtime.supervisor:self.runtime.supervisor.join(1)
 def test_retry_on_exception_without_browser_or_database_calls(self):
  completed=threading.Event();calls=[]
  def work():
   calls.append(1)
   if len(calls)==1:raise sqlite3.DatabaseError('failed persistence')
   completed.set()
  self.runtime.ensure_worker('paper',work,.01)
  self.assertTrue(completed.wait(1));row=self.runtime.snapshot()['workers']['paper']
  self.assertTrue(row['alive']);self.assertGreaterEqual(row['cycles'],2);self.assertIsNone(row['last_error'])
 def test_existing_worker_is_not_duplicated(self):
  self.runtime.ensure_worker('paper',lambda:None,.01);thread=self.runtime.workers['paper']['thread']
  for _ in range(20):self.runtime.ensure_worker('paper',lambda:None,.01)
  self.assertIs(thread,self.runtime.workers['paper']['thread']);self.assertEqual(self.runtime.snapshot()['workers']['paper']['restarts'],0)
 def test_dead_worker_can_restart_but_live_thread_is_never_replaced(self):
  self.runtime.ensure_worker('paper',lambda:(_ for _ in ()).throw(SystemExit()),.01)
  self.runtime.workers['paper']['thread'].join(1);self.assertFalse(self.runtime.workers['paper']['thread'].is_alive())
  done=threading.Event();self.runtime.ensure_worker('paper',done.set,.01)
  self.assertTrue(done.wait(1));self.assertEqual(self.runtime.snapshot()['workers']['paper']['restarts'],1)
 def test_login_supervisor_initializes_without_any_page_request(self):
  initialized=threading.Event()
  def initialize(r):r.ensure_worker('paper',lambda:None,.02);initialized.set()
  self.runtime.start(initialize);self.assertTrue(initialized.wait(1))
  path=Path(self.tmp.name)/'stock101-coinbase/runtime-heartbeat.json'
  deadline=time.monotonic()+1
  while not path.exists() and time.monotonic()<deadline:threading.Event().wait(.01)
  data=json.loads(path.read_text());self.assertTrue(data['independent_of_browser']);self.assertTrue(data['workers']['paper']['alive'])
  self.assertTrue(data['supervisor_alive']);self.assertGreater(data['heartbeat_at'],data['started_at'])
 def test_standby_never_starts_duplicate_trading_workers(self):
  with patch.dict('os.environ',{'AIRI_STANDBY':'1'}):
   self.runtime.start(lambda _:self.fail('started'));self.runtime.ensure_worker('paper',lambda:None,.01)
  self.assertIsNone(self.runtime.supervisor);self.assertFalse(self.runtime.workers)
 def test_failed_live_ledger_disarms_and_does_not_break_paper_ui(self):
  instance=SimpleNamespace(stop_event=threading.Event(),_permit=True)
  app=SimpleNamespace(DATA=Path(self.tmp.name))
  with patch.dict(service._live_instances,{str(app.DATA):instance}),patch.object(service,'live_engine',side_effect=sqlite3.DatabaseError('bad header')):
   status=service.live_state(app)
  self.assertFalse(status['running']);self.assertFalse(status['ledger_available']);self.assertEqual(status['exchange_order_status'],'UNKNOWN')
  self.assertTrue(instance.stop_event.is_set());self.assertFalse(instance._permit)
 def test_failure_of_live_stop_persistence_cannot_leave_permit_enabled(self):
  instance=SimpleNamespace(cycle=Mock(side_effect=sqlite3.DatabaseError()),fail=Mock(side_effect=sqlite3.DatabaseError()),stop_event=threading.Event(),_permit=True)
  with self.assertRaises(sqlite3.DatabaseError):service._live_cycle(instance)
  self.assertFalse(instance._permit);self.assertTrue(instance.stop_event.is_set())

class LosslessLedgerCodecTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.repo=Repository(Path(self.tmp.name)/'paper.db')
  self.value=dict(currency='CAD',initial='1000',cash='438.5757965263473915798129659',equity='967.49',positions=[dict(product=f'COIN{i}-USD',quantity='0.000123456789012345678901234',cost='12.456789012345678',market_value='12.12',unrealized='-.336789012345678',entry_plan={'strategy_id':'ma200','strategy_name':'추세 매매','data':['repeated evidence']*50}) for i in range(55)])
 def test_snapshot_roundtrip_preserves_every_decimal_and_strategy(self):
  original=json.dumps(self.value)
  with self.repo.db() as c:
   self.repo.put(c,'portfolio_snapshots','x',self.value)
   raw=c.execute('SELECT payload FROM portfolio_snapshots').fetchone()[0]
   self.assertLess(len(raw),len(original)*.2)
   self.assertEqual(self.repo.one(c,'portfolio_snapshots','x'),self.value)
   loaded=self.repo.rows(c,'portfolio_snapshots')[0];loaded.pop('id');loaded.pop('created');self.assertEqual(loaded,self.value)
   self.assertEqual(self.repo.decode_payload(raw),self.value)
   scalar=self.repo.equity_series(c,'CAD')[0];self.assertEqual(scalar['initial'],'1000');self.assertEqual(scalar['equity'],'967.49')
  self.assertEqual(json.dumps(self.value),original)
 def test_decision_detail_expands_both_portfolios_and_keeps_scalar_sql(self):
  record=dict(decision_id='ST101-X',timestamp=123,portfolio_before=self.value,portfolio_after=self.value,analysis={'decision':'HOLD'},status='SIMULATED_NO_TRADE')
  with self.repo.db() as c:
   self.repo.put(c,'trade_decisions','record',record)
   loaded=self.repo.decision_detail(c,'ST101-X','record');loaded.pop('created');loaded.pop('id');self.assertEqual(loaded,record)
   self.assertEqual(self.repo.decision_summaries(c)[0]['analysis']['decision'],'HOLD')
 def test_existing_plain_records_remain_byte_identical(self):
  raw=json.dumps(self.value)
  with self.repo.db() as c:c.execute('INSERT INTO portfolio_snapshots VALUES(?,?,?)',('old',123,raw))
  with self.repo.db() as c:
   self.assertEqual(self.repo.one(c,'portfolio_snapshots','old'),self.value)
   self.assertEqual(c.execute('SELECT payload FROM portfolio_snapshots').fetchone()[0],raw)
 def test_fills_and_positions_are_never_compressed(self):
  for table in ['positions','orders','fills','paper_orders']:
   self.assertIs(ledger_codec.encode(table,self.value),self.value)
 def test_bad_compressed_data_cannot_become_empty_holdings(self):
  for encoded in ['garbage','!!!!',base64.b64encode(zlib.compress(b'{}')).decode(),base64.b64encode(zlib.compress(b'[]')+b'trailing').decode()]:
   with self.subTest(encoded=encoded),self.assertRaises(ValueError):ledger_codec.decode(json.dumps({'positions':{ledger_codec.KEY:encoded}}))
 def test_expansion_is_bounded(self):
  encoded=base64.b64encode(zlib.compress(b'['+b' '*200+b']')).decode()
  with patch.object(ledger_codec,'MAX_BYTES',100),self.assertRaises(ValueError):ledger_codec.decode(json.dumps({'positions':{ledger_codec.KEY:encoded}}))
