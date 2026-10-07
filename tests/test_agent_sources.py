import datetime as dt,importlib.util,time,unittest
from unittest.mock import patch
from stock101.crypto import agent_sources

@unittest.skipUnless(importlib.util.find_spec('tradingagents'),'Isolated upstream environment required')
class SourcesTests(unittest.TestCase):
 def setUp(self):
  from tradingagents.agents import tools
  from tradingagents.dataflows import router
  from tradingagents.dataflows.vendors.yahoo import snapshot
  self.router=router;self.tools=tools
  for methods in router.VENDOR_METHODS.values():
   old=dict(methods);self.addCleanup(lambda methods=methods,old=old:(methods.clear(),methods.update(old)))
  old=tools.build_verified_market_snapshot;old2=snapshot.build_verified_market_snapshot
  self.addCleanup(setattr,tools,'build_verified_market_snapshot',old);self.addCleanup(setattr,snapshot,'build_verified_market_snapshot',old2)
  self.now=time.time();self.today=dt.date.today().isoformat()
  self.snap={'BTC-USD':{'daily':[dict(start=self.now-86400*(250-i),open=100+i,high=102+i,low=99+i,close=101+i,volume=3) for i in range(250)],'captured_at':self.now,'currency':'USD','news_status':'READY','articles':[]}}
 def test_uses_completed_coinbase_prices_and_indicators(self):
  self.snap['BTC-USD']['daily'].append(dict(start=self.now,open=999999,high=999999,low=999999,close=999999,volume=99))
  agent_sources.install(self.snap,self.today)
  report=self.tools.build_verified_market_snapshot('BTC-USD',self.today)
  self.assertIn('Coinbase',report);self.assertNotIn('999999',report);self.assertIn('close_200_sma',report)
 def test_missing_ticker_or_news_falls_back_instead_of_inventing_data(self):
  from tradingagents.dataflows.errors import VendorUnavailableError
  agent_sources.install(self.snap,self.today)
  with self.assertRaises(VendorUnavailableError):self.router.VENDOR_METHODS['get_stock_data']['coinbase']('ETH-USD','2020-01-01',self.today)
  with self.assertRaises(VendorUnavailableError):self.router.VENDOR_METHODS['get_news']['coinbase']('BTC-USD','2020-01-01',self.today)
 def test_past_runs_never_install_todays_snapshot(self):
  original=self.tools.build_verified_market_snapshot;agent_sources.install(self.snap,'2020-01-01');self.assertIs(self.tools.build_verified_market_snapshot,original)
