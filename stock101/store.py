"""Separate local ledger. Atomic transactions, persisted safety latch and audit trail."""
import contextlib
import datetime as dt
import json
import sqlite3


def now(): return dt.datetime.now(dt.timezone.utc).isoformat()


class Store:
    def __init__(self,folder):
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=folder/'stock101.sqlite3'
        with self.db() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts(market TEXT PRIMARY KEY,currency TEXT NOT NULL,initial TEXT NOT NULL,cash TEXT NOT NULL,peak TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions(market TEXT NOT NULL,symbol TEXT NOT NULL,quantity INTEGER NOT NULL,cost TEXT NOT NULL,PRIMARY KEY(market,symbol));
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,market TEXT NOT NULL,symbol TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL);
            ''')
            for m,currency,balance in [('CA','CAD',100000),('KR','KRW',100000000),('US','USD',100000)]:
                c.execute('INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,?)',(m,currency,str(balance),str(balance),str(balance)))
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row
        try:
            with c: yield c
        finally: c.close()

    def setting(self,key,default=None):
        with self.db() as c: row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,key,value):
        with self.db() as c: c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value,allow_nan=False)))

    def save(self,kind,key,value):
        with self.db() as c: c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?)',(key,kind,json.dumps(value,allow_nan=False),now()))

    def get(self,key):
        with self.db() as c: row=c.execute('SELECT payload FROM documents WHERE id=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self,kind):
        with self.db() as c: rows=c.execute('SELECT id,payload,created FROM documents WHERE kind=? ORDER BY created DESC LIMIT 100',(kind,)).fetchall()
        return [{'id':r['id'],'created':r['created'],'data':json.loads(r['payload'])} for r in rows]

    def audit(self,kind,payload):
        with self.db() as c: c.execute('INSERT INTO audit(kind,payload,created) VALUES(?,?,?)',(kind,json.dumps(payload,allow_nan=False),now()))

    def stop(self):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('stopped','true'))
            c.execute('INSERT INTO audit(kind,payload,created) VALUES(?,?,?)',('kill_switch','{"new_automated_orders":"blocked","positions":"unchanged"}',now()))
        return {'stopped':True,'positions_liquidated':False,'cancellation':'No automated broker orders have been submitted by this build. Live broker cancellation requires an authenticated adapter.'}
