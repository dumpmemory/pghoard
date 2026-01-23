"""
Copyright (c) 2022 Aiven Ltd
See LICENSE for details
"""
import datetime
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from requests import Session
from requests.auth import HTTPBasicAuth
from rohmu import BaseTransfer, dates, get_transfer

from pghoard import common


class BaseBackupInfo:
    def __init__(self, name: str, site: str, data: dict[str, Any]):
        self.name = name
        self.data = data
        self.site = site


class BaseBackupInfoFromBucket(BaseBackupInfo):
    def __init__(self, name: str, site: str, prefix: str, storage: BaseTransfer[Any], data: dict[str, Any]):
        super().__init__(data=data, name=name, site=site)
        self.storage = storage
        self.prefix = prefix
        self.log = logging.getLogger(self.__class__.__name__)

    def get_basebackup_metadata(self):
        return self.storage.get_metadata_for_key(self.name)

    def get_basebackup_file_to_fileobj(self, fileobj, *, progress_callback=None):
        return self.storage.get_contents_to_fileobj(self.name, fileobj, progress_callback=progress_callback)

    def request_backup_preservation(self, preserve_until: datetime.datetime) -> str:
        backup_name = Path(self.name).name
        request_name = f"{backup_name}_{preserve_until}"
        request_path = os.path.join(self.prefix, "preservation_request", request_name)
        self.storage.store_file_from_memory(
            request_path, b"", {
                "preserve-backup": backup_name,
                "preserve-until": str(preserve_until)
            }
        )
        return request_name

    def try_request_backup_preservation(self, preserve_until: datetime.datetime) -> Optional[str]:
        try:
            return self.request_backup_preservation(preserve_until)
        except Exception:  # pylint: disable=broad-except
            # rohmu does not wrap storage implementation errors in high-level errors:
            # we can't catch something more specific like "permission denied".
            self.log.exception("Could not request backup preservation")
            return None

    def try_cancel_backup_preservation(self, request_name: str) -> None:
        try:
            self.cancel_backup_preservation(request_name)
        except Exception:  # pylint: disable=broad-except
            # rohmu does not wrap storage implementation errors in high-level errors:
            # we can't catch something more specific like "permission denied".
            self.log.exception("Could not cancel backup preservation")

    def cancel_backup_preservation(self, request_name: str) -> None:
        request_path = os.path.join(self.prefix, "preservation_request", request_name)
        self.storage.delete_key(request_path)

    def get_file_bytes(self):
        return self.storage.get_contents_to_string(self.name)[0]


class ObjectStore(ABC):
    def __init__(self, pgdata: Any, sites: list[str]):
        self.log = logging.getLogger(self.__class__.__name__)
        self.pgdata = pgdata
        self.sites = sites

    @abstractmethod
    def list_basebackups(self) -> list[BaseBackupInfoFromBucket] | list[BaseBackupInfo]:
        raise NotImplementedError("list_basebackups is not implemented")

    def show_basebackup_list(self, verbose=True) -> None:
        result = self.list_basebackups()
        caption = "Available %r basebackups:" % self.sites[0]
        print_basebackup_list(list(result), caption=caption, verbose=verbose)


class BucketObjectStore(ObjectStore):
    def __init__(self, config: dict[str, Any], sites: list[str], pgdata: Any = None):
        super().__init__(pgdata=pgdata, sites=sites)
        self.config = config

    def get_site_prefix(self, site):
        return self.config["backup_sites"][site]["prefix"]

    def list_basebackups(self) -> list[BaseBackupInfoFromBucket] | list[BaseBackupInfo]:
        results = []
        for site in self.sites:
            storage_config = common.get_object_storage_config(self.config, site)
            storage = get_transfer(storage_config)
            for result in storage.list_path(os.path.join(self.get_site_prefix(site), "basebackup")):
                results += [
                    BaseBackupInfoFromBucket(
                        storage=storage, name=result["name"], prefix=self.get_site_prefix(site), site=site, data=result
                    )
                ]

        return results


class HTTPRestore(ObjectStore):
    def __init__(self, host: str, port: str, sites: list[str], *, username=None, password=None, pgdata: Any = None):
        super().__init__(pgdata=pgdata, sites=sites)
        self.host = host
        self.port = port
        self.session = Session()
        if username and password:
            self.session.auth = HTTPBasicAuth(username, password)

    def _url(self, path, site: str):
        return f"http://{self.host}:{self.port}/{site}/{path}"

    def list_basebackups(self) -> list[BaseBackupInfoFromBucket] | list[BaseBackupInfo]:
        result: list[BaseBackupInfo] = []
        for site in self.sites:
            response = self.session.get(self._url("basebackup", site))
            for basebackup in response.json()["basebackups"]:
                result.append(BaseBackupInfo(name=basebackup["name"], data=basebackup, site=site))
        return result


def print_basebackup_list(basebackups: list[BaseBackupInfo], *, caption="Available basebackups", verbose=True):
    print(caption, "\n")
    fmt = "{name:40}  {size:>11}  {orig_size:>11}  {time:20}".format
    print(fmt(name="Basebackup", size="Backup size", time="Start time", orig_size="Orig size"))
    print(fmt(name="-" * 40, size="-" * 11, time="-" * 20, orig_size="-" * 11))
    for basebackup in sorted(basebackups, key=lambda b: b.name):
        meta = basebackup.data["metadata"].copy()
        lm = meta.pop("start-time")
        if isinstance(lm, str):
            lm = dates.parse_timestamp(lm)
        if lm.tzinfo:
            lm = lm.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        lm_str = lm.isoformat()[:19] + "Z"  # # pylint: disable=no-member
        size_str = "{} MB".format(int(meta.get("total-size-enc", basebackup.data["size"])) // (1024 ** 2))
        orig_size = int(meta.get("total-size-plain", meta.get("original-file-size")) or 0)
        if orig_size:
            orig_size_str = "{} MB".format(orig_size // (1024 ** 2))
        else:
            orig_size_str = "n/a"
        print(fmt(name=basebackup.name, size=size_str, time=lm_str, orig_size=orig_size_str))
        if verbose:
            print("    metadata:", meta)
