"""Backup and restore of the whole application: database (with the Settings page values) and data files.

    docker compose exec app python -m cyfun.backup                       # write a backup to DATA_DIR/backups
    docker compose run --rm app python -m cyfun.backup restore FILE      # with the app container stopped
    docker compose exec app python -m cyfun.backup key                   # fingerprint of the key backups need

A backup is one encrypted file, cyfun-backup-YYYYmmdd-HHMMSS.cyfunbak. Inside is a zip with
manifest.json, a consistent SQLite copy (online backup API) and the evidence files and connector
snapshots. The zip is encrypted with AES-256-GCM in 1 MiB chunks under a key derived from
CYFUN_SECRET_KEY, so a copy on OneDrive or a USB stick is unreadable without the server key. The
same key is needed anyway to read the secrets stored on the Settings page after a restore.
Environment settings (.env) are not part of a backup.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import __version__
from .config import Settings, get_settings
from .secretbox import key_id

MAGIC = b"CYFUNBK1"
FORMAT = 1
CHUNK = 1024 * 1024
SUFFIX = ".cyfunbak"
PREFIX = "cyfun-backup-"
UNLABELLED = re.compile(r"cyfun-backup-\d{8}-\d{6}\.cyfunbak")  # scheduled and manual, not pre-restore copies
DB_MEMBER = "db/cyfun.sqlite3"
DATA_FOLDERS = ("evidence", "connector_snapshots")  # subfolders of DATA_DIR that a backup carries
MAX_UNPACKED = 20 * 1024**3  # refuse archives that would unpack to more than 20 GB
_lock = threading.Lock()  # one backup or restore at a time


class BackupError(Exception):
    pass


# --------------------------------------------------------------------------- encryption
def _key_id(settings: Settings) -> str:
    if settings.secret_key_problem:
        raise BackupError(f"Backups are encrypted with the server key: {settings.secret_key_problem}")
    return key_id(settings)


def _key(settings: Settings, salt: bytes) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"cyfun-backup-v1")
    return hkdf.derive(settings.secret_key.strip().encode("utf-8"))


def _nonce(prefix: bytes, index: int, final: bool) -> bytes:
    return prefix + struct.pack(">IB", index, final)


def encrypt_file(settings: Settings, src: BinaryIO, dst: BinaryIO) -> None:
    """Header: magic, key id (12 bytes), salt (16), nonce prefix (7). Then chunks of length + ciphertext.

    Each chunk's nonce carries its index and a final flag, so reordered, dropped or truncated
    chunks fail to decrypt."""
    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(7)
    header = MAGIC + _key_id(settings).encode("ascii") + salt + prefix
    aead = AESGCM(_key(settings, salt))
    dst.write(header)
    index, block = 0, src.read(CHUNK)
    while True:
        nxt = src.read(CHUNK)
        ct = aead.encrypt(_nonce(prefix, index, not nxt), block, header)
        dst.write(struct.pack(">I", len(ct)) + ct)
        if not nxt:
            return
        index, block = index + 1, nxt


def decrypt_file(settings: Settings, src: BinaryIO, dst: BinaryIO) -> None:
    header = src.read(len(MAGIC) + 12 + 16 + 7)
    if not header.startswith(MAGIC):
        raise BackupError("This is not a CyFun backup file.")
    kid, salt, prefix = header[8:20].decode("ascii", "replace"), header[20:36], header[36:43]
    current = _key_id(settings)
    if kid != current:
        raise BackupError(f"The backup was made with a different CYFUN_SECRET_KEY (key id {kid}, this server {current}).")
    aead = AESGCM(_key(settings, salt))
    index = 0
    while True:
        size = src.read(4)
        if len(size) != 4:
            raise BackupError("The backup file is incomplete.")
        ct = src.read(struct.unpack(">I", size)[0])
        final = _at_end(src)
        try:
            dst.write(aead.decrypt(_nonce(prefix, index, final), ct, header))
        except InvalidTag as exc:
            raise BackupError("The backup file is damaged or incomplete.") from exc
        if final:
            return
        index += 1


def _at_end(f: BinaryIO) -> bool:
    pos = f.tell()
    end = not f.read(1)
    f.seek(pos)
    return end


# --------------------------------------------------------------------------- create
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _data_files(settings: Settings):
    for folder in DATA_FOLDERS:
        root = settings.data_dir / folder
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    yield p, f"files/{p.relative_to(settings.data_dir).as_posix()}"


def write_backup(settings: Settings, dst: BinaryIO, actor: str) -> dict:
    """Write an encrypted backup to dst. Returns the manifest."""
    tmp_dir = settings.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_dir) as work:
        db_copy = Path(work) / "cyfun.sqlite3"
        src_uri = f"file:{settings.db_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(src_uri, uri=True)) as src, closing(sqlite3.connect(db_copy)) as out:
            src.backup(out)
        members = [(db_copy, DB_MEMBER), *_data_files(settings)]
        manifest = {
            "format": FORMAT,
            "app_version": __version__,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "created_by": actor,
            "files": {name: {"sha256": _sha256(p), "size": p.stat().st_size} for p, name in members},
        }
        archive = Path(work) / "backup.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            for p, name in members:
                z.write(p, name)
        with archive.open("rb") as plain:
            encrypt_file(settings, plain, dst)
    return manifest


def backup_name(at: datetime | None = None) -> str:
    return f"{PREFIX}{(at or datetime.now(UTC)).strftime('%Y%m%d-%H%M%S')}{SUFFIX}"


def backups_dir(settings: Settings) -> Path:
    return settings.data_dir / "backups"


def local_backups(settings: Settings) -> list[Path]:
    """Backups on the server, newest first."""
    return sorted(backups_dir(settings).glob(f"{PREFIX}*{SUFFIX}"), reverse=True)


def local_backup(settings: Settings, name: str) -> Path | None:
    """The backup file with this name in the backups folder, or None. Names from requests go through here."""
    if PurePosixPath(name).name != name or not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
        return None
    p = backups_dir(settings) / name
    return p if p.is_file() else None


def save_local(settings: Settings, actor: str, keep: int, label: str = "") -> tuple[Path, dict]:
    """Write a backup to DATA_DIR/backups and keep the newest `keep` scheduled ones (pre-restore copies are kept apart)."""
    out_dir = backups_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = backup_name()
    if label:
        name = name.replace(SUFFIX, f"-{label}{SUFFIX}")
    target = out_dir / name
    partial = target.with_suffix(".partial")
    with _lock:
        with partial.open("wb") as f:
            manifest = write_backup(settings, f, actor)
        partial.replace(target)
    if not label:
        scheduled = [p for p in local_backups(settings) if UNLABELLED.fullmatch(p.name)]
        for old in scheduled[max(1, keep) :]:
            old.unlink(missing_ok=True)
    return target, manifest


# --------------------------------------------------------------------------- restore
def _check_member(name: str) -> None:
    p = PurePosixPath(name)
    if name == "manifest.json" or name == DB_MEMBER:
        return
    if p.is_absolute() or ".." in p.parts or len(p.parts) < 3 or p.parts[0] != "files" or p.parts[1] not in DATA_FOLDERS:
        raise BackupError(f"The backup contains an unexpected entry: {name}")


def unpack(settings: Settings, path: Path, work: Path) -> dict:
    """Decrypt and check a backup into `work`: manifest, entry names, sizes, SHA-256 and SQLite integrity."""
    archive = work / "backup.zip"
    with path.open("rb") as src, archive.open("wb") as dst:
        decrypt_file(settings, src, dst)
    try:
        z = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise BackupError("The backup content is not readable.") from exc
    with z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except (KeyError, ValueError) as exc:
            raise BackupError("The backup has no readable manifest.") from exc
        if manifest.get("format") != FORMAT:
            raise BackupError(f"Backup format {manifest.get('format')} is not supported by this version.")
        infos = z.infolist()
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise BackupError("The backup would unpack to more than 20 GB.")
        expected = manifest.get("files") or {}
        names = {i.filename for i in infos if not i.is_dir()} - {"manifest.json"}
        if names != set(expected) or DB_MEMBER not in names:
            raise BackupError("The backup content does not match its manifest.")
        for info in infos:
            _check_member(info.filename)
            if info.is_dir() or info.filename == "manifest.json":
                continue
            target = work.joinpath(*PurePosixPath(info.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            if _sha256(target) != expected[info.filename]["sha256"]:
                raise BackupError(f"Checksum mismatch for {info.filename}.")
    archive.unlink()
    with closing(sqlite3.connect(work / "db" / "cyfun.sqlite3")) as conn:
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as exc:
            raise BackupError("The database in the backup is not readable.") from exc
    if not ok or not {"user", "activity", "app_setting", "organisation"} <= tables:
        raise BackupError("The database in the backup failed the integrity check.")
    return manifest


def restore(settings: Settings, path: Path, actor: str, keep: int = 14) -> tuple[dict, Path | None]:
    """Replace the database and data files with the backup's content.

    The backup is checked completely before anything changes, and the current state is first
    saved as a "pre-restore" backup. Returns (manifest, pre-restore backup path)."""
    from . import db as database
    from . import scheduler
    from .services import log_activity

    tmp_dir = settings.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_dir) as w:
        work = Path(w)
        manifest = unpack(settings, path, work)
        before = save_local(settings, actor, keep, label="pre-restore")[0] if settings.db_path.exists() else None
        with _lock, scheduler.paused():
            if database._engine is not None:
                database._engine.dispose()  # no pooled connection holds the old database open
            with closing(sqlite3.connect(work / "db" / "cyfun.sqlite3")) as src, closing(sqlite3.connect(settings.db_path)) as dst:
                src.backup(dst)
            for folder in DATA_FOLDERS:
                live, incoming, old = settings.data_dir / folder, work / "files" / folder, settings.data_dir / f".{folder}.old"
                shutil.rmtree(old, ignore_errors=True)
                if live.exists():
                    live.rename(old)
                if incoming.exists():
                    shutil.move(incoming, live)
                else:
                    live.mkdir()
                shutil.rmtree(old, ignore_errors=True)
            if database._engine is not None:
                database._engine.dispose()
                database.create_schema()  # adds the columns a backup from an older version lacks
    if database._engine is not None:
        with database.session() as db:
            details = {"backup_created_at": manifest.get("created_at"), "backup_app_version": manifest.get("app_version")}
            details["pre_restore_backup"] = before.name if before else ""
            log_activity(db, actor, "backup_restored", "backup", path.name[:60], details)
    return manifest, before


# --------------------------------------------------------------------------- command line
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from . import db as database
    from .appsettings import load_config

    if argv == ["key"]:
        try:
            print(_key_id(get_settings()))
        except BackupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if argv and not (argv[0] == "restore" and len(argv) == 2):
        print(__doc__)
        return 2
    s = get_settings()
    database.init_engine(f"sqlite:///{s.db_path.as_posix()}")
    keep = load_config(settings=s).backup_keep
    try:
        if argv:
            manifest, before = restore(s, Path(argv[1]), "command line", keep)
            print(f"restored backup of {manifest['created_at']}; previous state saved as {before}")
            return 0
        target, manifest = save_local(s, "command line", keep)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"backup written: {target} ({target.stat().st_size} bytes, {len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
