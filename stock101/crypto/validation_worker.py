"""Separate low-priority process; reads a public-candle snapshot only."""
import fcntl,os,sys,time
from pathlib import Path
from . import validation,validation_job

def main(directory):
    directory=Path(directory)
    with (directory/'worker.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        try:os.nice(10)
        except OSError:pass
        started=time.time()
        def progress(info):validation_job.atomic(directory/'progress.json',dict(status='RUNNING',started_at=started,updated_at=time.time(),**info))
        try:
            snapshot=validation_job.read(directory/'input.json')
            if snapshot['code_hash']!=validation_job.fingerprint():raise ValueError('VALIDATION_CODE_CHANGED')
            result=validation.run(snapshot,progress)
            result.update(code_hash=snapshot['code_hash'],created_at=time.time(),missing_assets=snapshot['missing_assets'],
                          fx_history_available=bool(snapshot.get('fx_history')))
            validation_job.atomic(directory/'report.json',result)
            validation_job.atomic(directory/'progress.json',dict(status='COMPLETE',started_at=started,updated_at=time.time(),completed=153,total=153))
        except Exception as error:
            reason=str(error)
            if not reason or len(reason)>100 or not all(c.isupper() or c=='_' or c.isdigit() for c in reason):reason='VALIDATION_FAILED'
            validation_job.atomic(directory/'progress.json',dict(status='FAILED',reason=reason,error_type=type(error).__name__,updated_at=time.time()))

if __name__=='__main__':main(sys.argv[1])
