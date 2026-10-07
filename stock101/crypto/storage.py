"""Verified external storage; a missing SSD must never create a fresh ledger."""
import json
import os
import plistlib
import shutil
import subprocess
import threading
import time
from pathlib import Path

CONFIG=Path(__file__).resolve().parents[2]/'config/stock101-storage.json'
MIN_FREE_BYTES=512*1024*1024
_lock=threading.Lock()
_verified={}


class StorageUnavailable(ValueError):
    pass


def profile(config=None):
    path=Path(config) if config is not None else CONFIG
    if not path.exists():return None
    try:
        row=json.loads(path.read_text())
        assert row['version']==1
        for key in ('source','target','mount_point'):
            assert Path(row[key]).is_absolute()
        assert row['identity'] and row['volume_uuid']
        assert Path(row['target']).is_relative_to(Path(row['mount_point']))
        return row
    except (OSError,ValueError,KeyError,AssertionError,TypeError):
        raise StorageUnavailable('Stock101 저장 위치 설정을 확인해야 합니다. 기존 원장을 새로 만들지 않습니다.') from None


def guard(path,config=None):
    """Check before directory creation/open, including cached worker instances."""
    row=profile(config)
    if row is None:return
    path=Path(path).absolute();source=Path(row['source']);target=Path(row['target']);mount=Path(row['mount_point'])
    if not (path.is_relative_to(source) or path.is_relative_to(target)):return
    try:
        if not os.path.ismount(mount):
            raise StorageUnavailable('외장 SSD를 연결해 주세요. Stock101 원장을 SSD에 보관 중이며, 빈 계좌로 대체하지 않습니다.')
        if not source.is_symlink() or source.resolve()!=target.resolve() or not target.is_dir() or not target.resolve().is_relative_to(mount.resolve()):
            raise StorageUnavailable('Stock101 원장 연결 경로가 달라졌습니다. 보관된 원장을 확인해야 합니다.')
        marker=json.loads((target/'.stock101-storage-identity.json').read_text())
        if any(marker.get(key)!=row[key] for key in ('identity','volume_uuid','source','target')):
            raise StorageUnavailable('연결된 SSD의 Stock101 원장이 일치하지 않습니다. 자동으로 새 계좌를 만들지 않습니다.')
        stat=mount.stat();key=(row['identity'],stat.st_dev,stat.st_ino)
        with _lock:
            # Refresh the physical volume identity periodically. The mount and
            # per-directory identity above are checked on every database open.
            if time.monotonic()-_verified.get(key,-1000)>30:
                data=plistlib.loads(subprocess.run(['diskutil','info','-plist',str(mount)],
                    check=True,capture_output=True,timeout=3).stdout)
                if data.get('VolumeUUID','').upper()!=row['volume_uuid'].upper() or data.get('MountPoint')!=str(mount):
                    raise StorageUnavailable('다른 외장 디스크가 연결돼 있습니다. Stock101을 보관한 SSD를 연결해 주세요.')
                if data.get('Writable') is not True:
                    raise StorageUnavailable('외장 SSD에 쓸 수 없습니다. 원장은 보존하며 기록을 중지합니다.')
                _verified.clear();_verified[key]=time.monotonic()
        if shutil.disk_usage(mount).free<MIN_FREE_BYTES:
            raise StorageUnavailable('외장 SSD 여유 공간이 512MB 미만입니다. 원장 손상을 막기 위해 새 기록을 대기합니다.')
        if path.name in ('cad-plan.sqlite3','live.sqlite3','paper.sqlite3') and (not path.is_file() or path.stat().st_size<100):
            raise StorageUnavailable('기존 거래 원장 파일이 없습니다. 새 계좌를 만들지 않고 복구를 기다립니다.')
    except StorageUnavailable:
        raise
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        raise StorageUnavailable('외장 SSD 연결을 확인하고 있습니다. 원장을 확인할 때까지 기록을 대기합니다.') from None


def status(path,config=None):
    try:
        row=profile(config)
        if row is None:return dict(location='INTERNAL',available=True)
        guard(path,config)
        usage=shutil.disk_usage(row['mount_point'])
        return dict(location='EXTERNAL_SSD',available=True,mount_point=row['mount_point'],
                    free_bytes=usage.free,total_bytes=usage.total,minimum_free_bytes=MIN_FREE_BYTES,
                    low_space=usage.free<10*1024**3)
    except StorageUnavailable as error:
        return dict(location='EXTERNAL_SSD',available=False,reason=str(error))
