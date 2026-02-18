# Copyright (c) 2022 Aiven, Helsinki, Finland. https://aiven.io/
import datetime
from pathlib import Path

from rohmu.object_storage.local import LocalTransfer

from pghoard.object_store import BaseBackupInfoFromBucket


def test_object_store_request_backup_preservation(tmp_path: Path) -> None:
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    storage = LocalTransfer(directory=str(storage_dir))
    store = BaseBackupInfoFromBucket(name="2022_12_10", storage=storage, prefix="site_name", site="", data={})
    preserve_until = datetime.datetime(2022, 12, 18, 10, 20, 30, 123456, tzinfo=datetime.timezone.utc)
    request_name = store.request_backup_preservation(preserve_until=preserve_until)
    requests = storage.list_path("site_name/preservation_request")
    assert len(requests) == 1
    assert requests[0]["name"] == f"site_name/preservation_request/{request_name}"
    assert requests[0]["metadata"]["preserve-backup"] == "2022_12_10"
    assert requests[0]["metadata"]["preserve-until"] == "2022-12-18 10:20:30.123456+00:00"


def test_object_store_cancel_backup_preservation(tmp_path: Path) -> None:
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    storage = LocalTransfer(directory=str(storage_dir))
    store = BaseBackupInfoFromBucket(name="2022_12_10", storage=storage, prefix="site_name", site="", data={})
    preserve_until = datetime.datetime(2022, 12, 18, 10, 20, 30, 123456, tzinfo=datetime.timezone.utc)
    request_name = store.request_backup_preservation(preserve_until=preserve_until)
    store.cancel_backup_preservation(request_name)
    requests = storage.list_path("site_name/preservation_request")
    assert len(requests) == 0


def test_object_store_try_request_backup_preservation_returns_none_on_failure(tmp_path: Path) -> None:
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    storage_dir.chmod(0o000)
    try:
        storage = LocalTransfer(directory=str(storage_dir))
        store = BaseBackupInfoFromBucket(name="2022_12_10", storage=storage, prefix="site_name", site="", data={})
        preserve_until = datetime.datetime(2022, 12, 18, 10, 20, 30, 123456, tzinfo=datetime.timezone.utc)
        request_name = store.try_request_backup_preservation(preserve_until=preserve_until)
        assert request_name is None
    finally:
        storage_dir.chmod(0o700)


def test_object_store_try_cancel_backup_preservation_silently_fails(tmp_path: Path) -> None:
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    storage = LocalTransfer(directory=str(storage_dir))
    store = BaseBackupInfoFromBucket(name="2022_12_10", storage=storage, prefix="site_name", site="", data={})
    preserve_until = datetime.datetime(2022, 12, 18, 10, 20, 30, 123456, tzinfo=datetime.timezone.utc)
    request_name = store.request_backup_preservation(preserve_until=preserve_until)
    storage_dir.chmod(0o000)
    try:
        store.try_cancel_backup_preservation(request_name)
    finally:
        storage_dir.chmod(0o700)
