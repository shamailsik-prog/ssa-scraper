"""Google Drive archive adapter: OAuth (personal Drive) and service-account (Shared Drive) paths
against an in-memory stand-in for the Drive v3 files() resource."""

from __future__ import annotations

import itertools
import re

import pytest

from scraper.storage.adapters import ArchiveError, GoogleDriveAdapter, ObjectExists

FOLDER = "application/vnd.google-apps.folder"


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Files:
    def __init__(self, root: str):
        self.items = {root: {"id": root, "name": "root", "mimeType": FOLDER, "parents": []}}
        self.data = {}
        self.calls = []
        self._ids = (f"id{n}" for n in itertools.count())

    def _record(self, method, kwargs):
        self.calls.append((method, kwargs))

    def list(self, q, fields, pageSize, pageToken=None, **kwargs):
        self._record("list", kwargs)
        m = re.search(r"'([^']+)' in parents", q)
        parent = m.group(1)
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        want_folder = "mimeType = 'application/vnd.google-apps.folder'" in q

        def run():
            out = [
                {"id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "size": str(len(self.data.get(f["id"], b"")))}
                for f in self.items.values()
                if parent in f["parents"]
                and (name is None or f["name"] == name.group(1).replace("\\'", "'"))
                and (not want_folder or f["mimeType"] == FOLDER)
            ]
            return {"files": out[:pageSize]}

        return _Call(run)

    def create(self, body, fields, media_body=None, **kwargs):
        self._record("create", kwargs)

        def run():
            fid = next(self._ids)
            self.items[fid] = {"id": fid, "name": body["name"], "mimeType": body.get("mimeType", "application/octet-stream"), "parents": body["parents"]}
            if media_body is not None:
                self.data[fid] = media_body.getbytes(0, media_body.size())
            return {"id": fid}

        return _Call(run)

    def get(self, fileId, fields, **kwargs):
        self._record("get", kwargs)
        return _Call(lambda: {"size": str(len(self.data.get(fileId, b"")))})

    def get_media(self, fileId, **kwargs):
        self._record("get_media", kwargs)
        return _Call(lambda: self.data[fileId])


class _Service:
    def __init__(self, root: str):
        self._files = _Files(root)

    def files(self):
        return self._files


def test_put_exists_get_list_and_shared_drive_flags():
    service = _Service("ROOT")
    adapter = GoogleDriveAdapter({"folder_id": "ROOT", "root": "SIKANDER"}, service=service)
    adapter.put("Citations/PLD/2020/PLD_2020_SC_1/judgment.txt", b"full text", "text/plain")
    assert adapter.exists("Citations/PLD/2020/PLD_2020_SC_1/judgment.txt")
    assert adapter.size("Citations/PLD/2020/PLD_2020_SC_1/judgment.txt") == len(b"full text")
    assert adapter.get("Citations/PLD/2020/PLD_2020_SC_1/judgment.txt") == b"full text"
    with pytest.raises(ObjectExists):
        adapter.put("Citations/PLD/2020/PLD_2020_SC_1/judgment.txt", b"again")
    assert adapter.list("Citations") == ["Citations/PLD/2020/PLD_2020_SC_1/judgment.txt"]
    for method, kwargs in service.files().calls:
        assert kwargs.get("supportsAllDrives") is True, method
        if method == "list":
            assert kwargs.get("includeItemsFromAllDrives") is True


def test_oauth_refresh_token_builds_user_credentials(monkeypatch):
    seen = {}

    def fake_build(api, version, credentials, cache_discovery):
        seen["credentials"] = credentials
        return _Service("ROOT")

    monkeypatch.setattr("googleapiclient.discovery.build", fake_build)
    GoogleDriveAdapter({"folder_id": "ROOT", "client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"})
    from google.oauth2.credentials import Credentials

    creds = seen["credentials"]
    assert isinstance(creds, Credentials)
    assert creds.refresh_token == "rtoken" and creds.client_id == "cid" and creds.token_uri == "https://oauth2.googleapis.com/token"


def test_oauth_requires_client_and_missing_auth_is_refused():
    with pytest.raises(ArchiveError, match="client_id"):
        GoogleDriveAdapter({"folder_id": "ROOT", "refresh_token": "rtoken"})
    with pytest.raises(ArchiveError, match="refresh_token"):
        GoogleDriveAdapter({"folder_id": "ROOT"})
    with pytest.raises(ArchiveError, match="folder_id"):
        GoogleDriveAdapter({"refresh_token": "rtoken", "client_id": "c", "client_secret": "s"})
