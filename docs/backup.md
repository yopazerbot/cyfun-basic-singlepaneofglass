# Backup and restore

The **Backup and restore** page (administrators, menu Verify) exports and imports the whole application. The **Scheduled backup** group on the Settings page sets the interval, the number of backups to keep and an optional OneDrive copy.

## What a backup holds

| Included | Not included |
|---|---|
| The database: assessment, scores, history, risk assessment, assets, documents, evidence records, actions, connector runs and checks, Claude proposals, users, sessions, activity log, Settings page values (stored secrets stay encrypted) | `.env`: sign-in configuration, `CYFUN_SECRET_KEY`, environment overrides |
| Evidence files and connector snapshots | Temporary files, audit packs, earlier backups |

A backup is one file, `cyfun-backup-YYYYmmdd-HHMMSS.cyfunbak`. Inside is a zip with `manifest.json` (format, application version, creator, SHA-256 of every file), a consistent SQLite copy made with the online backup API, and the data files.

The file is encrypted with AES-256-GCM in 1 MiB chunks under a key derived (HKDF-SHA256) from `CYFUN_SECRET_KEY`. Each chunk is bound to its position and to the end of the file, so a reordered, cut or altered file is refused. A copy on OneDrive or a USB stick is unreadable without the server key. Restoring needs a server with the same key; the same key also decrypts the secrets stored on the Settings page. Keep `.env` in the password manager. The Backup page shows the key's fingerprint; `docker compose exec app python -m cyfun.backup key` prints the fingerprint of the key the container has. To check the copy in the password manager, compute its fingerprint without touching the running instance:

```bash
sudo docker run --rm -e CYFUN_SECRET_KEY='<stored key>' cyfun-basic-spog:latest python -m cyfun.backup key
```

## Back up

* **Download backup** builds a backup of the current state and downloads it.
* **Run scheduled backup now** writes a backup to `DATA_DIR/backups` on the server and, when OneDrive is configured, uploads it.
* **Scheduled**: set *Hours between backups* on the Settings page (0 is off, 24 daily, 168 weekly). The newest *Backups to keep* scheduled backups are kept on the server and in the OneDrive folder; older ones are deleted. The interval change applies without a restart.
* **Command line** (in the container): `docker compose exec app python -m cyfun.backup` writes a backup to `DATA_DIR/backups` with the same retention.

Every backup, upload, failure and restore is in the activity log and in *Recent backup activity* on the Backup page.

## OneDrive copy

The backup is copied to a folder in one user's OneDrive for Business through Microsoft Graph with an application permission. Fill in on the Settings page, group Scheduled backup:

| Field | Value |
|---|---|
| OneDrive user | User principal name of the OneDrive owner, for example a dedicated `cyfun-backup@contoso.com` account |
| OneDrive folder | Path in that OneDrive, default `CyFun backups`; created at the first upload |
| Tenant ID, client ID, client secret | A separate app registration (recommended). Empty fields fall back to the Microsoft 365 connector's values |

App registration, in Entra ID, App registrations, New registration (single tenant, no redirect URI):

1. Certificates and secrets: new client secret. Paste it on the Settings page.
2. API permissions, Microsoft Graph, Application permissions, one of:
   * **Sites.Selected** (least privilege). Grant admin consent, then give the app write access to the user's OneDrive site only. With Microsoft Graph PowerShell as a SharePoint or Global Administrator:

     ```powershell
     Connect-MgGraph -Scopes Sites.FullControl.All
     $site = Get-MgUserDrive -UserId cyfun-backup@contoso.com | Select-Object -First 1   # the OneDrive
     $siteId = (Invoke-MgGraphRequest GET "https://graph.microsoft.com/v1.0/drives/$($site.Id)/root?`$select=sharepointIds").sharepointIds.siteId
     New-MgSitePermission -SiteId $siteId -BodyParameter @{
       roles = @("write")
       grantedToIdentities = @(@{ application = @{ id = "<client ID>"; displayName = "CyFun backup" } })
     }
     ```
   * **Files.ReadWrite.All**. Simpler, but the app can then write to every user's OneDrive. Keep the secret accordingly.
3. Do not add these permissions to the read-only Microsoft 365 connector app.

**Test connection** checks the token, the user's drive and the folder without writing anything. Outbound traffic goes to `login.microsoftonline.com`, `graph.microsoft.com` and the SharePoint host of the upload session (`*.sharepoint.com`).

## Restore

On the Backup page, either upload a `.cyfunbak` file under *Restore from a file*, or restore one of the backups listed under *Backups on the server*. Type `RESTORE` to confirm.

1. The file is decrypted and checked completely: manifest, entry names (only the database, `evidence/` and `connector_snapshots/`), size (at most 20 GB unpacked), SHA-256 of every file, SQLite integrity check. If any check fails, nothing changes.
2. The current state is saved as `cyfun-backup-…-pre-restore.cyfunbak` on the server. Pre-restore copies are not counted in the retention and are not uploaded; delete them from the data volume when no longer needed.
3. Scheduled jobs pause, the database content is replaced and the evidence and snapshot folders are swapped. Columns added in a newer application version are added to a restored older database.

Users and sessions come from the backup: you may have to sign in again, with the password valid at the time of the backup.

Upload size: the application accepts restore uploads up to `MAX_RESTORE_MB` (default 2048) and Caddy up to 2 GB on `/backup/restore` (`deploy/Caddyfile`); other requests stay at 64 MB. For a larger backup, copy the file into the volume and use the command line.

**Command line**, for a host where the web page is not reachable. Stop the application first so no request writes during the restore:

```bash
cd /opt/cyfun
sudo docker compose stop app
sudo docker compose run --rm app python -m cyfun.backup restore /data/backups/cyfun-backup-20261006-020000.cyfunbak
sudo docker compose start app
```

To restore on a new server: install it per docs/deployment-proxmox.md with the same `.env` (same `CYFUN_SECRET_KEY`), sign in with the bootstrap account, and restore the file on the Backup page.
