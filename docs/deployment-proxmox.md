# Deployment on Proxmox

## 1. Guest

A small VM is the simplest Docker host on Proxmox (LXC works too but needs nesting and keyctl enabled; a VM avoids that).

| Setting | Value |
|---|---|
| OS | Debian 12 or Ubuntu 24.04 LTS, minimal |
| vCPU / RAM / disk | 2 vCPU, 2 GB RAM, 20 GB disk (evidence files decide the disk size) |
| Disk | enable encryption on the Proxmox storage or LUKS in the guest; the data volume holds confidential material |
| Network | static IP, DNS A record `cyfun.internal.example` pointing to it |
| Firewall | inbound 443 (and 80 only if using Let's Encrypt HTTP challenge) from the user network; SSH from the admin network only |

Install Docker Engine and the Compose plugin from Docker's repository (`https://docs.docker.com/engine/install/`). Create an unprivileged user in the `docker` group for operations.

## 2. Application

```bash
git clone https://github.com/yopazerbot/cyfun-basic-singlepaneofglass.git
cd cyfun-basic-singlepaneofglass
cp .env.example .env
chmod 600 .env
nano .env                     # APP_BASE_URL, APP_HOSTNAME, AUTH_*, connectors
docker compose up -d --build
docker compose ps             # app must be "healthy", caddy "running"
docker compose logs -f app
```

Open `https://cyfun.internal.example`. The browser warns about the certificate until the Caddy internal root CA is trusted: export it with `docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt` and import it on the client devices (or deploy it through Intune). Alternatively set `CADDY_TLS=you@example.com` and make port 80/443 reachable from the internet for Let's Encrypt, or use a certificate from your own PKI by mounting it and adapting `deploy/Caddyfile`.

## 3. Backup

Everything lives in the Docker volume `cyfun-basic-singlepaneofglass_data` (`/data` in the container): the SQLite database, evidence files, connector snapshots and database backups.

Nightly cron on the host:

```bash
#!/bin/sh
set -e
cd /opt/cyfun-basic-singlepaneofglass
docker compose exec -T app python -m cyfun.backup
docker run --rm -v cyfun-basic-singlepaneofglass_data:/data:ro -v /srv/backups/cyfun:/out alpine \
  tar czf /out/cyfun-data-$(date +%F).tgz -C /data .
find /srv/backups/cyfun -name 'cyfun-data-*.tgz' -mtime +30 -delete
```

Add the guest to a Proxmox Backup Server job as well. Keep `.env` in the password manager, not in the backup set that leaves the organisation.

Restore: stop the stack, recreate the volume, extract the tarball into it, start the stack.

```bash
docker compose down
docker volume rm cyfun-basic-singlepaneofglass_data
docker volume create cyfun-basic-singlepaneofglass_data
docker run --rm -v cyfun-basic-singlepaneofglass_data:/data -v /srv/backups/cyfun:/in:ro alpine \
  sh -c 'tar xzf /in/cyfun-data-2026-10-05.tgz -C /data && chown -R 10001:10001 /data'
docker compose up -d
```

## 4. Updates

```bash
git pull
docker compose build --pull
docker compose up -d
docker compose ps
```

Database schema changes are applied at start (tables are created if missing). Release notes state when a manual step is needed.

## 5. Monitoring

* `GET https://host/healthz` returns `{"status":"ok"}` without authentication; point your monitoring at it.
* `docker compose ps` shows the health check result.
* Connector errors appear on the Connected systems screen and in the activity log.
* Logs: `docker compose logs app`, JSON access logs from Caddy. Ship them to your log platform if PR.PS-04.1 evidence for this system is wanted.

## 6. Moving from the development machine

Build and test locally, push to GitHub, then clone on the Proxmox guest as above. Nothing in the image depends on the build machine; the `.env` is created on the guest.
