"""Low-priority, checkpointed research worker; no broker or primary-wallet access."""
import fcntl,json,os,sys,threading,time
from pathlib import Path
from ..agent_research import atomic,read
from . import research,validation,specs

def main(folder):
    folder=Path(folder)
    with (folder/'worker.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        try:os.nice(10)
        except OSError:pass
        current=dict(status='RUNNING',started_at=time.time(),stage='자료 준비',completed=0,total=1)
        stop=threading.Event();mutex=threading.Lock()
        def publish(info):
            if (folder/'cancel').exists():raise ValueError('QUANT_CANCELLED')
            if 'checkpoint' in info:atomic(folder/'checkpoint.json',info['checkpoint'])
            with mutex:
                current.update({k:v for k,v in info.items() if k!='checkpoint'},updated_at=time.time())
                atomic(folder/'progress.json',current)
        def heartbeat():
            while not stop.wait(5):
                with mutex:current['updated_at']=time.time();atomic(folder/'progress.json',current)
        threading.Thread(target=heartbeat,daemon=True).start()
        try:
            request=read(folder/'request.json');publish({})
            from .manager import code_hash
            if request['code_hash']!=code_hash():raise ValueError('QUANT_CODE_CHANGED')
            if request['kind']=='research':result=research.generate(request,publish)
            else:
                snapshot=read(folder/'snapshot.json')
                if specs.digest(snapshot)!=request['snapshot_hash']:raise ValueError('QUANT_INPUT_HASH_MISMATCH')
                result=validation.run(snapshot,request['spec'],publish,read(folder/'checkpoint.json',{}))
            if (folder/'cancel').exists():raise ValueError('QUANT_CANCELLED')
            result.update(created_at=time.time(),code_hash=request['code_hash'],snapshot_hash=request.get('snapshot_hash'))
            atomic(folder/'result.json',result);publish(dict(status='COMPLETE',stage='완료'))
        except Exception as error:
            code=str(error)
            if len(code)>100 or not code.replace('_','').isalnum():code='QUANT_WORKER_FAILED'
            with mutex:
                current.update(status='CANCELLED' if code=='QUANT_CANCELLED' else 'FAILED',error=code,error_type=type(error).__name__,updated_at=time.time())
                atomic(folder/'progress.json',current)
        finally:stop.set()

if __name__=='__main__':main(sys.argv[1])
