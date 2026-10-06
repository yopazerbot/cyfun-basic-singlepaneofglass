# Deployment on Proxmox

One command in an Ubuntu VM installs everything: Docker, the application behind Caddy (HTTPS), a nightly backup and an update command. Nothing has to be published to the internet.

```
users (LAN / VPN) ──HTTPS 443──> Ubuntu VM, Docker
                                   ├── caddy  :80 :443   TLS, reverse proxy
                                   └── app    :8000      internal Docker network only
                                         └── volume cyfun_data: database, evidence, snapshots, backups
```

For every step by hand (creating the VM from the Proxmox shell, the Proxmox firewall, certificates from your own PKI, an LXC container), see [deployment-manual.md](deployment-manual.md).

## 1. The VM

An Ubuntu Server 26.04 LTS or 24.04 LTS VM (Debian 12 or 13 also works) with:

* 2 vCPU, 2 GB RAM, 32 GB disk;
* a fixed IP address (static, or a DHCP reservation);
* in Proxmox, VM, Options: **QEMU Guest Agent** enabled (the script installs the agent in the VM);
* outbound HTTPS to the internet (Ubuntu, Docker and GitHub downloads);
* a user with sudo rights.

## 2. Install (in the VM)

Open the VM's shell (SSH, or the Proxmox console) and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/yopazerbot/cyfun-basic-singlepaneofglass/main/deploy/install.sh | sudo bash
```

The script asks one question: the address users type. Press Enter to use the VM's IP address. A host name such as `cyfun.internal.example` also works, but then create a DNS record for it first (step 4). Microsoft sign-in (step 6) needs a host name.

The first run takes 5 to 10 minutes. At the end it shows:

```
CyFun is running.

  Address            https://192.168.10.50
  First sign-in      user admin, password Xq3...
  Root certificate   /opt/cyfun/cyfun-root-ca.crt
  ...
```

Save the settings file in your password manager now. A backup can only be restored with the `CYFUN_SECRET_KEY` it contains:

```bash
sudo cat /opt/cyfun/.env
```

## 3. Trust the certificate (each workstation)

Caddy issues the HTTPS certificate from its own root certificate. Without this step the browser shows a warning.

Show the certificate in the VM and copy the whole block, from `-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----`:

```bash
cat /opt/cyfun/cyfun-root-ca.crt
```

On Windows: paste it in Notepad, save it as `cyfun-root-ca.crt`, then in PowerShell as administrator:

```powershell
Import-Certificate -FilePath .\cyfun-root-ca.crt -CertStoreLocation Cert:\LocalMachine\Root
```

On macOS: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain cyfun-root-ca.crt`. For managed devices, deploy it with an Intune "Trusted certificate" profile or a group policy. Firefox needs `security.enterprise_roots.enabled` set to true.

Then open the address, sign in as `admin` with the password the script showed, and choose a new password. Continue on the Settings page (connectors, Claude, backup to OneDrive) and with Scope, level and journey.

## 4. Optional: a host name

To use `https://cyfun.internal.example` instead of the IP address:

1. Create a DNS A record for the name, pointing to the VM's IP address (Windows DNS: `Add-DnsServerResourceRecordA -ZoneName "internal.example" -Name "cyfun" -IPv4Address "192.168.10.50"`).
2. In the VM: `sudo cyfun-update --host cyfun.internal.example`
3. The root certificate stays the same; no new import is needed.

## 5. Backups

* **Nightly in the VM** (set up by the script): at 02:30 an encrypted application backup plus an archive of all data in `/srv/backups/cyfun`, kept 30 days. Run one now with `sudo cyfun-backup`.
* **Proxmox**: Datacenter, Backup, Add: schedule `03:00`, the VM, mode **Snapshot**, to your Proxmox Backup Server or NAS. This is the off-host copy.
* **OneDrive** (optional): on the Settings page, Scheduled backup ([backup.md](backup.md)).

Restore on a new VM: run the install command, replace `/opt/cyfun/.env` with your saved copy, run `sudo cyfun-update`, sign in and restore the backup file on the Backup and restore page.

## 6. Optional: Microsoft sign-in

Needs a host name (step 4). Register the application in Entra ID as described in [entra-id-sso.md](entra-id-sso.md) with the redirect URI `https://<host name>/auth/callback`, then put the three values in the settings file and apply:

```bash
sudo nano /opt/cyfun/.env        # AUTH_TENANT_ID, AUTH_CLIENT_ID, AUTH_CLIENT_SECRET
sudo cyfun-update
```

When Microsoft sign-in works, set `AUTH_LOCAL_ENABLED=false` in the same file and run `sudo cyfun-update` again (or keep local accounts for an external auditor).

## 7. Updates

```bash
sudo cyfun-update
```

This makes a backup, fetches the latest version, rebuilds and restarts. Ubuntu security updates install automatically; reboot once a month when Ubuntu asks for it: `[ -f /var/run/reboot-required ] && sudo reboot`.

## 8. Recommended: restrict access

Only the networks that use the application need ports 443 and 80; SSH only from where you administer. Set this in the Proxmox firewall (manual guide, step 5), not with ufw inside the VM: Docker bypasses ufw.

## Problems

| Symptom | Cause and fix |
|---|---|
| Browser warns about the certificate | The root certificate is not trusted on that device (step 3). |
| "Cross-site request blocked" on every save | The address in the browser differs from the one the script set. Use that address, or change it with `sudo cyfun-update --host NAME`. |
| The script stops with an error | Run the same command again after fixing the cause; it continues where it can. Logs: `cd /opt/cyfun && sudo docker compose logs app`. |
| The site does not load from a workstation | Check the address with `ping`, the DNS record (step 4) and the Proxmox firewall. In the VM, `cd /opt/cyfun && sudo docker compose ps` should show `cyfun-app` healthy and `cyfun-caddy` up. |

More cases are in the manual guide, Troubleshooting. Run through the operating checklist in [security.md](security.md) once the installation is done.
