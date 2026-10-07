"""Namespaced, additive migrations and revisioned research records."""
import json,re,time,uuid
from . import specs

TABLES=('quant_experiments','quant_events','quant_reports','quant_observations')
ACTIVE={'QUEUED','VALIDATING','INCUBATING','SHADOW_LIVE'}

def identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value):raise ValueError('QUANT_INVALID_ID')
    return value

class Store:
    def __init__(self,repo):
        self.repo=repo
        with repo.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for table in TABLES:
                c.execute(f'CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL)')
                c.execute(f'CREATE INDEX IF NOT EXISTS {table}_created ON {table}(created DESC)')
            repo.set(c,'quant_expansion_schema',{'version':1,'additive':True})
    @staticmethod
    def put(c,table,key,row):
        if table not in TABLES:raise ValueError('QUANT_INVALID_TABLE')
        c.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?)',(key,time.time(),json.dumps(row,ensure_ascii=False,allow_nan=False)))
    def get(self,eid):
        identifier(eid)
        with self.repo.db() as c:
            row=c.execute('SELECT payload FROM quant_experiments WHERE id=?',(eid,)).fetchone()
        if not row:raise ValueError('QUANT_NOT_FOUND')
        return json.loads(row['payload'])
    def list(self):
        with self.repo.db() as c:return [json.loads(r['payload']) for r in c.execute('SELECT payload FROM quant_experiments ORDER BY created DESC LIMIT 200')]
    def create(self,spec,source='manual',research=None):
        spec=specs.validate(spec);eid=uuid.uuid4().hex;now=time.time()
        row=dict(id=eid,spec=spec,spec_hash=specs.digest(spec),source=source,research=research,
                 critique=specs.critique(spec),status='PROPOSED',created_at=now,updated_at=now,revision=1)
        with self.repo.db() as c:
            c.execute('BEGIN IMMEDIATE')
            self.put(c,'quant_experiments',eid,row)
            self.put(c,'quant_events',uuid.uuid4().hex,dict(experiment_id=eid,at=now,event='PROPOSED',source=source))
        return row
    def update(self,eid,changes,allowed=None):
        identifier(eid)
        if set(changes)&{'spec','spec_hash','id','created_at'}:raise ValueError('QUANT_IMMUTABLE_SPEC')
        with self.repo.db() as c:
            c.execute('BEGIN IMMEDIATE')
            raw=c.execute('SELECT payload FROM quant_experiments WHERE id=?',(eid,)).fetchone()
            if not raw:raise ValueError('QUANT_NOT_FOUND')
            row=json.loads(raw['payload'])
            if allowed and row['status'] not in allowed:raise ValueError('QUANT_INVALID_TRANSITION')
            before=row['status'];row.update(changes,updated_at=time.time(),revision=row['revision']+1)
            self.put(c,'quant_experiments',eid,row)
            self.put(c,'quant_events',uuid.uuid4().hex,dict(experiment_id=eid,at=row['updated_at'],event=row['status'],previous=before,revision=row['revision']))
        return row
    def report(self,eid,result=None):
        identifier(eid)
        with self.repo.db() as c:
            if result is not None:self.put(c,'quant_reports',eid,result)
            row=c.execute('SELECT payload FROM quant_reports WHERE id=?',(eid,)).fetchone()
        return json.loads(row['payload']) if row else None
    def observation(self,eid,value=None):
        identifier(eid)
        with self.repo.db() as c:
            if value is not None:self.put(c,'quant_observations',eid,value)
            row=c.execute('SELECT payload FROM quant_observations WHERE id=?',(eid,)).fetchone()
        return json.loads(row['payload']) if row else None
    def events(self,eid):
        identifier(eid)
        with self.repo.db() as c:
            return [json.loads(r['payload']) for r in c.execute("SELECT payload FROM quant_events WHERE json_extract(payload,'$.experiment_id')=? ORDER BY created DESC LIMIT 40",(eid,))]
