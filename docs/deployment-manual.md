# Manual deployment on Proxmox

The quick install in [deployment-proxmox.md](deployment-proxmox.md) does steps 3, 4, 6, 7, 9 and the nightly backup of step 12 with one command. Use this guide to see what that command does, to create the VM from the Proxmox shell, or to set up each part by hand.

Step by step, from an empty Proxmox host to a running instance with TLS, backups and updates. The result is one Ubuntu Server 26.04 LTS VM that runs two containers: the application and Caddy as TLS reverse proxy. Users reach it on `https://<hostname>` from your internal network; nothing has to be published to the internet.

```
users (LAN / VPN) ──HTTPS 443──> Proxmox VM "cyfun" (Ubuntu 26.04, Docker)
                                   ├── caddy  :80 :443   TLS, HSTS, reverse proxy
                                   └── app    :8000      internal Docker network only
                                         └── volume cyfun_data: database, evidence, snapshots, backups
                                   outbound HTTPS only: Microsoft, GitHub, Railway, Cloudflare, Notion, Anthropic
```

Every command block can be pasted as is after you change the values marked `# change`. Commands run either on the **Proxmox host** (root shell, from the web UI: node, Shell) or **in the VM** (SSH as the user `cyfun`); each step says which.

## 0. Values to decide first

| Value | Example used below | Notes |
|---|---|---|
| VM ID | `210` | any free ID (`qm list` shows the used ones) |
| Storage for the VM disk | `local-lvm` | `pvesm status` lists your storages. Prefer encrypted storage (ZFS native encryption or LUKS underneath); the VM holds confidential evidence. |
| Bridge, optional VLAN | `vmbr0` | add `,tag=20` to the network line for a VLAN |
| VM address, gateway, DNS | `192.168.10.50/24`, `192.168.10.1`, `192.168.10.1` | a static address |
| Host name users type | `cyfun.internal.example` | must resolve in your internal DNS |
| Network users come from | `192.168.10.0/24` | for the firewall |
| Network you administer from | `192.168.10.0/24` | SSH access |
| Certificate | Caddy internal CA | or your own PKI, or Let's Encrypt (step 8) |
| Sign-in | local account first, then Entra ID | step 11 |

Sizing: 2 vCPU, 2 GB RAM and a 32 GB disk are enough. Evidence files decide the disk size; grow the disk later if needed.

## 1. SSH key for the VM (Proxmox host)

The VM accepts SSH keys only. Put the public key of the workstation you administer from into a file on the Proxmox host:

```bash
cat > /root/cyfun-admin.pub <<'EOF'
ssh-ed25519 AAAA...replace-with-your-public-key... you@workstation
EOF
```

On Windows the public key is in `%USERPROFILE%\.ssh\id_ed25519.pub`; create one with `ssh-keygen -t ed25519` if it does not exist.

## 2. Create the VM (Proxmox host)

Download the Ubuntu Server 26.04 LTS cloud image and check its checksum:

```bash
cd /var/lib/vz/template/iso
wget -N https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img
wget -N https://cloud-images.ubuntu.com/releases/26.04/release/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
```

The last command must print `ubuntu-26.04-server-cloudimg-amd64.img: OK`. The `.img` file is a QCOW2 disk image; Proxmox imports it as is. Use the plain `amd64` image, not `amd64v3`, unless you know the host CPU supports x86-64-v3.

Create, configure and start the VM:

```bash
VMID=210                      # change
STORAGE=local-lvm             # change
BRIDGE=vmbr0                  # change (append ,tag=20 for a VLAN)
IP=192.168.10.50/24           # change
GW=192.168.10.1               # change
DNS=192.168.10.1              # change
IMG=/var/lib/vz/template/iso/ubuntu-26.04-server-cloudimg-amd64.img

qm create $VMID --name cyfun --ostype l26 --cpu host --cores 2 --memory 2048 \
  --scsihw virtio-scsi-single --net0 virtio,bridge=$BRIDGE,firewall=1 \
  --agent enabled=1 --serial0 socket --vga serial0 --onboot 1
qm set $VMID --scsi0 $STORAGE:0,import-from=$IMG,discard=on,ssd=1,iothread=1
qm resize $VMID scsi0 32G
qm set $VMID --ide2 $STORAGE:cloudinit --boot order=scsi0
qm set $VMID --ciuser cyfun --sshkeys /root/cyfun-admin.pub \
  --ipconfig0 ip=$IP,gw=$GW --nameserver $DNS
qm start $VMID
```

After about a minute the VM answers on SSH. From your workstation:

```bash
ssh cyfun@192.168.10.50       # change
```

Cloud-init creates the user `cyfun` (instead of the image's default `ubuntu`) with sudo rights without a password; there is no password login.

## 3. Prepare Ubuntu (in the VM)

Updates, the QEMU guest agent (clean shutdown and consistent Proxmox backups; the cloud image does not include it), automatic security updates, time zone:

```bash
sudo apt-get update && sudo apt-get -y full-upgrade
sudo apt-get install -y qemu-guest-agent unattended-upgrades git curl ca-certificates nano
sudo systemctl start qemu-guest-agent
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades
sudo timedatectl set-timezone Europe/Brussels
timedatectl
```

`timedatectl` must show `System clock synchronized: yes`; Entra ID sign-in fails when the clock is off by more than a minute.

Restrict SSH to keys and no root login:

```bash
sudo tee /etc/ssh/sshd_config.d/10-cyfun.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
sudo sshd -t && sudo systemctl reload ssh
```

Reboot once if the upgrade installed a new kernel; Ubuntu marks this with a file:

```bash
[ -f /var/run/reboot-required ] && sudo reboot
```

If `apt-get` shows a "Pending kernel upgrade" or "Daemons using outdated libraries" screen, press Enter to accept the defaults.

## 4. Install Docker Engine (in the VM)

From Docker's own repository, as documented on docs.docker.com. Do not use Ubuntu's `docker.io` package or the Docker snap: they lag behind, and the snap keeps volumes in another location than the commands below expect.

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker version --format 'Docker {{.Server.Version}}' && sudo docker compose version
```

This guide runs Docker with `sudo`. Adding `cyfun` to the `docker` group would avoid typing it, but that group is equivalent to root on the VM.

## 5. Firewall (Proxmox host)

Docker opens ports 80 and 443 itself and bypasses a firewall inside the VM (ufw is installed on Ubuntu but inactive; leave it that way), so filter at the Proxmox level. The VM's network card already has `firewall=1` from step 2.

```bash
VMID=210                      # change
USER_NET=192.168.10.0/24      # change: where users open the application
ADMIN_NET=192.168.10.0/24     # change: where you administer from
cat > /etc/pve/firewall/$VMID.fw <<EOF
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -source $USER_NET -p tcp -dport 443 -log nolog
IN ACCEPT -source $USER_NET -p tcp -dport 80 -log nolog
IN ACCEPT -source $ADMIN_NET -p tcp -dport 22 -log nolog
IN ACCEPT -p icmp -log nolog
EOF
```

VM rules only apply when the firewall is enabled at datacenter level: Datacenter, Firewall, Options, Firewall: **Yes**. If it already is, nothing else is needed.

Before you switch it on for the first time: with the datacenter firewall enabled, the Proxmox web UI (8006) and SSH stay reachable only from the host's own network or the `management` IPSet. If you manage Proxmox from another network (a VPN or another VLAN), first add it under Datacenter, Firewall, IPSet: create `management` and add that network.

Port 80 only redirects to HTTPS (and is needed for Let's Encrypt). Outbound traffic stays open; Appendix B lists what the server needs if you want to restrict it.

## 6. Get the application (in the VM)

**Private repository:** give the VM a read-only deploy key.

```bash
ssh-keygen -t ed25519 -N "" -C "cyfun-vm-deploy" -f ~/.ssh/cyfun_deploy
cat ~/.ssh/cyfun_deploy.pub
```

In GitHub: the repository, Settings, Deploy keys, Add deploy key. Paste the key and leave "Allow write access" unticked. Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-cyfun
    HostName github.com
    User git
    IdentityFile ~/.ssh/cyfun_deploy
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
sudo install -d -o "$USER" -g "$USER" /opt/cyfun
git clone git@github-cyfun:yopazerbot/cyfun-basic-singlepaneofglass.git /opt/cyfun
```

At the first connection SSH asks to confirm GitHub's host key. GitHub's ED25519 key fingerprint is `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU` (published on docs.github.com under "GitHub's SSH key fingerprints").

**Public repository:** no key needed.

```bash
sudo install -d -o "$USER" -g "$USER" /opt/cyfun
git clone https://github.com/yopazerbot/cyfun-basic-singlepaneofglass.git /opt/cyfun
```

## 7. Configure `.env` (in the VM)

```bash
cd /opt/cyfun
cp .env.example .env
chmod 600 .env
HOST=cyfun.internal.example   # change: the name users type
sed -i \
  -e "s|^APP_BASE_URL=.*|APP_BASE_URL=https://$HOST|" \
  -e "s|^APP_HOSTNAME=.*|APP_HOSTNAME=$HOST|" \
  -e "s|^CYFUN_SECRET_KEY=.*|CYFUN_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')|" \
  -e "s|^AUTH_BOOTSTRAP_PASSWORD=.*|AUTH_BOOTSTRAP_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')|" \
  .env
echo "COMPOSE_PROJECT_NAME=cyfun" >> .env
grep -E '^(APP_BASE_URL|APP_HOSTNAME|CADDY_TLS|CYFUN_SECRET_KEY|AUTH_BOOTSTRAP_PASSWORD|COMPOSE_PROJECT_NAME)=' .env
```

What this sets:

| Variable | Why |
|---|---|
| `APP_BASE_URL`, `APP_HOSTNAME` | The address users type. Every form submission is checked against it, and the Entra redirect URI is derived from it. |
| `CYFUN_SECRET_KEY` | Encrypts the credentials you enter on the Settings page. |
| `AUTH_BOOTSTRAP_PASSWORD` | Password of the first `admin` account instead of `admin`, so no default password is ever exposed on the network. The first login still forces a change. |
| `COMPOSE_PROJECT_NAME` | Fixes the Docker names used below: volume `cyfun_data`, containers `cyfun-app` and `cyfun-caddy`. |

Copy the whole `.env` into your password manager now. A restore needs the same `CYFUN_SECRET_KEY`; without it the stored credentials cannot be decrypted and must be entered again.

The internal Docker network uses `172.31.250.0/24`. If your organisation uses that range anywhere, set another unused range in `CYFUN_SUBNET`.

Connector credentials and the Anthropic API key do not go into `.env`; you enter them on the Settings page in step 10.

## 8. DNS and certificate

**DNS.** Create an A record for the host name pointing to the VM address in your internal DNS. On a Windows DNS server:

```powershell
Add-DnsServerResourceRecordA -ZoneName "internal.example" -Name "cyfun" -IPv4Address "192.168.10.50"   # change
```

On Pi-hole, AdGuard Home, OPNsense or pfSense this is a local DNS record or host override. Check from a workstation: `nslookup cyfun.internal.example`.

**Certificate.** Pick one option.

* **A. Caddy internal CA (default, `CADDY_TLS=internal`).** Caddy issues the certificate from its own root CA. After the first start (step 9) you install that root on the devices that use the application. Fits a LAN-only host.
* **B. Certificate from your own PKI.** Put the certificate chain and key on the VM and point Caddy at them with an override file that `git pull` never touches:

  ```bash
  cd /opt/cyfun
  mkdir -p certs                 # copy fullchain.pem and privkey.pem into it
  chmod 700 certs
  sed 's|tls {$CADDY_TLS:internal}|tls /certs/fullchain.pem /certs/privkey.pem|' deploy/Caddyfile > deploy/Caddyfile.local
  cat > compose.override.yaml <<'EOF'
  services:
    caddy:
      volumes:
        - ./deploy/Caddyfile.local:/etc/caddy/Caddyfile:ro
        - ./certs:/certs:ro
  EOF
  printf 'compose.override.yaml\ndeploy/Caddyfile.local\ncerts/\n' >> .git/info/exclude
  ```

  When the certificate is renewed, replace the two files and run `sudo docker compose restart caddy`.
* **C. Let's Encrypt.** Only when the host name is in public DNS and ports 80 and 443 are reachable from the internet. Set `CADDY_TLS=you@example.com` in `.env`. This exposes the application; only do it if the CAB needs remote access, and only for that period.

## 9. Start and check (in the VM)

```bash
cd /opt/cyfun
sudo docker compose up -d --build
sudo docker compose ps
```

The first build takes a few minutes. `cyfun-app` must show `healthy` (about 20 seconds after start) and `cyfun-caddy` `Up`. Check the full path through Caddy:

```bash
HOST=$(grep '^APP_HOSTNAME=' .env | cut -d= -f2)
curl -sk --resolve "$HOST:443:127.0.0.1" "https://$HOST/healthz"; echo
sudo docker compose logs --tail 30 app
```

The health check answers `{"status":"ok"}`.

**Option A only: trust the Caddy root certificate.** Export it in the VM:

```bash
sudo docker compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > ~/cyfun-root-ca.crt
```

Copy it to a workstation and import it as a trusted root:

```bash
scp cyfun@192.168.10.50:cyfun-root-ca.crt .   # change the address
```

```powershell
# Windows, PowerShell as administrator
Import-Certificate -FilePath .\cyfun-root-ca.crt -CertStoreLocation Cert:\LocalMachine\Root
```

```bash
# macOS
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain cyfun-root-ca.crt
```

For managed devices, deploy it with an Intune "Trusted certificate" configuration profile or a group policy. The root stays the same as long as the `cyfun_caddy_data` volume exists, which the VM backup in step 12 covers.

## 10. First sign-in and setup (browser)

1. Open `https://cyfun.internal.example`.
2. Sign in as `admin` with the `AUTH_BOOTSTRAP_PASSWORD` value: `grep '^AUTH_BOOTSTRAP_PASSWORD=' /opt/cyfun/.env`. Set a new password of at least 12 characters.
3. Remove the bootstrap password from `.env`; it is used only while no account exists:

   ```bash
   sed -i 's|^AUTH_BOOTSTRAP_PASSWORD=.*|AUTH_BOOTSTRAP_PASSWORD=|' /opt/cyfun/.env
   ```

4. Scope, level and journey: organisation, scope, CAB, target level. Then the risk assessment.
5. Settings: enter the connector credentials (permissions per connector in docs/connectors.md), use "Test connection", save. Then Connected systems, "Run now" per connector.
6. Optional: Settings, Claude: paste an Anthropic API key and set the monthly limit (docs/ai-assistance.md). Allow outbound HTTPS to `api.anthropic.com` if you restrict outbound traffic.
7. Users: create accounts for colleagues or an external auditor, or continue with step 11.

## 11. Microsoft Entra ID sign-in (recommended)

Full details and troubleshooting: docs/entra-id-sso.md. In short:

1. Entra admin center, App registrations, New registration: single tenant, platform **Web**, redirect URI `https://cyfun.internal.example/auth/callback`. Entra accepts an internal host name here; only the browser follows the redirect.
2. Authentication: tick **ID tokens**. Certificates & secrets: new client secret (note its expiry date).
3. App roles: `Admin` and `Auditor` (value equals the name, member type Users/Groups).
4. Enterprise applications, the new app, Properties: **Assignment required: Yes**. Users and groups: assign yourself `Admin`.
5. Put the three values in `.env` and recreate the application container:

   ```bash
   cd /opt/cyfun
   nano .env        # AUTH_TENANT_ID, AUTH_CLIENT_ID, AUTH_CLIENT_SECRET
   sudo docker compose up -d --force-recreate app
   ```

6. Sign in with Microsoft. When that works, turn local accounts off (or keep them for an external auditor until the verification is done):

   ```bash
   sed -i 's|^AUTH_LOCAL_ENABLED=.*|AUTH_LOCAL_ENABLED=false|' /opt/cyfun/.env
   sudo docker compose up -d --force-recreate app
   ```

The Microsoft 365 connector uses a second, separate app registration with application permissions; its values go on the Settings page, not in `.env`.

## 12. Backups

Three layers: a consistent nightly copy of the data inside the VM, a Proxmox backup of the whole VM, and `.env` in the password manager.

**In the application.** Set *Hours between backups* on the Settings page (24 for daily) and, for an off-host copy, a OneDrive user and folder. The application then writes an encrypted backup of the database, settings and data files to the data volume and uploads it to OneDrive. Download and restore are on the Backup and restore page. Details, permissions and restore steps: [docs/backup.md](backup.md).

**Nightly data backup (in the VM).** Independent of the application schedule: the script writes an encrypted application backup, then archives the whole data volume (database, evidence files, connector snapshots, backups):

```bash
sudo install -d -m 700 /srv/backups/cyfun
sudo tee /usr/local/sbin/cyfun-backup >/dev/null <<'EOF'
#!/bin/sh
# Encrypted application backup (docs/backup.md), then a tarball of the whole data volume.
set -eu
cd /opt/cyfun
docker compose exec -T app python -m cyfun.backup
docker run --rm -v cyfun_data:/data:ro -v /srv/backups/cyfun:/out alpine:3 \
  tar czf "/out/cyfun-data-$(date +%F).tgz" -C /data .
find /srv/backups/cyfun -name 'cyfun-data-*.tgz' -mtime +30 -delete
EOF
sudo chmod 750 /usr/local/sbin/cyfun-backup

sudo tee /etc/systemd/system/cyfun-backup.service >/dev/null <<'EOF'
[Unit]
Description=CyFun data backup
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cyfun-backup
EOF

sudo tee /etc/systemd/system/cyfun-backup.timer >/dev/null <<'EOF'
[Unit]
Description=Nightly CyFun data backup

[Timer]
OnCalendar=*-*-* 02:30
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cyfun-backup.timer
sudo systemctl start cyfun-backup.service
ls -lh /srv/backups/cyfun
```

The last command shows today's archive. Thirty days of archives are kept.

**Proxmox backup of the VM.** Datacenter, Backup, Add: your Proxmox Backup Server or NAS storage, schedule `03:00` (after the data backup), selection VM `210`, mode **Snapshot**. On Proxmox Backup Server, enable client-side encryption for the storage so the backups are encrypted at rest. This copy also contains the archives from `/srv/backups/cyfun` and the Caddy root CA, and is your off-host copy.

**Restore the data** from an application backup: on the Backup and restore page, or with the command line in docs/backup.md. From a volume archive on the VM:

```bash
cd /opt/cyfun
sudo docker compose down
sudo docker volume rm cyfun_data
sudo docker volume create cyfun_data
sudo docker run --rm -v cyfun_data:/data -v /srv/backups/cyfun:/in:ro alpine:3 \
  sh -c 'tar xzf /in/cyfun-data-2026-10-05.tgz -C /data && chown -R 10001:10001 /data'   # change the date
sudo docker compose up -d
```

The same `.env` (same `CYFUN_SECRET_KEY`) must be in place. To restore the whole VM, use the Proxmox backup (VM, Backup, Restore). Test a restore once, for example into a VM with another ID and no network, before you rely on it.

## 13. Updates

**Application** (in the VM). Back up first, then pull, rebuild with fresh base images and restart:

```bash
cd /opt/cyfun
sudo systemctl start cyfun-backup.service
git pull
git diff 'HEAD@{1}' --stat -- .env.example     # new variables? compare with your .env
sudo docker compose build --pull
sudo docker compose pull caddy
sudo docker compose up -d
sudo docker compose ps
sudo docker image prune -f
```

Database changes are applied at start (new tables and columns are added; nothing is removed). Monthly is a good rhythm, plus whenever GitHub shows a security update.

**Operating system.** Security updates install automatically (step 3). Once a month: `sudo apt-get update && sudo apt-get -y full-upgrade`, then `[ -f /var/run/reboot-required ] && sudo reboot`. Ubuntu 26.04 LTS gets standard security updates until 2031. Move to the next LTS with `sudo do-release-upgrade` after a backup; Ubuntu offers it once that release's first point release is out. Docker restarts both containers by itself.

## 14. Monitoring

* `https://cyfun.internal.example/healthz` answers `{"status":"ok"}` without sign-in. Point your monitoring (for example Uptime Kuma) at it.
* `sudo docker compose ps` shows the container health; `sudo docker compose logs app` and `sudo docker compose logs caddy` the logs (rotated at 5 x 10 MB).
* Disk space: `df -h /` and `sudo docker system df`.
* In the application: connector run errors on Connected systems, failed sign-ins and settings changes in the Activity log.

## 15. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Browser warns about the certificate | Option A: the Caddy root is not trusted on that device (step 9). Firefox needs `security.enterprise_roots.enabled` or its own import. |
| "Cross-site request blocked" on every save | `APP_BASE_URL` differs from the address in the browser (scheme, host name or port). Fix `.env`, then `sudo docker compose up -d --force-recreate app`. |
| Sign-in succeeds but returns to the sign-in page | `APP_BASE_URL` starts with `http://` while the site is opened over HTTPS, or the other way round. |
| `cyfun-app` stays `unhealthy` or restarts | `sudo docker compose logs app`; usually a typo in `.env`. |
| Settings page: "Secrets cannot be stored" | `CYFUN_SECRET_KEY` is missing or shorter than 32 characters. Set it and recreate the app container. |
| Settings page: "The stored value cannot be used" | `CYFUN_SECRET_KEY` changed. Put the previous key back, or enter the secrets again. |
| Connector test: timeout or "no connection" | Outbound HTTPS from the VM is blocked; see Appendix B. |
| Entra error `AADSTS50011` | The redirect URI differs from `APP_BASE_URL` + `/auth/callback`. |
| "ID token validation failed" | Clock of the VM is off (`timedatectl`), or wrong tenant ID. |
| `docker compose up` fails with "Pool overlaps with other one on this address space" | `CYFUN_SUBNET` overlaps an existing network. Choose another range. |
| Upload refused | The file is above `MAX_UPLOAD_MB` (default 25 MB). |
| The site does not load from a workstation but `/healthz` works in the VM | DNS record missing, or the Proxmox firewall rules (step 5) do not include the workstation's network. |

## Appendix A: LXC container instead of a VM

Proxmox recommends a VM for Docker, and this guide uses one. An unprivileged LXC container also works and uses less memory, at the cost of sharing the host kernel. On the Proxmox host:

```bash
pveam update
pveam available --section system | grep ubuntu-26.04-standard
pveam download local ubuntu-26.04-standard_26.04-1_amd64.tar.zst      # change to the exact name listed
pct create 210 local:vztmpl/ubuntu-26.04-standard_26.04-1_amd64.tar.zst \
  --hostname cyfun --unprivileged 1 --features nesting=1,keyctl=1 \
  --cores 2 --memory 2048 --swap 512 --rootfs local-lvm:32 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.10.50/24,gw=192.168.10.1,firewall=1 \
  --nameserver 192.168.10.1 --ssh-public-keys /root/cyfun-admin.pub --onboot 1
pct start 210
```

Then `ssh root@192.168.10.50` and continue at step 3 as root: leave out `sudo` and `qemu-guest-agent`, and skip the SSH drop-in until you have created a personal account with sudo rights, because it disables root login. The firewall file of step 5 works the same for a container.

## Appendix B: outbound connections

Leave outbound traffic open, or allow at least these destinations on TCP 443:

| Destination | Needed for |
|---|---|
| `archive.ubuntu.com`, `security.ubuntu.com` (TCP 80 and 443), `download.docker.com` | operating system and Docker updates |
| `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com` | base images (Python, Caddy, Alpine) |
| `pypi.org`, `files.pythonhosted.org` | building the application image |
| `github.com` | `git pull` |
| `login.microsoftonline.com` | Entra ID sign-in and the Microsoft Graph token |
| `graph.microsoft.com` | Microsoft 365 connector, OneDrive backup copy |
| `<tenant>-my.sharepoint.com` | OneDrive backup upload (upload session), only when configured |
| `api.github.com` | GitHub connector |
| `backboard.railway.com` | Railway connector |
| `api.cloudflare.com` | Cloudflare connector |
| `api.notion.com` | Notion connector (document register) |
| `api.anthropic.com` | Claude review, only when an API key is set |
| `acme-v02.api.letsencrypt.org` | option C certificates only |

Plus DNS (UDP/TCP 53) to your resolver and time synchronisation: Ubuntu 26.04 runs chrony with NTS against Ubuntu's time servers (UDP 123 and TCP 4460). To use your own NTP server instead, put `server ntp.internal.example iburst` (change the name) in `/etc/chrony/sources.d/local.sources`, run `sudo chronyc reload sources` and check with `chronyc sources`.

## After deployment

Run through the operating checklist in docs/security.md: first login completed and bootstrap password removed, `.env` in the password manager, Entra ID with assignment required, local accounts off or limited to the auditor, host not published to the internet, nightly and Proxmox backups running and a restore tested, monthly updates.
