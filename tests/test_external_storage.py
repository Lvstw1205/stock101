import json,plistlib,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from stock101.crypto import storage
from stock101.crypto.repository import Repository


class ExternalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.mount=self.root/'SSD';self.mount.mkdir()
        self.target=self.mount/'Stock101';self.target.mkdir()
        self.source=self.root/'original';self.source.symlink_to(self.target,target_is_directory=True)
        self.config=self.root/'storage.json'
        self.profile=dict(version=1,source=str(self.source),target=str(self.target),mount_point=str(self.mount),identity='test-identity',volume_uuid='TEST-VOLUME')
        self.config.write_text(json.dumps(self.profile))
        (self.target/'.stock101-storage-identity.json').write_text(json.dumps(self.profile))
        (self.target/'cad-plan.sqlite3').write_bytes(b'x'*100)
        self.disk=dict(VolumeUUID='TEST-VOLUME',MountPoint=str(self.mount),Writable=True)
        storage._verified.clear()
        self.mounted=patch.object(storage.os.path,'ismount',return_value=True);self.mounted.start();self.addCleanup(self.mounted.stop)
        self.diskutil=patch.object(storage.subprocess,'run',return_value=SimpleNamespace(stdout=plistlib.dumps(self.disk)));self.diskutil_mock=self.diskutil.start();self.addCleanup(self.diskutil.stop)
        self.space=patch.object(storage.shutil,'disk_usage',return_value=SimpleNamespace(free=20*1024**3,total=100*1024**3));self.space_mock=self.space.start();self.addCleanup(self.space.stop)

    def test_correct_mount_and_identity_allow_existing_ledger(self):
        storage.guard(self.source/'cad-plan.sqlite3',self.config)
        result=storage.status(self.source/'cad-plan.sqlite3',self.config)
        self.assertTrue(result['available']);self.assertEqual(result['location'],'EXTERNAL_SSD')
        self.assertEqual(self.diskutil_mock.call_count,1)

    def test_disconnected_drive_does_not_create_a_directory_or_blank_ledger(self):
        self.target.rename(self.mount/'saved')
        with patch.object(storage.os.path,'ismount',return_value=False),patch.object(storage,'CONFIG',self.config):
            with self.assertRaisesRegex(storage.StorageUnavailable,'연결해 주세요'):
                Repository(self.source/'cad-plan.sqlite3')
        self.assertFalse(self.target.exists());self.assertTrue((self.mount/'saved/cad-plan.sqlite3').exists())

    def test_wrong_volume_or_copied_directory_is_rejected(self):
        self.disk['VolumeUUID']='WRONG'
        self.diskutil_mock.return_value.stdout=plistlib.dumps(self.disk)
        with self.assertRaisesRegex(storage.StorageUnavailable,'다른 외장'):
            storage.guard(self.source/'cad-plan.sqlite3',self.config)
        marker=self.target/'.stock101-storage-identity.json';bad=dict(self.profile,identity='different');marker.write_text(json.dumps(bad))
        with self.assertRaisesRegex(storage.StorageUnavailable,'일치하지'):
            storage.guard(self.source/'cad-plan.sqlite3',self.config)

    def test_missing_or_empty_financial_database_is_not_reinitialized(self):
        for state in ('missing','empty'):
            db=self.target/'cad-plan.sqlite3';db.unlink(missing_ok=True)
            if state=='empty':db.touch()
            with patch.object(storage,'CONFIG',self.config),self.assertRaisesRegex(storage.StorageUnavailable,'원장 파일이 없습니다'):
                Repository(self.source/'cad-plan.sqlite3')
            self.assertEqual(db.stat().st_size if db.exists() else None,0 if state=='empty' else None)

    def test_low_space_is_reported_before_database_open(self):
        self.space_mock.return_value.free=storage.MIN_FREE_BYTES-1
        with self.assertRaisesRegex(storage.StorageUnavailable,'512MB'):
            storage.guard(self.source/'cad-plan.sqlite3',self.config)
        self.assertFalse(storage.status(self.source/'cad-plan.sqlite3',self.config)['available'])

    def test_remount_restores_access_without_resetting_records(self):
        before=(self.target/'cad-plan.sqlite3').read_bytes()
        with patch.object(storage.os.path,'ismount',return_value=False):
            self.assertFalse(storage.status(self.source/'cad-plan.sqlite3',self.config)['available'])
        storage.guard(self.source/'cad-plan.sqlite3',self.config)
        self.assertEqual((self.target/'cad-plan.sqlite3').read_bytes(),before)

    def test_existing_cached_connections_are_guarded_again(self):
        repo=object.__new__(Repository);repo.path=self.source/'cad-plan.sqlite3'
        with patch.object(storage,'CONFIG',self.config),patch.object(storage.os.path,'ismount',return_value=False):
            with self.assertRaises(storage.StorageUnavailable),repo.db():self.fail('Database opened without SSD')

    def test_unconfigured_internal_test_databases_still_work(self):
        storage.guard(self.root/'other/db.sqlite3',self.root/'missing-config.json')
        with patch.object(storage,'CONFIG',self.config):
            repo=Repository(self.root/'other/db.sqlite3')
            with repo.db() as c:self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')


if __name__=='__main__':unittest.main()
