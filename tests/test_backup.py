"""Backup and restore: encryption, integrity checks, the round trip through the web page, retention, OneDrive."""

import io
import json
import zipfile

import httpx
import pytest

from cyfun import backup, onedrive
from cyfun import db as database
from cyfun.config import Settings, get_settings
from cyfun.models import Activity, Organisation


def _settings(**env) -> Settings:
    base = {"CYFUN_SECRET_KEY": get_settings().secret_key, "DATA_DIR": str(get_settings().data_dir)}
    base.update(env)
    return Settings(_env_file=None, **base)


def _encrypt(data: bytes, settings: Settings | None = None) -> bytes:
    out = io.BytesIO()
    backup.encrypt_file(settings or get_settings(), io.BytesIO(data), out)
    return out.getvalue()


def _decrypt(blob: bytes, settings: Settings | None = None) -> bytes:
    out = io.BytesIO()
    backup.decrypt_file(settings or get_settings(), io.BytesIO(blob), out)
    return out.getvalue()


def test_encryption_round_trip_in_chunks(monkeypatch):
    monkeypatch.setattr(backup, "CHUNK", 1000)
    for data in (b"", b"x" * 999, b"y" * 1000, bytes(range(256)) * 20):
        blob = _encrypt(data)
        assert data[:200] not in blob or not data
        assert _decrypt(blob) == data


def test_encryption_detects_tampering_truncation_and_wrong_key(monkeypatch):
    monkeypatch.setattr(backup, "CHUNK", 1000)
    blob = _encrypt(b"z" * 3500)
    flipped = bytearray(blob)
    flipped[-5] ^= 1
    for bad in (bytes(flipped), blob[: len(blob) - 1004], b"not a backup at all"):
        with pytest.raises(backup.BackupError):
            _decrypt(bad)
    with pytest.raises(backup.BackupError, match="different CYFUN_SECRET_KEY"):
        _decrypt(blob, _settings(CYFUN_SECRET_KEY="another-server-key-0123456789-abcdefghij"))
    with pytest.raises(backup.BackupError, match="server key"):
        _encrypt(b"x", _settings(CYFUN_SECRET_KEY=""))


def _crafted(tmp_path, members: dict[str, bytes], manifest_files: dict | None = None):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        files = manifest_files if manifest_files is not None else {n: {"sha256": backup.hashlib.sha256(b).hexdigest()} for n, b in members.items()}
        z.writestr("manifest.json", json.dumps({"format": 1, "files": files}))
        for name, data in members.items():
            z.writestr(name, data)
    path = tmp_path / "crafted.cyfunbak"
    path.write_bytes(_encrypt(raw.getvalue()))
    return path


def test_unpack_refuses_unsafe_or_inconsistent_archives(client, tmp_path):
    db_bytes = get_settings().db_path.read_bytes()
    cases = [
        {backup.DB_MEMBER: db_bytes, "files/evidence/../../evil.txt": b"x"},
        {backup.DB_MEMBER: db_bytes, "files/other/x.txt": b"x"},
        {"files/evidence/a.txt": b"x"},  # no database
        {backup.DB_MEMBER: b"not sqlite"},
    ]
    for i, members in enumerate(cases):
        work = tmp_path / f"w{i}"
        work.mkdir()
        with pytest.raises(backup.BackupError):
            backup.unpack(get_settings(), _crafted(tmp_path, members), work)
    work = tmp_path / "sha"
    work.mkdir()
    with pytest.raises(backup.BackupError, match="Checksum"):
        backup.unpack(get_settings(), _crafted(tmp_path, {backup.DB_MEMBER: db_bytes}, {backup.DB_MEMBER: {"sha256": "0" * 64}}), work)


def test_backup_page_download_and_restore_round_trip(admin, auditor):
    assert auditor.get("/backup", follow_redirects=False).status_code == 403
    assert auditor.get("/backup/download", follow_redirects=False).status_code == 403
    assert admin.get("/backup").status_code == 200

    files = {"file": ("restore-proof.txt", b"evidence kept across restore", "text/plain")}
    admin.post("/assessment/PR.DS-11.1/evidence", data={"title": "Restore proof"}, files=files)
    with database.session() as db:
        db.get(Organisation, 1).name = "Before backup"
        db.commit()

    r = admin.get("/backup/download")
    assert r.status_code == 200 and r.content.startswith(backup.MAGIC)
    assert b"Before backup" not in r.content and b"evidence kept" not in r.content
    blob = r.content

    with database.session() as db:
        db.get(Organisation, 1).name = "Changed after backup"
        db.commit()
    evidence_dir = get_settings().evidence_dir
    for f in evidence_dir.iterdir():
        f.unlink()

    r = admin.post("/backup/restore", data={"confirm": "no"}, files={"file": ("b.cyfunbak", blob)}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    with database.session() as db:
        assert db.get(Organisation, 1).name == "Changed after backup"

    r = admin.post("/backup/restore", data={"confirm": "RESTORE"}, files={"file": ("b.cyfunbak", blob)}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers["location"]
    with database.session() as db:
        assert db.get(Organisation, 1).name == "Before backup"
        restored = db.query(Activity).filter(Activity.action == "backup_restored").order_by(Activity.id.desc()).first()
        assert restored is not None and restored.details["pre_restore_backup"].endswith("-pre-restore.cyfunbak")
    assert any(f.read_bytes() == b"evidence kept across restore" for f in evidence_dir.iterdir())
    assert "Restore proof" in admin.get("/evidence").text

    pre = restored.details["pre_restore_backup"]
    assert admin.get(f"/backup/files/{pre}").content.startswith(backup.MAGIC)
    assert "err=" in admin.get("/backup/files/..%2Fcyfun.sqlite3", follow_redirects=False).headers.get("location", "err=")
    assert backup.local_backup(get_settings(), "../cyfun.sqlite3") is None


def test_wrong_key_restore_changes_nothing(admin, tmp_path):
    other = _settings(CYFUN_SECRET_KEY="another-server-key-0123456789-abcdefghij")
    out = io.BytesIO()
    backup.write_backup(other, out, "test")
    with database.session() as db:
        before = db.query(Activity).count()
    r = admin.post("/backup/restore", data={"confirm": "RESTORE"}, files={"file": ("b.cyfunbak", out.getvalue())}, follow_redirects=False)
    assert "err=" in r.headers["location"] and "different" in r.headers["location"]
    with database.session() as db:
        assert db.query(Activity).count() == before


def test_local_retention_keeps_labelled_copies(client, monkeypatch):
    s = get_settings()
    for p in backup.local_backups(s):
        p.unlink()
    stamps = iter(f"20260101-00000{i}" for i in range(9))
    monkeypatch.setattr(backup, "backup_name", lambda at=None: f"{backup.PREFIX}{next(stamps)}{backup.SUFFIX}")
    backup.save_local(s, "test", 5, label="pre-restore")
    for _ in range(4):
        backup.save_local(s, "test", 2)
    names = [p.name for p in backup.local_backups(s)]
    assert len(names) == 3 and sum(n.endswith("-pre-restore.cyfunbak") for n in names) == 1


def test_scheduled_backup_uploads_to_onedrive_and_prunes(admin, monkeypatch):
    s = get_settings()
    for p in backup.local_backups(s):
        p.unlink()
    from cyfun.appsettings import save_group

    with database.session() as db:
        form = {
            "backup_interval_hours": "24",
            "backup_keep": "2",
            "backup_onedrive_user": "backup@example.test",
            "backup_onedrive_folder": "CyFun/backups",
            "backup_graph_tenant_id": "11111111-1111-1111-1111-111111111111",
            "backup_graph_client_id": "22222222-2222-2222-2222-222222222222",
            "backup_graph_client_secret": "client-secret-value-0123456789",
        }
        changes, errors = save_group(db, s, "backup", form, "test")
        assert not errors and changes

    calls: list[tuple[str, str]] = []
    uploaded = bytearray()
    uploads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.raw_path.decode()))
        path = request.url.path
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok"})
        if request.method == "POST" and path.endswith("/children"):
            return httpx.Response(409 if "CyFun" not in request.content.decode() else 201, json={})
        if path.endswith(":/createUploadSession"):
            uploads.append(path.removesuffix(":/createUploadSession").split("/")[-1])
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(200, json={"uploadUrl": "https://upload.example.test/session"})
        if request.url.host == "upload.example.test":
            assert "Authorization" not in request.headers
            uploaded.extend(request.content)
            return httpx.Response(201, json={})
        if request.method == "GET" and path.endswith(":/children"):
            names = [
                *uploads,
                "cyfun-backup-20250101-000000.cyfunbak",
                "cyfun-backup-20250102-000000.cyfunbak",
                "notes.txt",
                "cyfun-backup-20990101-000000.cyfunbak",
            ]
            return httpx.Response(200, json={"value": [{"id": n, "name": n} for n in names]})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(onedrive.httpx, "post", lambda url, **kw: real_client(transport=transport).post(url, **kw))
    monkeypatch.setattr(onedrive.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))

    r = admin.post("/backup/run", follow_redirects=False)
    assert "msg=" in r.headers["location"]
    with database.session() as db:
        acts = {a.action: a for a in db.query(Activity).filter(Activity.entity == "backup").order_by(Activity.id).all()}
    assert "backup_uploaded" in acts, acts.get("backup_failed") and acts["backup_failed"].details
    assert acts["backup_uploaded"].details["pruned"] == ["cyfun-backup-20250102-000000.cyfunbak", "cyfun-backup-20250101-000000.cyfunbak"]
    newest = backup.local_backups(s)[0]
    assert bytes(uploaded) == newest.read_bytes()
    assert any(p.endswith(f"/CyFun/backups/{newest.name}:/createUploadSession") for _, p in calls)

    with database.session() as db:
        from cyfun.models import AppSetting

        db.query(AppSetting).filter(AppSetting.key.like("backup_%")).delete(synchronize_session=False)
        db.commit()


def test_key_fingerprint_on_page_and_command_line(admin, capsys):
    from cyfun.secretbox import key_id

    assert key_id(get_settings()) in admin.get("/backup").text
    assert backup.main(["key"]) == 0
    assert capsys.readouterr().out.strip() == key_id(get_settings())
