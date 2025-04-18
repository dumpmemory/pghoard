"""
pghoard - basebackup tests

Copyright (c) 2015 Ohmu Ltd
See LICENSE for details
"""
import datetime
import os
import tarfile
import time
from copy import deepcopy
from os import makedirs
from queue import Queue
from subprocess import check_call
from typing import Any, Dict, Optional

import dateutil.parser
import psycopg2
import pytest
from mock import ANY, Mock
from mock.mock import patch
from rohmu import dates, get_transfer

from pghoard import common, metrics
from pghoard.basebackup.base import PGBaseBackup
from pghoard.common import (BackupReason, BaseBackupFormat, BaseBackupMode, CallbackEvent, CallbackQueue)
from pghoard.pghoard import DeltaBaseBackupFailureInfo
from pghoard.restore import Restore, RestoreError

from ..conftest import PGHoardForTest, PGTester
from ..util import switch_wal

Restore.log_tracebacks = True

SIMPLE_BACKUP_CONFIG: Dict[str, Any] = {
    "backup_sites": {
        "foosite": {
            "encryption_key_id": None,
            "encryption_keys": {},
            "prefix": "a/b/c",
        },
    },
    "compression": {
        "algorithm": "snappy",
        "level": 10
    },
}


class TestPGBaseBackup:
    def test_parse_backup_label(self, tmpdir):
        td = str(tmpdir)
        fn = os.path.join(td, "backup.tar")
        with tarfile.open(fn, "w") as tfile:
            with open(os.path.join(td, "backup_label"), "wb") as fp:
                fp.write(
                    b'''\
START WAL LOCATION: 0/4000028 (file 000000010000000000000004)
CHECKPOINT LOCATION: 0/4000060
BACKUP METHOD: streamed
BACKUP FROM: master
START TIME: 2015-02-12 14:07:19 GMT
LABEL: pg_basebackup base backup
'''
                )
            tfile.add(os.path.join(td, "backup_label"), arcname="backup_label")
        pgb = PGBaseBackup(
            config=SIMPLE_BACKUP_CONFIG,
            site="foosite",
            connection_info=None,
            basebackup_path=None,
            compression_queue=None,
            storage=None,
            transfer_queue=None,
            metrics=metrics.Metrics(statsd={})
        )
        start_wal_segment, start_time = pgb.parse_backup_label_in_tar(fn)
        assert start_wal_segment == "000000010000000000000004"
        assert start_time == "2015-02-12T14:07:19+00:00"

    def test_find_files(self, db):
        top1 = os.path.join(db.pgdata, "top1.test")
        top2 = os.path.join(db.pgdata, "top2.test")
        sub1 = os.path.join(db.pgdata, "global", "sub1.test")
        sub2 = os.path.join(db.pgdata, "global", "sub2.test")
        sub3 = os.path.join(db.pgdata, "global", "sub3.test")

        def create_test_files():
            # Create two temporary files on top level and one in global/ that we'll unlink while iterating
            with open(top1, "w") as t1, open(top2, "w") as t2, \
                    open(sub1, "w") as s1, open(sub2, "w") as s2, open(sub3, "w") as s3:
                t1.write("t1\n")
                t2.write("t2\n")
                s1.write("s1\n")
                s2.write("s2\n")
                s3.write("s3\n")

        pgb = PGBaseBackup(
            config=SIMPLE_BACKUP_CONFIG,
            site="foosite",
            connection_info=None,
            basebackup_path=None,
            compression_queue=None,
            storage=None,
            transfer_queue=None,
            metrics=metrics.Metrics(statsd={})
        )
        create_test_files()
        files = pgb.find_files_to_backup(pgdata=db.pgdata, tablespaces={})
        first_file = next(files)
        os.unlink(top1)
        os.unlink(top2)
        os.unlink(sub1)
        os.unlink(sub2)

        # Missing files are not accepted at top level
        with pytest.raises(FileNotFoundError):
            list(files)

        # Recreate test files and unlink just the one from a subdirectory
        create_test_files()
        files = pgb.find_files_to_backup(pgdata=db.pgdata, tablespaces={})
        first_file = next(files)
        os.unlink(sub1)
        # Missing files in sub directories are ok
        ftbu = [first_file[:-1]] + list(f[:-1] for f in files if f[-1] != "leave")

        # Check that missing_ok flag is not set for top-level items
        for bu_path, local_path, missing_ok in ftbu:
            if os.path.dirname(bu_path) == "pgdata":
                assert missing_ok is False, (bu_path, local_path, missing_ok)
            else:
                assert missing_ok is True, (bu_path, local_path, missing_ok)

        # files to backup should include both top level items and two sub-level items
        bunameset = set(item[0] for item in ftbu)
        assert len(bunameset) == len(ftbu)
        assert "pgdata/top1.test" in bunameset
        assert "pgdata/top2.test" in bunameset
        assert "pgdata/global/sub1.test" not in bunameset
        assert "pgdata/global/sub2.test" in bunameset
        assert "pgdata/global/sub3.test" in bunameset

        # Now delete a file on the top level before we have a chance of tarring anything
        os.unlink(top2)

        class FakeTar:
            def __init__(self):
                self.items = []

            def add(self, local_path, *, arcname, recursive):
                assert recursive is False
                self.items.append((local_path, arcname, os.stat(local_path)))

        # This will fail because top-level items may not be missing
        faketar = FakeTar()
        with pytest.raises(FileNotFoundError):
            pgb.chunk_uploader.write_files_to_tar(files=ftbu, tar=faketar)

        # Recreate test files and unlink just a subdirectory item
        create_test_files()
        os.unlink(sub2)

        # Now adding files should work and we should end up with every file except for sub2 in the archive
        faketar = FakeTar()
        pgb.chunk_uploader.write_files_to_tar(files=ftbu, tar=faketar)
        arcnameset = set(item[1] for item in faketar.items)
        assert len(arcnameset) == len(faketar.items)
        expected_items = bunameset - {"pgdata/global/sub2.test"}
        assert arcnameset == expected_items
        assert "pgdata/global/sub1.test" not in arcnameset  # not in set of files to backup
        assert "pgdata/global/sub2.test" not in arcnameset  # acceptable loss
        assert "pgdata/global/sub3.test" in arcnameset  # acceptable

    def test_find_and_split_files_to_backup(self, tmpdir):
        pgdata = str(tmpdir.mkdir("pgdata"))
        top = os.path.join(pgdata, "split_top")
        sub = os.path.join(top, "split_sub")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(top, "f1"), "w") as f:
            f.write("a" * 50000)
        with open(os.path.join(top, "f2"), "w") as f:
            f.write("a" * 50000)
        with open(os.path.join(top, "f3"), "w") as f:
            f.write("a" * 50000)
        with open(os.path.join(sub, "f1"), "w") as f:
            f.write("a" * 50000)
        with open(os.path.join(sub, "f2"), "w") as f:
            f.write("a" * 50000)
        with open(os.path.join(sub, "f3"), "w") as f:
            f.write("a" * 50000)

        pgb = PGBaseBackup(
            config=SIMPLE_BACKUP_CONFIG,
            site="foosite",
            connection_info=None,
            basebackup_path=None,
            compression_queue=None,
            storage=None,
            transfer_queue=None,
            metrics=metrics.Metrics(statsd={})
        )
        total_file_count, chunks = pgb.find_and_split_files_to_backup(
            pgdata=pgdata, tablespaces={}, target_chunk_size=110000
        )
        # 6 files and 2 directories
        assert total_file_count == 8
        assert len(chunks) == 3
        print(chunks)

        # split_top, split_top/f1, split_top/f2
        chunk1 = [c[0] for c in chunks[0]]
        assert len(chunk1) == 3
        assert chunk1[0] == "pgdata/split_top"
        assert chunk1[1] == "pgdata/split_top/f1"
        assert chunk1[2] == "pgdata/split_top/f2"

        # split_top, split_top/f3, split_top/split_sub, split_top/split_sub/f1
        chunk2 = [c[0] for c in chunks[1]]
        assert len(chunk2) == 4
        assert chunk2[0] == "pgdata/split_top"
        assert chunk2[1] == "pgdata/split_top/f3"
        assert chunk2[2] == "pgdata/split_top/split_sub"
        assert chunk2[3] == "pgdata/split_top/split_sub/f1"

        # split_top, split_top/split_sub, split_top/split_sub/f2, split_top/split_sub/f3
        chunk3 = [c[0] for c in chunks[2]]
        assert len(chunk3) == 4
        assert chunk3[0] == "pgdata/split_top"
        assert chunk3[1] == "pgdata/split_top/split_sub"
        assert chunk3[2] == "pgdata/split_top/split_sub/f2"
        assert chunk3[3] == "pgdata/split_top/split_sub/f3"

    def _test_create_basebackup(self, capsys, db, pghoard, mode, replica=False, active_backup_mode="archive_command"):
        pghoard.create_backup_site_paths(pghoard.test_site)
        basebackup_path = os.path.join(pghoard.config["backup_location"], pghoard.test_site, "basebackup")
        q = Queue()

        pghoard.config["backup_sites"][pghoard.test_site]["basebackup_mode"] = mode
        pghoard.config["backup_sites"][pghoard.test_site]["active_backup_mode"] = active_backup_mode

        now = datetime.datetime.now(datetime.timezone.utc)
        metadata = {
            "backup-reason": BackupReason.scheduled,
            "backup-decision-time": now.isoformat(),
            "normalized-backup-time": now.isoformat(),
        }
        pghoard.create_basebackup(pghoard.test_site, db.user, basebackup_path, q, metadata)
        result = q.get(timeout=60)
        assert result.success

        # make sure it shows on the list
        Restore().run([
            "list-basebackups",
            "--config",
            pghoard.config_path,
            "--site",
            pghoard.test_site,
            "--verbose",
        ])
        out, _ = capsys.readouterr()
        assert pghoard.test_site in out
        assert "pg-version" in out

        assert "start-wal-segment" in out
        if mode in {BaseBackupMode.local_tar, BaseBackupMode.delta}:
            assert "end-time" in out
            if replica is False:
                assert "end-wal-segment" in out

        storage_config = common.get_object_storage_config(pghoard.config, pghoard.test_site)
        storage = get_transfer(storage_config)
        backups = storage.list_path(os.path.join(pghoard.config["backup_sites"][pghoard.test_site]["prefix"], "basebackup"))
        assert len(backups) > 0
        backups = sorted(backups, key=lambda backup: backup["metadata"]["backup-decision-time"])
        last_backup = backups[-1]
        assert "start-wal-segment" in last_backup["metadata"]
        assert "start-time" in last_backup["metadata"]
        assert dateutil.parser.parse(last_backup["metadata"]["start-time"]).tzinfo  # pylint: disable=no-member
        assert last_backup["metadata"]["backup-reason"] == BackupReason.scheduled
        assert last_backup["metadata"]["backup-decision-time"] == now.isoformat()
        assert last_backup["metadata"]["normalized-backup-time"] == now.isoformat()
        if mode in {BaseBackupMode.local_tar, BaseBackupMode.delta}:
            if replica is False:
                assert "end-wal-segment" in last_backup["metadata"]
            assert "end-time" in last_backup["metadata"]
            assert dateutil.parser.parse(last_backup["metadata"]["end-time"]).tzinfo  # pylint: disable=no-member
        assert last_backup["metadata"]["active-backup-mode"] == active_backup_mode
        assert last_backup["metadata"]["basebackup-mode"] == mode

    def _restore_basebackup(
        self,
        pghoard: PGHoardForTest,
        backup_out: str,
        preserve_until: Optional[str] = None,
        cancel_preserve_on_success: bool = True,
        overwrite: bool = False
    ) -> None:
        # Restoring to empty directory works
        os.makedirs(backup_out, exist_ok=True)
        arguments = [
            "get-basebackup", "--config", pghoard.config_path, "--site", pghoard.test_site, "--target-dir", backup_out
        ]
        if preserve_until is not None:
            arguments.extend(["--preserve-until", preserve_until])
            if cancel_preserve_on_success:
                arguments.extend(["--cancel-preserve-on-success"])
            else:
                arguments.extend(["--no-cancel-preserve-on-success"])
        if overwrite:
            arguments.append("--overwrite")
        Restore().run(arguments)

    def _test_restore_basebackup(
        self, db, pghoard, tmpdir, active_backup_mode="archive_command", preserve_until: Optional[str] = None
    ):
        backup_out = tmpdir.join("test-restore").strpath
        self._restore_basebackup(pghoard, backup_out, preserve_until=preserve_until)
        # Restoring on top of another $PGDATA doesn't
        with pytest.raises(RestoreError) as excinfo:
            self._restore_basebackup(pghoard, backup_out, preserve_until=preserve_until)
        assert "--overwrite not specified" in str(excinfo.value)
        # Until we use the --overwrite flag
        self._restore_basebackup(pghoard, backup_out, preserve_until=preserve_until, overwrite=True)
        check_call([os.path.join(db.pgbin, "pg_controldata"), backup_out])
        # TODO: check that the backup is valid

        # there should only be a single backup so lets compare what was in the metadata with what
        # was in the backup label
        storage_config = common.get_object_storage_config(pghoard.config, pghoard.test_site)
        storage = get_transfer(storage_config)
        backups = storage.list_path(os.path.join(pghoard.config["backup_sites"][pghoard.test_site]["prefix"], "basebackup"))

        # lets grab the backup label details for what we restored
        pgb = PGBaseBackup(
            config=SIMPLE_BACKUP_CONFIG,
            site="foosite",
            connection_info=None,
            basebackup_path=None,
            compression_queue=None,
            storage=storage,
            transfer_queue=None,
            metrics=metrics.Metrics(statsd={})
        )

        path = os.path.join(backup_out, "backup_label")
        with open(path, "r") as myfile:
            data = myfile.read()
            start_wal_segment, start_time = pgb.parse_backup_label(data)

        assert start_wal_segment == backups[0]["metadata"]["start-wal-segment"]
        assert start_time == backups[0]["metadata"]["start-time"]

        # for a standalone hot backup, the start wal file will be in the pg_xlog / pg_wal directory
        wal_dir = "pg_xlog"
        if float(db.pgver) >= float("10.0"):
            wal_dir = "pg_wal"

        path = os.path.join(backup_out, wal_dir, backups[0]["metadata"]["start-wal-segment"])
        if active_backup_mode == "standalone_hot_backup":
            assert os.path.isfile(path) is True
        else:
            assert os.path.isfile(path) is False

    def _test_basebackups(self, capsys, db, pghoard, tmpdir, mode, *, replica=False, preserve_until: Optional[None] = None):
        self._test_create_basebackup(capsys, db, pghoard, mode, replica=replica)
        self._test_restore_basebackup(db, pghoard, tmpdir, preserve_until=preserve_until)

    def test_basic_standalone_hot_backups(self, capsys, db, pghoard, tmpdir):
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic, False, "standalone_hot_backup")
        self._test_restore_basebackup(db, pghoard, tmpdir, "standalone_hot_backup")

    def test_pipe_standalone_hot_backups(self, capsys, db, pghoard, tmpdir):
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.pipe, False, "standalone_hot_backup")
        self._test_restore_basebackup(db, pghoard, tmpdir, "standalone_hot_backup")

    def test_basebackups_basic(self, capsys, db, pghoard, tmpdir):
        self._test_basebackups(capsys, db, pghoard, tmpdir, BaseBackupMode.basic)

    def test_basebackups_basic_lzma(self, capsys, db, pghoard_lzma, tmpdir):
        self._test_basebackups(capsys, db, pghoard_lzma, tmpdir, BaseBackupMode.basic)

    def test_basebackups_preserve_until(self, capsys, db, pghoard, tmpdir):
        preserve_until = str(dates.now() + datetime.timedelta(days=2))
        self._test_basebackups(capsys, db, pghoard, tmpdir, BaseBackupMode.basic, preserve_until=preserve_until)

    def test_basebackups_deletion(self, capsys, db, pghoard):
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._run_backup_deletion(pghoard)
        self._check_backups_count(pghoard, expected_count=2)

    def test_basebackups_preservation_from_delete(self, capsys, db, pghoard, tmpdir):
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        backup_out = tmpdir.join("test-restore").strpath
        preserve_until = str(dates.now() + datetime.timedelta(days=2))
        self._restore_basebackup(pghoard, backup_out, preserve_until=preserve_until, cancel_preserve_on_success=False)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._run_backup_deletion(pghoard)
        self._check_backups_count(pghoard, expected_count=4)

    def test_basebackups_preservation_from_delete_is_canceled_on_success(self, capsys, db, pghoard, tmpdir):
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        backup_out = tmpdir.join("test-restore").strpath
        preserve_until = str(dates.now() + datetime.timedelta(days=2))
        self._restore_basebackup(pghoard, backup_out, preserve_until=preserve_until, cancel_preserve_on_success=True)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.basic)
        self._run_backup_deletion(pghoard)
        self._check_backups_count(pghoard, expected_count=2)

    def _run_backup_deletion(self, pghoard: PGHoardForTest) -> None:
        pghoard.set_state_defaults(pghoard.test_site)
        pghoard.refresh_backup_list_and_delete_old(pghoard.test_site)

    def _check_backups_count(self, pghoard: PGHoardForTest, expected_count: int) -> None:
        storage_config = common.get_object_storage_config(pghoard.config, pghoard.test_site)
        storage = get_transfer(storage_config)
        backups = storage.list_path(os.path.join(pghoard.config["backup_sites"][pghoard.test_site]["prefix"], "basebackup"))
        assert len(backups) == expected_count

    @pytest.mark.parametrize(
        "delta_file_size, delta_chunk_size, expected_chunks_count, expected_delta_files_count",
        [(None, None, 1, 0), (1024 * 1024, None, 1, 2), (1024 * 1024 * 2, None, 1, 1), (1024 * 1024 * 5, None, 1, 0),
         (None, 1024 * 1024 * 10, 3, 0)]
    )
    def test_basebackups_delta_config_params(
        self, capsys, db, pghoard, tmpdir, delta_file_size: int, delta_chunk_size: int, expected_chunks_count: int,
        expected_delta_files_count: int
    ) -> None:
        site = pghoard.test_site
        site_config = pghoard.config["backup_sites"][site]
        pghoard.set_state_defaults(site)

        if delta_file_size is not None:
            site_config["basebackup_delta_mode_min_delta_file_size"] = delta_file_size
        if delta_chunk_size is not None:
            site_config["basebackup_delta_mode_chunk_size"] = delta_chunk_size

        self._test_create_basebackup(capsys, db, pghoard, BaseBackupMode.delta, replica=False)

        storage_config = common.get_object_storage_config(pghoard.config, site)
        storage = get_transfer(storage_config)
        backup_prefix = pghoard.config["backup_sites"][site]["prefix"]

        chunks_count = len(storage.list_path(os.path.join(backup_prefix, "basebackup_delta_chunk"), deep=True))
        assert chunks_count == expected_chunks_count

        delta_files_count = len(storage.list_path(os.path.join(backup_prefix, "basebackup_delta")))
        assert delta_files_count == expected_delta_files_count

        self._test_restore_basebackup(db, pghoard, tmpdir)

    def test_basebackups_local_tar_with_delta_stats(self, capsys, db, pghoard, tmpdir):
        self._test_basebackups(capsys, db, pghoard, tmpdir, BaseBackupMode.local_tar_delta_stats)

    def test_basebackups_local_tar_nonexclusive(self, capsys, db, pghoard, tmpdir):
        self._test_basebackups(capsys, db, pghoard, tmpdir, BaseBackupMode.local_tar)

    def test_basebackups_replica_local_tar_nonexclusive(self, capsys, recovery_db, pghoard, tmpdir):
        self._test_basebackups(capsys, recovery_db, pghoard, tmpdir, BaseBackupMode.local_tar, replica=True)

    def test_basebackups_pipe(self, capsys, db, pghoard, tmpdir):
        self._test_basebackups(capsys, db, pghoard, tmpdir, BaseBackupMode.pipe)

    def test_basebackups_tablespaces(self, capsys, db, pghoard, tmpdir, pg_version: str):
        # Create a test tablespace for this instance, but make sure we drop it at the end of the test as the
        # database we use is shared by all test cases, and tablespaces are a global concept so the test
        # tablespace could interfere with other tests
        tspath = tmpdir.join("extra-ts").strpath
        os.makedirs(tspath)
        conn = db.connect()
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("CREATE TABLESPACE tstest LOCATION %s", [tspath])
        r_db, r_conn = None, None
        try:
            cursor.execute("CREATE TABLE tstest (id BIGSERIAL PRIMARY KEY, value BIGINT) TABLESPACE tstest")
            cursor.execute("INSERT INTO tstest (value) SELECT * FROM generate_series(1, 1000)")
            cursor.execute("CHECKPOINT")
            cursor.execute("SELECT oid, pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname = 'tstest'")
            res = cursor.fetchone()
            assert res[1] == tspath

            # Start receivexlog since we want the WALs to be able to restore later on
            wal_directory = os.path.join(pghoard.config["backup_location"], pghoard.test_site, "xlog_incoming")
            makedirs(wal_directory, exist_ok=True)
            pghoard.receivexlog_listener(pghoard.test_site, db.user, wal_directory)
            switch_wal(conn)
            self._test_create_basebackup(capsys, db, pghoard, "local-tar")
            switch_wal(conn)
            data_dir = pghoard.config["backup_sites"][pghoard.test_site]["pg_data_directory"]
            backup_out = os.path.join(data_dir, "test-restore")
            backup_ts_out = os.path.join(data_dir, "test-restore-tstest")

            # Tablespaces are extracted to their previous absolute paths by default, but the path must be empty
            # and it isn't as it's still used by the running PG
            with pytest.raises(RestoreError) as excinfo:
                Restore().run([
                    "get-basebackup",
                    "--config",
                    pghoard.config_path,
                    "--site",
                    pghoard.test_site,
                    "--target-dir",
                    backup_out,
                ])
            assert "Tablespace 'tstest' target directory" in str(excinfo.value)
            assert "not empty" in str(excinfo.value)
            # We can't restore tablespaces to non-existent directories either
            with pytest.raises(RestoreError) as excinfo:
                Restore().run([
                    "get-basebackup",
                    "--config",
                    pghoard.config_path,
                    "--site",
                    pghoard.test_site,
                    "--target-dir",
                    backup_out,
                    "--tablespace-dir",
                    "tstest={}".format(backup_ts_out),
                ])
            assert "Tablespace 'tstest' target directory" in str(excinfo.value)
            assert "does not exist" in str(excinfo.value)
            os.makedirs(backup_ts_out)
            # We can't restore if the directory isn't writable
            os.chmod(backup_ts_out, 0o500)
            with pytest.raises(RestoreError) as excinfo:
                Restore().run([
                    "get-basebackup",
                    "--config",
                    pghoard.config_path,
                    "--site",
                    pghoard.test_site,
                    "--target-dir",
                    backup_out,
                    "--tablespace-dir",
                    "tstest={}".format(backup_ts_out),
                ])
            assert "Tablespace 'tstest' target directory" in str(excinfo.value)
            assert "empty, but not writable" in str(excinfo.value)
            os.chmod(backup_ts_out, 0o700)
            # We can't proceed if we request mappings for non-existent tablespaces
            backup_other_out = tmpdir.join("test-restore-other").strpath
            os.makedirs(backup_other_out)
            with pytest.raises(RestoreError) as excinfo:
                Restore().run([
                    "get-basebackup",
                    "--config",
                    pghoard.config_path,
                    "--site",
                    pghoard.test_site,
                    "--target-dir",
                    backup_out,
                    "--tablespace-dir",
                    "tstest={}".format(backup_ts_out),
                    "--tablespace-dir",
                    "other={}".format(backup_other_out),
                ])
            assert "Tablespace mapping for ['other'] was requested, but" in str(excinfo.value)

            # Now, finally, everything should be valid and we can proceed with restore
            Restore().run([
                "get-basebackup",
                "--config",
                pghoard.config_path,
                "--site",
                pghoard.test_site,
                "--restore-to-primary",
                "--target-dir",
                backup_out,
                "--tablespace-dir",
                "tstest={}".format(backup_ts_out),
            ])

            # Adjust the generated recovery.conf to point pghoard_postgres_command to our instance
            new_py_restore_cmd = "PYTHONPATH={} python3 -m pghoard.postgres_command --mode restore".format(
                os.path.dirname(os.path.dirname(__file__))
            )
            new_go_restore_cmd = "{}/pghoard_postgres_command_go --mode restore".format(
                os.path.dirname(os.path.dirname(__file__))
            )

            if conn.server_version >= 120000:
                target_recovery_conf = "postgresql.auto.conf"
                # PG 12+: indicate the server should start up in targeted recovery mode
                with open(os.path.join(backup_out, "recovery.signal"), "w"):
                    pass
            else:
                target_recovery_conf = "recovery.conf"

            with open(os.path.join(backup_out, target_recovery_conf), "r+") as fp:
                rconf = fp.read()
                rconf = rconf.replace("pghoard_postgres_command_go --mode restore", new_go_restore_cmd)
                rconf = rconf.replace("pghoard_postgres_command --mode restore", new_py_restore_cmd)
                fp.seek(0)
                fp.write(rconf)

            r_db = PGTester(pgdata=backup_out, pg_version=pg_version)
            r_db.user = dict(db.user, host=backup_out)
            r_db.run_pg()

            # Wait for PG to start up
            start_time = time.monotonic()
            while True:
                try:
                    r_conn = r_db.connect()
                    break
                except psycopg2.OperationalError as ex:
                    if "starting up" in str(ex) or "not yet accepting connections" in str(ex):
                        assert time.monotonic() - start_time <= 10
                        time.sleep(1)
                    else:
                        raise

            r_cursor = r_conn.cursor()
            # Make sure the tablespace is defined and points to the right (new) path
            r_cursor.execute("SELECT oid, pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname = 'tstest'")
            r_res = r_cursor.fetchone()
            assert r_res[1] == backup_ts_out

            # We should be able to read from the table in the tablespace and the values should match what we stored before
            r_cursor.execute("SELECT id FROM tstest")
            r_res = r_cursor.fetchall()
            cursor.execute("SELECT id FROM tstest")
            orig_res = cursor.fetchall()
            assert r_res == orig_res

        finally:
            if r_conn:
                r_conn.close()
            if r_db:
                r_db.kill(force=True)
            cursor.execute("DROP TABLE IF EXISTS tstest")
            cursor.execute("DROP TABLESPACE tstest")
            conn.close()

    def test_handle_site_create_backup_ignoring_failures(self, pghoard):
        site_config = deepcopy(pghoard.config["backup_sites"][pghoard.test_site])
        assert pghoard.basebackups == {}
        utc_now_dt = datetime.datetime.now(datetime.timezone.utc)
        pghoard.delta_backup_failures[pghoard.test_site] = DeltaBaseBackupFailureInfo(
            retries=site_config["basebackup_delta_mode_max_retries"] + 1, last_failed_time=utc_now_dt
        )

        # skip - too many errors
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site not in pghoard.basebackups

        # create "requested" backup
        pghoard.requested_basebackup_sites.add(pghoard.test_site)
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site in pghoard.basebackups

        # last failed attempt was more than "basebackup_interval_hours" ago - create backup
        pghoard.basebackups = {}
        last_failed_time = utc_now_dt - datetime.timedelta(hours=site_config["basebackup_interval_hours"], seconds=5)
        pghoard.delta_backup_failures[pghoard.test_site].last_failed_time = last_failed_time
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site in pghoard.basebackups

    def test_handle_site(self, pghoard):
        site_config = deepcopy(pghoard.config["backup_sites"][pghoard.test_site])
        site_config["basebackup_interval_hours"] = 1 / 3600
        assert pghoard.basebackups == {}

        # initialize with a single backup
        backup_start = time.monotonic()
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site in pghoard.basebackups
        # wait for backup to complete and put the event back in so pghoard finds it, too
        pghoard.basebackups_callbacks[pghoard.test_site].put(pghoard.basebackups_callbacks[pghoard.test_site].get())

        # adjust basebackup interval to be slightly longer than what this
        # basebackup took and make sure it's not retriggered
        # We save some buffer by using two times the backup_duration
        backup_duration = max((time.monotonic() - backup_start), 1)
        site_config["basebackup_interval_hours"] = (backup_duration * 2) / 3600
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site not in pghoard.basebackups

        # create a new backup now that we have some state
        time.sleep(backup_duration * 2 + 1)
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site in pghoard.basebackups
        # wait for backup to complete and put the event back in so pghoard finds it, too
        pghoard.basebackups_callbacks[pghoard.test_site].put(pghoard.basebackups_callbacks[pghoard.test_site].get())
        # now call handle_site so it notices the backup has finished (this must not start a new one)
        # We should be fine since the interval is at least two times the
        # duration of one backup, so even if this backup took longer than
        # expected it should still not start a new one.
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site not in pghoard.basebackups
        first_basebackups = pghoard.state["backup_sites"][pghoard.test_site]["basebackups"]
        assert first_basebackups[0]["metadata"]["backup-reason"] == BackupReason.scheduled
        assert first_basebackups[0]["metadata"]["backup-decision-time"]
        assert first_basebackups[0]["metadata"]["normalized-backup-time"] is None
        first_time_of_check = pghoard.time_of_last_backup_check[pghoard.test_site]

        # reset the timer to something more sensible and make sure we don't trigger any new basebackups
        site_config["basebackup_interval_hours"] = 1
        pghoard.time_of_last_backup_check[pghoard.test_site] = 0
        time.sleep(1)
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site not in pghoard.basebackups

        second_basebackups = pghoard.state["backup_sites"][pghoard.test_site]["basebackups"]
        second_time_of_check = pghoard.time_of_last_backup_check[pghoard.test_site]
        assert second_basebackups == first_basebackups
        assert second_time_of_check > first_time_of_check

        # create another backup by using the triggering mechanism
        pghoard.requested_basebackup_sites.add(pghoard.test_site)
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site in pghoard.basebackups
        # again, let pghoard notice the backup is done
        pghoard.basebackups_callbacks[pghoard.test_site].put(pghoard.basebackups_callbacks[pghoard.test_site].get())
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site not in pghoard.basebackups

        third_basebackups = pghoard.state["backup_sites"][pghoard.test_site]["basebackups"]
        third_time_of_check = pghoard.time_of_last_backup_check[pghoard.test_site]
        assert third_basebackups != second_basebackups
        assert third_time_of_check > second_time_of_check

        # call handle_site yet again - nothing should happen and no timestamps should be updated
        time.sleep(1)
        pghoard.handle_site(pghoard.test_site, site_config)
        assert pghoard.test_site not in pghoard.basebackups

        fourth_basebackups = pghoard.state["backup_sites"][pghoard.test_site]["basebackups"]
        fourth_time_of_check = pghoard.time_of_last_backup_check[pghoard.test_site]
        assert fourth_basebackups == third_basebackups
        assert fourth_time_of_check == third_time_of_check

        pghoard.write_backup_state_to_json_file()

    def test_get_new_backup_details(self, pghoard):
        now = datetime.datetime.now(datetime.timezone.utc).replace(hour=15, minute=20, second=30, microsecond=0)

        site = pghoard.test_site
        pghoard.set_state_defaults(site)
        site_config = pghoard.config["backup_sites"][site]

        # No backups, one should be created. No backup schedule defined so normalized backup time is None
        metadata = pghoard.get_new_backup_details(now=now, site=pghoard.test_site, site_config=site_config)
        assert metadata
        assert metadata["backup-reason"] == BackupReason.scheduled
        assert metadata["backup-decision-time"] == now.isoformat()
        assert metadata["normalized-backup-time"] is None

        # No backups, one should be created. Backup schedule defined so normalized backup time is set
        site_config["basebackup_hour"] = 13
        site_config["basebackup_minute"] = 10
        metadata = pghoard.get_new_backup_details(now=now, site=pghoard.test_site, site_config=site_config)
        assert metadata
        assert metadata["backup-reason"] == BackupReason.scheduled
        assert metadata["backup-decision-time"] == now.isoformat()
        assert "T13:10:00+00:00" in metadata["normalized-backup-time"]

        # No backups, one should be created. Backup schedule defined so normalized backup time is set
        site_config["basebackup_interval_hours"] = 1.5
        metadata = pghoard.get_new_backup_details(now=now, site=pghoard.test_site, site_config=site_config)
        assert metadata
        assert metadata["backup-reason"] == BackupReason.scheduled
        assert metadata["backup-decision-time"] == now.isoformat()
        assert "T14:40:00+00:00" in metadata["normalized-backup-time"]

        pghoard.state["backup_sites"][site]["basebackups"].append({
            "metadata": {
                "start-time": now - datetime.timedelta(hours=1),
                "backup-decision-time": now - datetime.timedelta(hours=1),
                "backup-reason": BackupReason.scheduled,
                "normalized-backup-time": metadata["normalized-backup-time"],
            },
            "name": "name01",
        })

        # A backup already exists. Current time yields the same normalized backup time so no new one is created
        assert not pghoard.get_new_backup_details(now=now, site=pghoard.test_site, site_config=site_config)

        # A backup already exists. Current time yields different normalized backup time so new one is created
        now2 = now + datetime.timedelta(hours=1)
        metadata = pghoard.get_new_backup_details(now=now2, site=pghoard.test_site, site_config=site_config)
        assert metadata
        assert metadata["backup-reason"] == BackupReason.scheduled
        assert metadata["backup-decision-time"] == now2.isoformat()
        assert "T16:10:00+00:00" in metadata["normalized-backup-time"]

        # A backup already exists. Current time yields different normalized backup time but not enough time has
        # elapsed since the last backup so no new one is created
        site_config["basebackup_interval_hours"] = 12
        site_config["basebackup_hour"] = 14
        site_config["basebackup_minute"] = 50
        assert not pghoard.get_new_backup_details(now=now2, site=pghoard.test_site, site_config=site_config)

        # A backup already exists. Current time yields different normalized backup time and enough time has
        # elapsed since the last backup so new one is created
        now3 = now + datetime.timedelta(hours=7)
        metadata = pghoard.get_new_backup_details(now=now3, site=pghoard.test_site, site_config=site_config)
        assert metadata
        assert metadata["backup-reason"] == BackupReason.scheduled
        assert metadata["backup-decision-time"] == now3.isoformat()
        assert "T14:50:00+00:00" in metadata["normalized-backup-time"]

        # Having manual backup that is very recent doesn't prevent new scheduled backup from being created
        # so long as the normalized-backup-time of the manual backup differs
        pghoard.state["backup_sites"][site]["basebackups"].append({
            "metadata": {
                "start-time": now3 - datetime.timedelta(hours=1),
                "backup-decision-time": now - datetime.timedelta(hours=1),
                "backup-reason": BackupReason.requested,
                "normalized-backup-time": metadata["normalized-backup-time"] + "different",
            },
            "name": "name02",
        })
        metadata2 = pghoard.get_new_backup_details(now=now3, site=pghoard.test_site, site_config=site_config)
        assert metadata2 == metadata

        # normalized-backup-time of requested backup is the same as current normalized backup time so new backup is
        # not created
        pghoard.state["backup_sites"][site]["basebackups"][-1]["metadata"]["normalized-backup-time"] = \
            metadata["normalized-backup-time"]
        assert not pghoard.get_new_backup_details(now=now3, site=pghoard.test_site, site_config=site_config)

        # New manual backups are always created
        pghoard.requested_basebackup_sites.add(site)
        metadata2 = pghoard.get_new_backup_details(now=now3, site=pghoard.test_site, site_config=site_config)
        assert metadata2
        assert metadata2["backup-reason"] == BackupReason.requested
        assert metadata2["backup-decision-time"] == now3.isoformat()
        assert metadata2["normalized-backup-time"] == metadata["normalized-backup-time"]

    def test_patch_basebackup_info(self, pghoard):
        now = datetime.datetime.now(datetime.timezone.utc)
        site_config = {
            "basebackup_hour": 10,
            "basebackup_interval_hours": 24,
            "basebackup_minute": 20,
        }
        entry = {
            "name": "foo/bar",
            "metadata": {
                "start-time": now.isoformat(),
            }
        }
        pghoard.patch_basebackup_info(entry=entry, site_config=site_config)
        assert entry["name"] == "bar"
        assert entry["metadata"]["start-time"] == now
        assert entry["metadata"]["backup-reason"] == BackupReason.scheduled
        assert entry["metadata"]["backup-decision-time"] == now
        assert isinstance(entry["metadata"]["normalized-backup-time"], str)

        entry = {
            "name": "foo/bar",
            "metadata": {
                "start-time": now.isoformat(),
                "backup-decision-time": (now - datetime.timedelta(seconds=30)).isoformat(),
                "backup-reason": BackupReason.requested,
                "normalized-backup-time": None,
            }
        }
        pghoard.patch_basebackup_info(entry=entry, site_config=site_config)
        assert entry["name"] == "bar"
        assert entry["metadata"]["start-time"] == now
        assert entry["metadata"]["backup-reason"] == BackupReason.requested
        assert entry["metadata"]["backup-decision-time"] == now - datetime.timedelta(seconds=30)
        assert entry["metadata"]["normalized-backup-time"] is None

    def test_run_safe_unsupported_basebackup(self):
        config = SIMPLE_BACKUP_CONFIG.copy()
        config["backup_sites"]["foosite"]["basebackup_mode"] = "non-existing"
        callback_queue = CallbackQueue()
        callback_queue.put = Mock()
        pgb = PGBaseBackup(
            config=SIMPLE_BACKUP_CONFIG,
            site="foosite",
            connection_info=None,
            callback_queue=callback_queue,
            basebackup_path=None,
            compression_queue=None,
            storage=None,
            transfer_queue=None,
            metrics=metrics.Metrics(statsd={})
        )
        pgb.run_safe()
        callback_queue.put.assert_called_once_with(CallbackEvent(success=False, exception=ANY))

    def test_fetch_all_data_files_hashes(self):
        pgb = PGBaseBackup(
            config=SIMPLE_BACKUP_CONFIG,
            site="foosite",
            connection_info=None,
            callback_queue=None,
            basebackup_path=None,
            compression_queue=None,
            storage=None,
            transfer_queue=None,
            metrics=metrics.Metrics(statsd={})
        )

        def fake_download_backup_meta_file(basebackup_path: str, **kwargs):  # pylint: disable=unused-argument
            meta = {}
            # Local base backup name includes path to the backup file
            name_prefix = os.path.join(SIMPLE_BACKUP_CONFIG["backup_sites"]["foosite"]["prefix"], "basebackup")
            if basebackup_path == os.path.join(name_prefix, "backup2"):
                meta = {
                    "delta_stats": {
                        "hashes": {
                            "8ee55c458dde7fd7ea43b946dfb3c9713a360280ee2927e600b9d6d4630ef3fd": 1636,
                            "7e0c70d50c0ccd9ca4cb8c6837fbfffb4ef7e885aa1c6370fcfc307541a03e27": 8192,
                        }
                    }
                }
            elif basebackup_path == os.path.join(name_prefix, "backup3"):
                meta = {
                    "delta_stats": {
                        "hashes": {
                            "8ee55c458dde7fd7ea43b946dfb3c9713a360280ee2927e600b9d6d4630ef3fd": 1636,
                            "7e0c70d50c0ccd9ca4cb8c6837fbfffb4ef7e885aa1c6370fcfc307541a03e28": 800,
                        }
                    }
                }
            elif basebackup_path == os.path.join(name_prefix, "backup4"):
                meta = {"delta_stats": {"hashes": {}}}

            return meta, b"some content"

        with patch.object(pgb, "get_remote_basebackups_info") as mock_get_remote_basebackups_info, \
                patch("pghoard.basebackup.base.download_backup_meta_file", new=fake_download_backup_meta_file):
            mock_get_remote_basebackups_info.return_value = [{
                "name": f"backup{idx}",
                "metadata": {
                    "format": bb_format
                }
            } for idx, bb_format in enumerate([BaseBackupFormat.v1] + [BaseBackupFormat.v2] * 3 +
                                              [BaseBackupFormat.delta_v1, BaseBackupFormat.delta_v2])]
            res = pgb.fetch_all_data_files_hashes()
            assert res == {
                "8ee55c458dde7fd7ea43b946dfb3c9713a360280ee2927e600b9d6d4630ef3fd": 1636,
                "7e0c70d50c0ccd9ca4cb8c6837fbfffb4ef7e885aa1c6370fcfc307541a03e27": 8192,
                "7e0c70d50c0ccd9ca4cb8c6837fbfffb4ef7e885aa1c6370fcfc307541a03e28": 800
            }

    @pytest.mark.parametrize(
        "backup_mode",
        [BaseBackupMode.local_tar, BaseBackupMode.delta, BaseBackupMode.local_tar_delta_stats],
    )
    def test_create_basebackup_lost_pg_connection(self, db, pghoard, backup_mode: BaseBackupMode):
        with patch("pghoard.basebackup.base.check_if_pg_connection_is_alive", return_value=False):
            pghoard.create_backup_site_paths(pghoard.test_site)
            basebackup_path = os.path.join(pghoard.config["backup_location"], pghoard.test_site, "basebackup")
            q: Queue[CallbackEvent] = Queue()

            pghoard.config["backup_sites"][pghoard.test_site]["basebackup_mode"] = backup_mode
            pghoard.config["backup_sites"][pghoard.test_site]["active_backup_mode"] = "archive_command"

            now = datetime.datetime.now(datetime.timezone.utc)
            metadata = {
                "backup-reason": BackupReason.scheduled,
                "backup-decision-time": now.isoformat(),
                "normalized-backup-time": now.isoformat(),
            }
            pghoard.create_basebackup(pghoard.test_site, db.user, basebackup_path, q, metadata)
            result = q.get(timeout=60)

            assert result.success is False
            assert result.exception and isinstance(result.exception, RuntimeError)
            assert result.exception.args[0] == "ERROR: PostgreSQL connection was lost during backup process."
