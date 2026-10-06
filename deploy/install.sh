#!/usr/bin/env bash
# Install or update CyFun on an Ubuntu (or Debian) server: Docker, the application behind Caddy, a nightly backup.
# Paste in the server's shell:
#
#   curl -fsSL https://raw.githubusercontent.com/yopazerbot/cyfun-basic-singlepaneofglass/main/deploy/install.sh | sudo bash
#
# It asks one question (the address users type; Enter uses the server's IP address). Options:
#   --host NAME     address users type (host name or IP address); skips the question
#   --yes           no questions: use --host or the IP address
# Run it again, or `sudo cyfun-update`, to update. Existing settings in /opt/cyfun/.env are kept.
set -Eeuo pipefail

REPO_URL="${CYFUN_REPO:-https://github.com/yopazerbot/cyfun-basic-singlepaneofglass.git}"
BRANCH="${CYFUN_BRANCH:-main}"
DIR=/opt/cyfun
BACKUP_DIR=/srv/backups/cyfun
TIMEZONE="${CYFUN_TIMEZONE:-Europe/Brussels}"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nError: %s\n' "$*" >&2; exit 1; }
trap 'printf "\nError: the installation stopped at line %s. Run the command again after fixing the cause.\n" "$LINENO" >&2' ERR

primary_ip() { ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}' || true; }
random() { head -c "$1" /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'; }
env_get() { grep -m1 "^$1=" "$DIR/.env" | cut -d= -f2- || true; }
env_set() {
  if grep -q "^$1=" "$DIR/.env"; then sed -i "s|^$1=.*|$1=$2|" "$DIR/.env"; else echo "$1=$2" >>"$DIR/.env"; fi
}
apt_install() { apt-get install -y -q -o Dpkg::Options::=--force-confold "$@" </dev/null; }

install_packages() {
  say "Operating system packages"
  apt-get update -q </dev/null
  apt_install ca-certificates curl git openssl unattended-upgrades
  if [ "$(systemd-detect-virt 2>/dev/null || true)" = kvm ]; then
    # Clean shutdown and consistent backups from Proxmox (enable "QEMU Guest Agent" in the VM options).
    apt_install qemu-guest-agent
    systemctl start qemu-guest-agent 2>/dev/null || true
  fi
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
    >/etc/apt/apt.conf.d/20auto-upgrades
  timedatectl set-timezone "$TIMEZONE" 2>/dev/null || true
}

install_docker() {
  if docker compose version >/dev/null 2>&1; then return; fi
  say "Docker Engine (from Docker's repository)"
  # shellcheck disable=SC1091
  . /etc/os-release
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -q </dev/null
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

get_application() {
  say "Application in $DIR"
  if [ -d "$DIR/.git" ]; then
    git -c safe.directory="$DIR" -C "$DIR" pull --ff-only -q
  else
    git clone -q --branch "$BRANCH" "$REPO_URL" "$DIR"
  fi
}

configure() {
  local host="$1" ask="$2" ip answer
  ip="$(primary_ip)"
  if [ -f "$DIR/.env" ]; then
    FIRST=no
    if [ -n "$host" ]; then
      env_set APP_HOSTNAME "$host"
      env_set APP_BASE_URL "https://$host"
    fi
    return
  fi
  FIRST=yes
  if [ -z "$host" ] && [ "$ask" = yes ] && [ -r /dev/tty ]; then
    printf '\nAddress users will type in the browser, without https://\n'
    printf 'A host name needs a DNS record pointing to %s. Press Enter to use the IP address.\n' "${ip:-this server}"
    read -r -p "Address [${ip}]: " answer </dev/tty || true
    host="${answer#https://}"
    host="${host%%/*}"
  fi
  host="${host:-$ip}"
  [ -n "$host" ] || die "no IP address found; run again with --host NAME"
  say "Configuration in $DIR/.env"
  cp "$DIR/.env.example" "$DIR/.env"
  chmod 600 "$DIR/.env"
  env_set APP_HOSTNAME "$host"
  env_set APP_BASE_URL "https://$host"
  env_set CYFUN_SECRET_KEY "$(random 32)"
  env_set AUTH_BOOTSTRAP_PASSWORD "$(random 18)"
  env_set COMPOSE_PROJECT_NAME cyfun
}

start() {
  say "Build and start (the first build takes a few minutes)"
  cd "$DIR"
  docker compose build --pull --quiet
  docker compose pull --quiet caddy
  if ! docker compose up -d --remove-orphans --wait --wait-timeout 300; then
    docker compose logs --tail 40 app >&2
    die "the application did not start; the log is above"
  fi
  docker image prune -f >/dev/null
  local host
  host="$(env_get APP_HOSTNAME)"
  for _ in $(seq 1 15); do
    if curl -skf --connect-to "$host:443:127.0.0.1:443" "https://$host/healthz" >/dev/null; then return; fi
    sleep 2
  done
  docker compose logs --tail 20 caddy >&2
  curl -skv --connect-to "$host:443:127.0.0.1:443" "https://$host/healthz" 2>&1 | tail -n 15 >&2 || true
  die "the application runs, but https://$host/healthz does not answer through Caddy; the log is above"
}

install_backup_and_update() {
  say "Nightly backup (02:30, kept 30 days in $BACKUP_DIR) and the update command"
  install -d -m 700 "$BACKUP_DIR"
  cat >/usr/local/sbin/cyfun-backup <<EOF
#!/bin/sh
# Encrypted application backup (docs/backup.md), then a tarball of the whole data volume.
set -eu
cd $DIR
docker compose exec -T app python -m cyfun.backup
docker run --rm -v cyfun_data:/data:ro -v $BACKUP_DIR:/out alpine:3 \\
  tar czf "/out/cyfun-data-\$(date +%F).tgz" -C /data .
find $BACKUP_DIR -name 'cyfun-data-*.tgz' -mtime +30 -delete
EOF
  cat >/usr/local/sbin/cyfun-update <<EOF
#!/bin/sh
# Back up, then fetch the latest version and rebuild (deploy/install.sh).
set -eu
/usr/local/sbin/cyfun-backup
git -c safe.directory=$DIR -C $DIR pull --ff-only -q
exec bash $DIR/deploy/install.sh --yes "\$@"
EOF
  chmod 750 /usr/local/sbin/cyfun-backup /usr/local/sbin/cyfun-update
  cat >/etc/systemd/system/cyfun-backup.service <<'EOF'
[Unit]
Description=CyFun data backup
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cyfun-backup
EOF
  cat >/etc/systemd/system/cyfun-backup.timer <<'EOF'
[Unit]
Description=Nightly CyFun data backup

[Timer]
OnCalendar=*-*-* 02:30
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now cyfun-backup.timer >/dev/null 2>&1
}

export_root_certificate() {
  local tls
  tls="$(env_get CADDY_TLS)"
  [ -z "$tls" ] || [ "$tls" = internal ] || return 0
  for _ in $(seq 1 15); do
    if docker compose -f "$DIR/compose.yaml" --project-directory "$DIR" exec -T caddy \
      cat /data/caddy/pki/authorities/local/root.crt >"$DIR/cyfun-root-ca.crt" 2>/dev/null; then
      chmod 644 "$DIR/cyfun-root-ca.crt"
      return 0
    fi
    sleep 2
  done
  rm -f "$DIR/cyfun-root-ca.crt"
}

summary() {
  local host password
  host="$(env_get APP_HOSTNAME)"
  password="$(env_get AUTH_BOOTSTRAP_PASSWORD)"
  printf '\n----------------------------------------------------------------------\n'
  printf 'CyFun is running.\n\n'
  printf '  Address            https://%s\n' "$host"
  if [ "$FIRST" = yes ] && [ -n "$password" ]; then
    printf '  First sign-in      user admin, password %s\n' "$password"
    printf '                     (the first sign-in asks for a new password)\n'
  fi
  if [ -f "$DIR/cyfun-root-ca.crt" ]; then
    printf '  Root certificate   %s\n' "$DIR/cyfun-root-ca.crt"
    printf '                     %s\n' "$(openssl x509 -noout -fingerprint -sha256 -in "$DIR/cyfun-root-ca.crt")"
    printf '                     Trust it on the devices that open the address (docs/deployment-proxmox.md, step 3).\n'
  fi
  printf '  Settings           %s/.env\n' "$DIR"
  printf '  Update             sudo cyfun-update\n'
  printf '  Nightly backup     %s, 02:30\n' "$BACKUP_DIR"
  if [ "$FIRST" = yes ]; then
    printf '\nSave a copy of %s/.env in your password manager now:\n' "$DIR"
    printf '  sudo cat %s/.env\n' "$DIR"
    printf 'A backup can only be restored with the CYFUN_SECRET_KEY in that file.\n'
  fi
  printf -- '----------------------------------------------------------------------\n'
}

main() {
  local host="" ask=yes
  while [ $# -gt 0 ]; do
    case "$1" in
      --host) host="${2:?--host needs a value}"; shift 2 ;;
      --host=*) host="${1#*=}"; shift ;;
      --yes | -y) ask=no; shift ;;
      *) die "unknown option $1 (use --host NAME or --yes)" ;;
    esac
  done
  [ "$(id -u)" -eq 0 ] || die "run as root: curl ... | sudo bash"
  # shellcheck disable=SC1091
  ID="$(. /etc/os-release && echo "$ID")"
  case "$ID" in ubuntu | debian) ;; *) die "this script supports Ubuntu and Debian, found $ID" ;; esac

  install_packages
  install_docker
  get_application
  configure "$host" "$ask"
  start
  install_backup_and_update
  export_root_certificate
  summary
}

# Everything runs from main, so `curl | bash` has read the whole script before the first command.
main "$@"
