"""Process-owned worker supervision, independent of browser or chat activity."""
import json
import logging
import os
import threading
import time
from pathlib import Path

_lock=threading.RLock()
_runtimes={}

class Runtime:
    def __init__(self, data):
        self.data=Path(data)
        self.started_at=time.time()
        self.stop=threading.Event()
        self.workers={}
        self.supervisor=None
        self.heartbeat_at=None
        self.init_errors={}
        self.lock=threading.RLock()

    def ensure_worker(self,name,callback,interval):
        if os.environ.get('AIRI_STANDBY')=='1':return
        with self.lock:
            previous=self.workers.get(name,{})
            if previous.get('thread') and previous['thread'].is_alive():return
            row=dict(started_at=time.time(),last_started_at=None,last_completed_at=None,
                     cycles=0,last_error=None,restarts=previous.get('restarts',-1)+1)
            self.workers[name]=row
            def run():
                while not self.stop.is_set():
                    row['last_started_at']=time.time()
                    try:
                        callback()
                        row.update(last_completed_at=time.time(),last_error=None)
                    except Exception as error:
                        # One bad persistence/network call must not kill the loop.
                        row['last_error']=type(error).__name__
                        logging.getLogger(__name__).warning('Stock101 %s worker retry: %s',name,type(error).__name__)
                    finally:row['cycles']+=1
                    self.stop.wait(interval)
            row['thread']=threading.Thread(target=run,name='stock101-'+name,daemon=True)
            row['thread'].start()

    def snapshot(self):
        from .storage import status
        storage_state=status(self.data/'stock101-coinbase'/'cad-plan.sqlite3')
        with self.lock:
            workers={name:{**{k:v for k,v in row.items() if k!='thread'},'alive':row['thread'].is_alive()} for name,row in self.workers.items()}
            return dict(pid=os.getpid(),started_at=self.started_at,heartbeat_at=self.heartbeat_at,
                        supervisor_alive=bool(self.supervisor and self.supervisor.is_alive()),
                        independent_of_browser=True,login_autostart=os.environ.get('AIRI_LOGIN_AUTOSTART')=='1',workers=workers,
                        init_errors=dict(self.init_errors),storage=storage_state)

    def start(self,initialize):
        if os.environ.get('AIRI_STANDBY')=='1':return self
        with self.lock:
            if self.supervisor and self.supervisor.is_alive():return self
            def supervise():
                while not self.stop.is_set():
                    try:
                        initialize(self)
                    except Exception as error:
                        self.init_errors['supervisor']=type(error).__name__
                    self.heartbeat_at=time.time()
                    try:
                        path=self.data/'stock101-coinbase'/'runtime-heartbeat.json'
                        path.parent.mkdir(parents=True,exist_ok=True)
                        temporary=path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(self.snapshot(),allow_nan=False))
                        temporary.chmod(0o600);temporary.replace(path)
                    except OSError:
                        logging.getLogger(__name__).warning('Stock101 heartbeat file unavailable')
                    self.stop.wait(10)
            self.supervisor=threading.Thread(target=supervise,name='stock101-supervisor',daemon=True)
            self.supervisor.start()
        return self


def for_app(app):
    key=str(app.DATA)
    with _lock:
        if key not in _runtimes:_runtimes[key]=Runtime(app.DATA)
        return _runtimes[key]
