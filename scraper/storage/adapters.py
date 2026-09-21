"""
Archive target adapters (Amendment §19; Section 9A): google_drive, dropbox, onedrive,
s3_compatible, sftp, smb, local_path.

Every adapter implements the same small surface. Adapters never delete or overwrite: `put`
refuses when the key already exists (write-once is enforced here AND in the ledger). Client
libraries are imported lazily so the main stack starts without them; a missing dependency is a
per-target failure, never a service failure. These adapters are the only code that may talk to
internal/private storage hosts; they do not pass through the SSRF guard for externally
discovered URLs because their destinations are operator-configured.
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
import posixpath
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ArchiveError(RuntimeError):
    pass


class ObjectExists(ArchiveError):
    """Write-once violation: the key is already present."""


class ArchiveAdapter:
    target_type = "base"

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.root = (config.get("root") or config.get("root_path") or "").strip("/")

    def _key(self, key: str) -> str:
        key = key.strip("/")
        return posixpath.join(self.root, key) if self.root else key

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def size(self, key: str) -> Optional[int]:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def list(self, prefix: str) -> List[str]:
        raise NotImplementedError

    def ensure_tree(self, key: str) -> None:
        """Create parent folders where the backend needs them (no-op for object stores)."""
        return None

    def check(self) -> Dict[str, Any]:
        try:
            self.list("_index")
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}


# --------------------------------------------------------------------------- local_path
class LocalPathAdapter(ArchiveAdapter):
    target_type = "local_path"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        base = config.get("path") or config.get("root_path") or config.get("root")
        if not base:
            raise ArchiveError("local_path target requires 'path'")
        self.base = pathlib.Path(base)
        self.root = ""

    def _path(self, key: str) -> pathlib.Path:
        p = (self.base / key.strip("/")).resolve()
        if self.base.resolve() not in p.parents and p != self.base.resolve():
            raise ArchiveError("key escapes the archive root")
        return p

    def ensure_tree(self, key: str) -> None:
        self._path(key).parent.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        p = self._path(key)
        if p.exists():
            raise ObjectExists(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_bytes(data)
        # O_EXCL-style: fail if someone wrote it meanwhile
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            raise ObjectExists(key)
        os.close(fd)
        os.replace(tmp, p)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def size(self, key: str) -> Optional[int]:
        p = self._path(key)
        return p.stat().st_size if p.exists() else None

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def list(self, prefix: str) -> List[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return [str(p.relative_to(self.base)) for p in base.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------- s3_compatible
class S3Adapter(ArchiveAdapter):
    target_type = "s3_compatible"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        import boto3  # lazy

        self.bucket = config["bucket"]
        self.client = boto3.client(
            "s3",
            endpoint_url=config.get("endpoint") or None,
            aws_access_key_id=config.get("access_key"),
            aws_secret_access_key=config.get("secret_key"),
            region_name=config.get("region") or None,
        )

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        k = self._key(key)
        if self.exists(key):
            raise ObjectExists(key)
        self.client.put_object(Bucket=self.bucket, Key=k, Body=data, ContentType=content_type, IfNoneMatch="*")

    def exists(self, key: str) -> bool:
        return self.size(key) is not None

    def size(self, key: str) -> Optional[int]:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return int(head["ContentLength"])
        except Exception:
            return None

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def list(self, prefix: str) -> List[str]:
        out: List[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                out.append(k[len(self.root) + 1 :] if self.root and k.startswith(self.root + "/") else k)
        return out


# --------------------------------------------------------------------------- sftp
class SFTPAdapter(ArchiveAdapter):
    target_type = "sftp"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        import paramiko  # lazy

        self.transport = paramiko.Transport((config["host"], int(config.get("port", 22))))
        if config.get("private_key"):
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(config["private_key"]))
            self.transport.connect(username=config["user"], pkey=pkey)
        else:
            self.transport.connect(username=config["user"], password=config.get("password"))
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)

    def ensure_tree(self, key: str) -> None:
        parts = self._key(key).split("/")[:-1]
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}" if cur else part
            try:
                self.sftp.stat(cur)
            except IOError:
                self.sftp.mkdir(cur)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self.exists(key):
            raise ObjectExists(key)
        self.ensure_tree(key)
        k = self._key(key)
        with self.sftp.open(k + ".part", "wb") as f:
            f.write(data)
        self.sftp.rename(k + ".part", k)

    def exists(self, key: str) -> bool:
        return self.size(key) is not None

    def size(self, key: str) -> Optional[int]:
        try:
            return int(self.sftp.stat(self._key(key)).st_size)
        except IOError:
            return None

    def get(self, key: str) -> bytes:
        with self.sftp.open(self._key(key), "rb") as f:
            return f.read()

    def list(self, prefix: str) -> List[str]:
        out: List[str] = []

        def walk(path: str, rel: str) -> None:
            try:
                for entry in self.sftp.listdir_attr(path):
                    p = f"{path}/{entry.filename}"
                    r = f"{rel}/{entry.filename}" if rel else entry.filename
                    import stat as st

                    if st.S_ISDIR(entry.st_mode):
                        walk(p, r)
                    else:
                        out.append(r)
            except IOError:
                return

        walk(self._key(prefix), prefix.strip("/"))
        return out


# --------------------------------------------------------------------------- smb
class SMBAdapter(ArchiveAdapter):
    target_type = "smb"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        import smbclient  # lazy (smbprotocol)

        self.smbclient = smbclient
        self.server = config["server"]
        self.share = config["share"]
        smbclient.register_session(self.server, username=config.get("user"), password=config.get("password"))

    def _unc(self, key: str) -> str:
        return rf"\\{self.server}\{self.share}\{self._key(key).replace('/', chr(92))}"

    def ensure_tree(self, key: str) -> None:
        parent = posixpath.dirname(self._key(key))
        if parent:
            self.smbclient.makedirs(rf"\\{self.server}\{self.share}\{parent.replace('/', chr(92))}", exist_ok=True)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self.exists(key):
            raise ObjectExists(key)
        self.ensure_tree(key)
        with self.smbclient.open_file(self._unc(key), mode="xb") as f:
            f.write(data)

    def exists(self, key: str) -> bool:
        return self.size(key) is not None

    def size(self, key: str) -> Optional[int]:
        try:
            return int(self.smbclient.stat(self._unc(key)).st_size)
        except Exception:
            return None

    def get(self, key: str) -> bytes:
        with self.smbclient.open_file(self._unc(key), mode="rb") as f:
            return f.read()

    def list(self, prefix: str) -> List[str]:
        out: List[str] = []
        base = self._unc(prefix)
        try:
            for root, _dirs, files in self.smbclient.walk(base):
                for fn in files:
                    full = root + "\\" + fn
                    share_prefix = "\\\\" + self.server + "\\" + self.share + "\\"
                    rel = full[len(share_prefix) :].replace("\\", "/")
                    if self.root and rel.startswith(self.root + "/"):
                        rel = rel[len(self.root) + 1 :]
                    out.append(rel)
        except Exception:
            return out
        return out


# --------------------------------------------------------------------------- dropbox
class DropboxAdapter(ArchiveAdapter):
    target_type = "dropbox"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        import dropbox  # lazy

        self.dbx = dropbox.Dropbox(config["token"])
        self._dropbox = dropbox

    def _path(self, key: str) -> str:
        return "/" + self._key(key)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self.exists(key):
            raise ObjectExists(key)
        self.dbx.files_upload(data, self._path(key), mode=self._dropbox.files.WriteMode.add, autorename=False, mute=True)

    def exists(self, key: str) -> bool:
        return self.size(key) is not None

    def size(self, key: str) -> Optional[int]:
        try:
            md = self.dbx.files_get_metadata(self._path(key))
            return int(getattr(md, "size", 0))
        except Exception:
            return None

    def get(self, key: str) -> bytes:
        _md, resp = self.dbx.files_download(self._path(key))
        return resp.content

    def list(self, prefix: str) -> List[str]:
        out: List[str] = []
        try:
            res = self.dbx.files_list_folder(self._path(prefix), recursive=True)
        except Exception:
            return out
        while True:
            for e in res.entries:
                if isinstance(e, self._dropbox.files.FileMetadata):
                    p = e.path_display.lstrip("/")
                    out.append(p[len(self.root) + 1 :] if self.root and p.startswith(self.root + "/") else p)
            if not res.has_more:
                break
            res = self.dbx.files_list_folder_continue(res.cursor)
        return out


# --------------------------------------------------------------------------- google_drive
class GoogleDriveAdapter(ArchiveAdapter):
    target_type = "google_drive"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        import json

        from google.oauth2 import service_account  # lazy
        from googleapiclient.discovery import build

        creds_json = config.get("service_account_json")
        if not creds_json:
            raise ArchiveError("google_drive target requires service_account_json")
        info = json.loads(creds_json) if isinstance(creds_json, str) and creds_json.strip().startswith("{") else None
        if info is None:
            creds = service_account.Credentials.from_service_account_file(creds_json, scopes=["https://www.googleapis.com/auth/drive"])
        else:
            creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive"])
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.root_folder_id = config.get("folder_id") or config.get("root_folder_id")
        if not self.root_folder_id:
            raise ArchiveError("google_drive target requires folder_id")
        self.chunk_size_bytes = max(1, int(config.get("chunk_size_mb", 8))) * 1024 * 1024
        self.api_retries = max(0, int(config.get("api_retries", 5)))
        self._folder_cache: Dict[str, str] = {"": self.root_folder_id}

    def _execute(self, request):
        return request.execute(num_retries=self.api_retries)

    def _folder_for(self, path: str, create: bool) -> Optional[str]:
        path = path.strip("/")
        if path in self._folder_cache:
            return self._folder_cache[path]
        parent_path, _, name = path.rpartition("/")
        parent = self._folder_for(parent_path, create)
        if parent is None:
            return None
        q = f"name = '{name.replace(chr(39), chr(92) + chr(39))}' and '{parent}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = self._execute(self.service.files().list(q=q, fields="files(id)", pageSize=1))
        files = res.get("files", [])
        if files:
            fid = files[0]["id"]
        elif create:
            fid = self._execute(
                self.service.files().create(
                    body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]},
                    fields="id",
                )
            )["id"]
        else:
            return None
        self._folder_cache[path] = fid
        return fid

    def _file_id(self, key: str) -> Optional[str]:
        k = self._key(key)
        folder, _, name = k.rpartition("/")
        parent = self._folder_for(folder, create=False)
        if parent is None:
            return None
        q = f"name = '{name.replace(chr(39), chr(92) + chr(39))}' and '{parent}' in parents and trashed = false"
        files = self._execute(self.service.files().list(q=q, fields="files(id,size)", pageSize=1)).get("files", [])
        return files[0]["id"] if files else None

    def ensure_tree(self, key: str) -> None:
        folder, _, _ = self._key(key).rpartition("/")
        self._folder_for(folder, create=True)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        from googleapiclient.http import MediaIoBaseUpload

        if self.exists(key):
            raise ObjectExists(key)
        k = self._key(key)
        folder, _, name = k.rpartition("/")
        parent = self._folder_for(folder, create=True)
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type, resumable=True, chunksize=self.chunk_size_bytes)
        self._execute(self.service.files().create(body={"name": name, "parents": [parent]}, media_body=media, fields="id"))

    def exists(self, key: str) -> bool:
        return self._file_id(key) is not None

    def size(self, key: str) -> Optional[int]:
        fid = self._file_id(key)
        if fid is None:
            return None
        meta = self._execute(self.service.files().get(fileId=fid, fields="size"))
        return int(meta.get("size", 0))

    def get(self, key: str) -> bytes:
        fid = self._file_id(key)
        if fid is None:
            raise ArchiveError(f"missing {key}")
        return self._execute(self.service.files().get_media(fileId=fid))

    def list(self, prefix: str) -> List[str]:
        out: List[str] = []
        start = self._folder_for(self._key(prefix), create=False)
        if start is None:
            return out

        def walk(folder_id: str, rel: str) -> None:
            page = None
            while True:
                res = self._execute(
                    self.service.files().list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields="nextPageToken, files(id,name,mimeType)",
                        pageToken=page,
                        pageSize=200,
                    )
                )
                for f in res.get("files", []):
                    r = f"{rel}/{f['name']}" if rel else f["name"]
                    if f["mimeType"] == "application/vnd.google-apps.folder":
                        walk(f["id"], r)
                    else:
                        out.append(r)
                page = res.get("nextPageToken")
                if not page:
                    break

        walk(start, prefix.strip("/"))
        return out


# --------------------------------------------------------------------------- onedrive (Microsoft Graph)
class OneDriveAdapter(ArchiveAdapter):
    target_type = "onedrive"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        import httpx  # lazy

        self.token = config["token"]
        self.drive = config.get("drive_id")
        base = f"https://graph.microsoft.com/v1.0/drives/{self.drive}" if self.drive else "https://graph.microsoft.com/v1.0/me/drive"
        self.client = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {self.token}"}, timeout=120)

    def _item(self, key: str) -> str:
        return f"/root:/{self._key(key)}"

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self.exists(key):
            raise ObjectExists(key)
        r = self.client.put(f"{self._item(key)}:/content?@microsoft.graph.conflictBehavior=fail", content=data, headers={"Content-Type": content_type})
        if r.status_code == 409:
            raise ObjectExists(key)
        r.raise_for_status()

    def exists(self, key: str) -> bool:
        return self.size(key) is not None

    def size(self, key: str) -> Optional[int]:
        r = self.client.get(self._item(key))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return int(r.json().get("size", 0))

    def get(self, key: str) -> bytes:
        r = self.client.get(f"{self._item(key)}:/content", follow_redirects=True)
        r.raise_for_status()
        return r.content

    def list(self, prefix: str) -> List[str]:
        out: List[str] = []

        def walk(path: str, rel: str) -> None:
            r = self.client.get(f"/root:/{path}:/children")
            if r.status_code == 404:
                return
            r.raise_for_status()
            for item in r.json().get("value", []):
                rr = f"{rel}/{item['name']}" if rel else item["name"]
                if "folder" in item:
                    walk(f"{path}/{item['name']}", rr)
                else:
                    out.append(rr)

        walk(self._key(prefix), prefix.strip("/"))
        return out


ADAPTERS = {
    "local_path": LocalPathAdapter,
    "s3_compatible": S3Adapter,
    "sftp": SFTPAdapter,
    "smb": SMBAdapter,
    "dropbox": DropboxAdapter,
    "google_drive": GoogleDriveAdapter,
    "onedrive": OneDriveAdapter,
}


def build_adapter(target_type: str, config: Dict[str, Any]) -> ArchiveAdapter:
    if target_type not in ADAPTERS:
        raise ArchiveError(f"unknown archive target type {target_type}")
    return ADAPTERS[target_type](config)
