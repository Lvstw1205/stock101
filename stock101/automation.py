"""Opt-in daily paper replay. Never imports or calls a live broker adapter."""
import datetime as dt
import threading
import uuid
from . import indicators,strategies,data
from .brokers import PaperTradingAdapter
from .store import now

_threads={};_thread_lock=threading.Lock();_cycle_lock=threading.Lock()


def ensure_started(app,store):
    key=str(store.path)
    with _thread_lock:
        if key in _threads:return
        def work():
            while not threading.Event().wait(60):
                try:cycle(app,store)
                except Exception as e:store.audit('paper_scheduler_error',{'error':type(e).__name__})
        t=threading.Thread(target=work,name='stock101-paper-scheduler',daemon=True)
        _threads[key]=t;t.start()


def cycle(app,store):
    from .service import dataset
    if not _cycle_lock.acquire(blocking=False):return {'busy':True}
    results=[]
    try:
        for item in store.list('automation'):
            cfg=item['data']
            if not cfg.get('enabled'):continue
            if store.setting('stopped',False) or store.setting('stage',0)<2:break
            try:
                d=dataset(app,cfg['symbol'])
                if d.get('imported'):raise ValueError('Automated paper requires provider data, not an imported dataset.')
                bars=d['bars'];last=bars[-1]
                age=(dt.datetime.now(dt.timezone.utc).date()-dt.date.fromisoformat(last['date'])).days
                if age>7 or age<0:raise ValueError('Latest completed bar is too old for daily paper replay.')
                if len(bars)<202:raise ValueError('Need at least 202 validated daily bars for warm-up.')
                if last['date']<=cfg['armed_after'] or last['date']==cfg.get('last_processed'):continue
                broker=PaperTradingAdapter(store,d['market']);positions=broker.getPositions()['positions']
                held=next((p for p in positions if p['symbol']==d['symbol']),None)
                strategy=store.get(cfg['strategy_id'])
                if not strategy:raise ValueError('Saved strategy missing.')
                ind=indicators.calculate(bars);i=len(bars)-2
                side='SELL' if held else 'BUY';node=strategy['exit'] if held else strategy['entry']
                result={'date':last['date'],'signal_date':bars[i]['date'],'symbol':d['symbol'],'status':'NO_SIGNAL'}
                if strategies.evaluate(node,ind,i):
                    order_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'stock101:'+item['id']+':'+last['date']+':'+side))
                    # NN07: deterministic IDs; the ledger is consulted after every uncertain restart.
                    existing=broker.getOrderStatus(order_id)
                    if existing['ok']:result={'date':last['date'],'status':'ALREADY_RECORDED','order':existing['order']}
                    else:
                        quantity=held['quantity'] if held else cfg['quantity']
                        order={'id':order_id,'symbol':d['symbol'],'side':side,'quantity':quantity,'fee':cfg['fee'],'slippage_bps':cfg['slippage_bps'],'automated':True,'automation_id':item['id']}
                        reference={'price':last['open'],'as_of':last['date'],'signal_date':bars[i]['date'],'source':d['source'],'currency':d['currency'],'mode':'AUTOMATED PAPER DAILY REPLAY: prior-close signal, next completed bar open; not a real-time execution','reason':strategies.reason(node,ind,i)}
                        result=broker.placeOrder(order,reference)
                        result['date']=last['date']
                import json
                with store.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    current=json.loads(c.execute('SELECT payload FROM documents WHERE id=?',(item['id'],)).fetchone()[0])
                    current.update(last_processed=last['date'],last_check=now(),last_result=result)
                    if result.get('ok') is False:current['enabled']=False
                    c.execute('UPDATE documents SET payload=? WHERE id=?',(json.dumps(current),item['id']))
                store.audit('paper_scheduler',{'id':item['id'],**result});results.append(result)
            except Exception as e:
                # Unknown data or state pauses the run; no submit retry or automatic resume.
                cfg.update(enabled=False,last_check=now(),last_result={'status':'PAUSED_ERROR','error':str(e)[:300]})
                store.save('automation',item['id'],cfg);store.audit('paper_scheduler_paused',{'id':item['id'],'error':str(e)[:300]})
        return {'results':results}
    finally:_cycle_lock.release()
