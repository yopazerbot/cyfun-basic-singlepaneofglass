"""Copy backups to a OneDrive for Business folder through Microsoft Graph (application permission).

The app registration needs Files.ReadWrite.All, or Sites.Selected with write access granted on
the target user's OneDrive site only (docs/backup.md). Backup tenant, client ID and secret fall
back to the Microsoft 365 connector's values when left empty.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx

from .backup import UNLABELLED

GRAPH = "https://graph.microsoft.com/v1.0"
PART = 32 * 320 * 1024  # upload sessions take multiples of 320 KiB; 10 MiB per request


class OneDrive:
    def __init__(self, config):
        self.tenant = config.backup_graph_tenant_id or config.ms_graph_tenant_id
        self.client_id = config.backup_graph_client_id or config.ms_graph_client_id
        self.secret = config.backup_graph_client_secret or config.ms_graph_client_secret
        self.user = config.backup_onedrive_user
        self.folder = config.backup_onedrive_folder.strip("/")

    def configured(self) -> bool:
        return bool(self.user and self.folder and self.tenant and self.client_id and self.secret)

    def _client(self) -> httpx.Client:
        r = httpx.post(
            f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=20,
        )
        r.raise_for_status()
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return httpx.Client(base_url=f"{GRAPH}/users/{quote(self.user, safe='@')}/drive", headers=headers, timeout=60)

    def _path(self, *parts: str) -> str:
        return quote("/".join((self.folder, *parts)))

    def test(self) -> str:
        with self._client() as c:
            r = c.get("", params={"$select": "driveType,owner"})
            r.raise_for_status()
            folder = c.get(f"/root:/{self._path()}")
        if folder.status_code == 404:
            return f"OneDrive of {self.user} reached. The folder {self.folder} is created at the first upload."
        folder.raise_for_status()
        return f"OneDrive of {self.user} reached; folder {self.folder} exists."

    def _ensure_folder(self, c: httpx.Client) -> None:
        segs = self.folder.split("/")
        for i, seg in enumerate(segs):
            parent = f"/root:/{quote('/'.join(segs[:i]))}:" if i else "/root"
            r = c.post(f"{parent}/children", json={"name": seg, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
            if r.status_code != 409:  # 409: the folder exists
                r.raise_for_status()

    def upload(self, path: Path) -> None:
        size = path.stat().st_size
        with self._client() as c:
            self._ensure_folder(c)
            r = c.post(f"/root:/{self._path(path.name)}:/createUploadSession", json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
            r.raise_for_status()
            url = r.json()["uploadUrl"]
        # The upload URL is pre-authorised; Graph rejects requests that also carry the bearer token.
        with httpx.Client(timeout=120) as up, path.open("rb") as f:
            start = 0
            while start < size:
                block = f.read(PART)
                end = start + len(block) - 1
                r = up.put(url, content=block, headers={"Content-Range": f"bytes {start}-{end}/{size}"})
                r.raise_for_status()
                start = end + 1

    def prune(self, keep: int) -> list[str]:
        """Delete the oldest scheduled backups in the folder beyond `keep`. Returns the deleted names."""
        with self._client() as c:
            items, url = [], f"/root:/{self._path()}:/children?$select=id,name&$top=200"
            while url:
                r = c.get(url)
                r.raise_for_status()
                data = r.json()
                items += data.get("value", [])
                url = data.get("@odata.nextLink", "")
            ours = sorted((i for i in items if UNLABELLED.fullmatch(i["name"])), key=lambda i: i["name"], reverse=True)
            deleted = []
            for item in ours[max(1, keep) :]:
                c.delete(f"/items/{item['id']}").raise_for_status()
                deleted.append(item["name"])
        return deleted
