"""Isolated paper ledger. Monetary amounts persisted as decimal strings."""
import contextlib
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path
from . import ledger_codec,storage

TABLES=('exchange_accounts','portfolios','assets','market_snapshots','strategies','strategy_versions','strategy_signals','trade_decisions','risk_decisions','orders','fills','positions','portfolio_snapshots','performance_metrics','system_events','paper_orders','backtest_runs','strategy_experiments','post_trade_analysis','market_history','daily_history')
class Repository:
    def __init__(self,path):
        storage.guard(path)
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS paper_accounts(currency TEXT PRIMARY KEY,cash TEXT NOT NULL,initial TEXT NOT NULL)')
            for table in TABLES:
                c.execute(f'CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL)')
            self.ensure_indexes(c)
            c.execute('CREATE TABLE IF NOT EXISTS decision_sequence(id INTEGER PRIMARY KEY AUTOINCREMENT)')
            for k,v in {'mode':'PAPER','halted':True,'halt_reason':'STARTUP_RECONCILIATION_REQUIRED','armed':None}.items():
                c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,json.dumps(v)))
            # Restart cannot erase a hard loss stop or a failed risk calculation.
            prior_reason=self.get(c,'halt_reason') if self.get(c,'halted') else None
            self.set(c,'armed',None);self.set(c,'halted',True)
            if prior_reason not in ('LOSS_LIMIT','RISK_CALCULATION_FAILED'):
                self.set(c,'halt_reason','STARTUP_RECONCILIATION_REQUIRED');self.set(c,'halt_at',time.time())
            self.put(c,'system_events',str(time.time_ns()),{'category':'system','code':'STARTUP_HALTED'})
    @contextlib.contextmanager
    def db(self):
        storage.guard(self.path)
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row
        try:
            c.execute('PRAGMA busy_timeout=15000');yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()
    @staticmethod
    def ensure_indexes(c):
        # LIMIT queries must not parse/sort years of nested portfolio JSON.
        # Indexes preserve the immutable journal and all Decimal string values.
        for table in TABLES:
            c.execute(f'CREATE INDEX IF NOT EXISTS {table}_created ON {table}(created DESC)')
        c.execute("CREATE INDEX IF NOT EXISTS portfolio_risk_scalars ON portfolio_snapshots(json_extract(payload,'$.currency'),created DESC,json_extract(payload,'$.equity'),json_extract(payload,'$.initial'))")
    @staticmethod
    def get(c,key,default=None):
        row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    @staticmethod
    def set(c,key,value):c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value,allow_nan=False)))
    @staticmethod
    def put(c,table,key,value,replace=False):
        if table not in TABLES:raise ValueError('UNKNOWN_TABLE')
        c.execute(f'INSERT {"OR REPLACE" if replace else ""} INTO {table} VALUES(?,?,?)',(key,time.time(),json.dumps(ledger_codec.encode(table,value),allow_nan=False,separators=(',',':'))))
    @staticmethod
    def decode_payload(raw):return ledger_codec.decode(raw)
    @staticmethod
    def rows(c,table,limit=500):
        if table not in TABLES:raise ValueError('UNKNOWN_TABLE')
        return [{**ledger_codec.decode(r['payload']),'id':r['id'],'created':r['created']} for r in c.execute(f'SELECT * FROM {table} ORDER BY created DESC LIMIT ?', (limit,))]
    @staticmethod
    def decision_summaries(c,limit=100):
        # UI rows need neither full indicator arrays nor two copies of every
        # holding. Project from the immutable journal without rewriting it.
        query="""SELECT id,created,json_extract(payload,'$.timestamp') AS original_timestamp,json_object(
            'decision_id',json_extract(payload,'$.decision_id'),
            'timestamp',json_extract(payload,'$.timestamp'),
            'product',json_extract(payload,'$.product'),
            'status',json_extract(payload,'$.status'),
            'strategy_version',json_extract(payload,'$.strategy_version'),
            'analysis',json_object('decision',json_extract(payload,'$.analysis.decision'),
                'regime',json_object('name',json_extract(payload,'$.analysis.regime.name')),
                'plan',json_object('strategy_name',json_extract(payload,'$.analysis.plan.strategy_name'))),
            'requested_order',json_object('notional',json_extract(payload,'$.requested_order.notional')),
            'risk',json_object('approved_notional',json_extract(payload,'$.risk.approved_notional'),
                'status',json_extract(payload,'$.risk.status'))
            ) AS summary FROM trade_decisions ORDER BY created DESC LIMIT ?"""
        return [{**json.loads(r['summary']),'timestamp':r['original_timestamp'],'id':r['id'],'created':r['created']} for r in c.execute(query,(limit,))]
    @staticmethod
    def decision_detail(c,decision_id,record_id=None):
        # A detail click must not deserialize up to 100,000 unrelated records.
        if record_id:
            row=c.execute('SELECT * FROM trade_decisions WHERE id=?',(record_id,)).fetchone()
        else:
            row=c.execute("SELECT * FROM trade_decisions WHERE json_extract(payload,'$.decision_id')=? ORDER BY created DESC LIMIT 1",(decision_id,)).fetchone()
        if not row:return None
        value=ledger_codec.decode(row['payload'])
        if value.get('decision_id')!=decision_id:return None
        return {**value,'id':row['id'],'created':row['created']}
    @staticmethod
    def decision_related(c,table,decision_id):
        if table not in ('orders','fills'):raise ValueError('UNKNOWN_DECISION_RELATION')
        query=f"SELECT * FROM {table} WHERE json_extract(payload,'$.decision_id')=? ORDER BY created DESC LIMIT 100000"
        return [{**ledger_codec.decode(r['payload']),'id':r['id'],'created':r['created']} for r in c.execute(query,(decision_id,))]
    @staticmethod
    def equity_series(c,currency):
        # Risk needs only these scalars, not thousands of historical copies of
        # every position/entry plan. Preserve decimal strings and time ordering.
        return list(c.execute("SELECT created,json_extract(payload,'$.equity') AS equity,json_extract(payload,'$.initial') AS initial FROM portfolio_snapshots WHERE json_extract(payload,'$.currency')=? ORDER BY created DESC LIMIT 100000",(currency,)))
    @staticmethod
    def one(c,table,key):
        if table not in TABLES:raise ValueError('UNKNOWN_TABLE')
        row=c.execute(f'SELECT payload FROM {table} WHERE id=?',(key,)).fetchone()
        return ledger_codec.decode(row[0]) if row else None
    @staticmethod
    def decision_id(c):
        n=c.execute('INSERT INTO decision_sequence DEFAULT VALUES').lastrowid
        return 'ST101-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')+'-'+str(n).zfill(6)
    def halt(self,code,diagnostic=None):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');self.set(c,'halted',True);self.set(c,'halt_reason',code);self.set(c,'armed',None)
            self.set(c,'halt_at',time.time())
            self.put(c,'system_events',str(time.time_ns()),{'category':'risk','code':code,'diagnostic':diagnostic})
