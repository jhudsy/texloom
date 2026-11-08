#!/usr/bin/env bash
# Install or update texloom on an Ubuntu/Debian server, from this checkout.
#
#   deploy/install.sh root@server.example.org texloom.example.org
#
# Re-run it to update. Used for https://texloom.jhudsy.org (Ubuntu 24.04, aarch64,
# alongside other nginx sites). It only adds things: a `texloom` system user, the app in
# /var/www/texloom (set TEXLOOM_DIR for elsewhere), its data in /var/lib/texloom, settings
# in /etc/texloom, a systemd service on 127.0.0.1:8787 and an nginx site for the domain,
# plus a Let's Encrypt certificate via certbot. Other sites are left alone. An install in
# the old place (/opt/texloom) is moved over and then removed. It keeps no backups: back up
# /var/lib/texloom (the data) and /etc/texloom (codes, admin token) yourself. Extra ssh
# options can be passed in SSH_OPTS; set SKIP_CERT=1 to skip certbot.
set -euo pipefail

TARGET=${1:?usage: deploy/install.sh user@host domain}
DOMAIN=${2:?usage: deploy/install.sh user@host domain}
SSH_OPTS=${SSH_OPTS:-}
APP_DIR=${TEXLOOM_DIR:-/var/www/texloom}
# TeX Live mirror to use first, e.g. https://mirror.example.ac.uk/ctan/systems/texlive/tlnet/
# (default: mirror.ctan.org picks one near the server)
TEXLIVE_MIRROR=${TEXLIVE_MIRROR:-}
HERE=$(cd "$(dirname "$0")/.." && pwd)
BUSYTEX_VERSION=$(node -p "require('$HERE/web/node_modules/texlyre-busytex/package.json').version")
# sha256 of busytex-assets.tar.gz per release; set BUSYTEX_SHA256 for a version not listed here
case "$BUSYTEX_VERSION" in
  1.4.1) BUSYTEX_SHA256=${BUSYTEX_SHA256:-4f63731fe4ec9238d877846074ab424350add6f6e956944def389e6752d2c78b} ;;
esac
[ -n "${BUSYTEX_SHA256:-}" ] || { echo "No checksum known for TeX assets v$BUSYTEX_VERSION: set BUSYTEX_SHA256"; exit 1; }
ssh_() { ssh $SSH_OPTS "$TARGET" "$@"; }

echo "==> Building the web app locally"
(cd "$HERE/web" && npx tsc && npx vite build >/dev/null)
(cd "$HERE" && uv export --frozen --no-dev --no-emit-project --format requirements-txt -q -o deploy/.requirements.txt)

echo "==> Copying code to $TARGET:$APP_DIR"
ssh_ "mkdir -p '$APP_DIR'"
# Only what the server runs: the Python packages, the built web app and the deploy files
# (not the desktop app build, the web sources, caches or CI files).
# (Excluded paths are left alone on the server: its tex-assets and .venv stay.)
rsync -az --delete -e "ssh $SSH_OPTS" \
  --include /web/ --include '/web/dist/***' --exclude '/web/*' \
  --exclude /dist --exclude /packaging --exclude /.github --exclude .ruff_cache --exclude .pytest_cache \
  --exclude .git --exclude .venv --exclude node_modules --exclude /tex-assets --exclude /data \
  --exclude .claude --exclude '__pycache__' --exclude .DS_Store --exclude /build --exclude '*.egg-info' \
  --exclude /.python-version \
  "$HERE/" "$TARGET:$APP_DIR/"

echo "==> Installing on the server"
ssh_ DOMAIN="$DOMAIN" APP_DIR="$APP_DIR" TEXLIVE_MIRROR="$TEXLIVE_MIRROR" BUSYTEX_VERSION="$BUSYTEX_VERSION" BUSYTEX_SHA256="$BUSYTEX_SHA256" SKIP_CERT="${SKIP_CERT:-}" bash -s <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1  # leave no caches behind

# packages (only installs what's missing)
need=()
for p in nginx python3-venv rsync curl; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
command -v certbot >/dev/null || need+=(certbot python3-certbot-nginx)
if ((${#need[@]})); then apt-get update -qq && apt-get install -y -qq "${need[@]}"; fi

# user, directories, Python environment
id texloom >/dev/null 2>&1 || useradd --system --home /var/lib/texloom --shell /usr/sbin/nologin texloom
install -d -o texloom -g texloom -m 750 /var/lib/texloom
install -d -m 700 /root/texloom-backups
OLD=/opt/texloom  # where earlier versions of this script installed it
if [ "$APP_DIR" != "$OLD" ] && [ -d "$OLD/tex-assets" ] && [ ! -e "$APP_DIR/tex-assets" ]; then
  mv "$OLD/tex-assets" "$APP_DIR/tex-assets"  # ~700 MB: moved, not downloaded again
fi
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -q --no-cache-dir --upgrade pip
.venv/bin/pip install -q --no-cache-dir --require-hashes -r deploy/.requirements.txt
.venv/bin/pip install -q --no-cache-dir --no-deps .
chown -R root:root "$APP_DIR" && chmod -R a+rX "$APP_DIR"

# in-browser TeX (WebAssembly TeX Live): downloaded once, ~700 MB unpacked
if [ "$(cat tex-assets/.version 2>/dev/null)" != "$BUSYTEX_VERSION" ]; then
  echo "    downloading TeX assets v$BUSYTEX_VERSION"
  rm -rf tex-assets.new && mkdir -p tex-assets.new
  tmp=$(mktemp)
  curl -fsSL -o "$tmp" "https://github.com/TeXlyre/texlyre-busytex/releases/download/assets-v$BUSYTEX_VERSION/busytex-assets.tar.gz"
  echo "$BUSYTEX_SHA256  $tmp" | sha256sum -c --quiet - || { rm -f "$tmp"; echo "TeX assets checksum mismatch: refusing to install"; exit 1; }
  tar -xzf "$tmp" -C tex-assets.new && rm -f "$tmp"
  [ -f tex-assets.new/busytex/busytex.wasm ] || { echo "unexpected archive layout"; ls -R tex-assets.new | head; exit 1; }
  echo "$BUSYTEX_VERSION" > tex-assets.new/.version
  # the archive unpacks as uid 1000 with a world-writable directory: make it root-owned, read-only
  chown -R root:root tex-assets.new && chmod -R u=rwX,go=rX tex-assets.new
  rm -rf tex-assets && mv tex-assets.new tex-assets
fi
# fixes to the engine (e.g. biber and .bib files in folders); idempotent, on every deploy
python3 deploy/patch-busytex.py tex-assets

# storage allowances: without a code nothing is stored (default 0); print the owner's code
install -d -o root -g texloom -m 750 /etc/texloom
if [ ! -s /etc/texloom/codes.txt ]; then
  echo "    owner's creation code (unlimited): $(.venv/bin/texloom-server --codes-file /etc/texloom/codes.txt --db /var/lib/texloom/texloom.db --add-code owner --allowance unlimited)"
fi
chown root:texloom /etc/texloom/codes.txt && chmod 640 /etc/texloom/codes.txt
# admin page (/admin, read-only usage): password in /etc/texloom/admin-token
if [ ! -s /etc/texloom/admin-token ]; then
  head -c 24 /dev/urandom | base64 | tr -d '/+=' > /etc/texloom/admin-token
  echo "    admin page: https://$DOMAIN/admin  (password: sudo cat /etc/texloom/admin-token)"
fi
chown root:texloom /etc/texloom/admin-token && chmod 640 /etc/texloom/admin-token

# TeX Live files on demand for the in-browser engine: the package index (refreshed on each
# deploy; ~2 s), packages fetched from mirrors when first asked for, into this folder
install -d -o texloom -g texloom -m 750 /var/lib/texloom/texlive
MIRROR_ARG=${TEXLIVE_MIRROR:+--texlive-mirror $TEXLIVE_MIRROR}
runuser -u texloom -- .venv/bin/texloom-server --texlive /var/lib/texloom/texlive --texlive-init $MIRROR_ARG \
  || echo "    couldn't refresh the TeX Live index; keeping the one there is (if any)"

# systemd service
cat > /etc/systemd/system/texloom.service <<UNIT
[Unit]
Description=TeXLoom relay server
After=network-online.target
Wants=network-online.target

[Service]
User=texloom
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/texloom-server --host 127.0.0.1 --port 8787 --db /var/lib/texloom/texloom.db --static $APP_DIR/web/dist --tex-assets $APP_DIR/tex-assets --codes-file /etc/texloom/codes.txt --admin-token-file /etc/texloom/admin-token --min-free 2G --project-max-bytes 200M --texlive /var/lib/texloom/texlive --texlive-max 3G $MIRROR_ARG
Restart=on-failure
UMask=0077
MemoryMax=768M
TasksMax=128
NoNewPrivileges=true
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
SystemCallFilter=@system-service
ReadWritePaths=/var/lib/texloom

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q texloom
systemctl restart texloom

# nginx site. Static files are served by nginx; only /ws/ and /blob/ reach Python.
# Written only on first install: certbot adds the TLS lines to this file afterwards.
SITE=/etc/nginx/sites-available/$DOMAIN
if [ ! -f "$SITE" ]; then
  cat > "$SITE" <<NGINX
# Rate limits (http context; names prefixed to avoid clashing with other sites)
limit_req_zone \$binary_remote_addr zone=texloom_code:1m rate=6r/m;
limit_req_zone \$binary_remote_addr zone=texloom_list:1m rate=1r/s;
map \$request_method \$texloom_put_key { PUT \$binary_remote_addr; default ""; }
limit_req_zone \$texloom_put_key zone=texloom_upload:10m rate=10r/s;
limit_conn_zone \$binary_remote_addr zone=texloom_ws:10m;

server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    root $APP_DIR/web/dist;
    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;

    # security headers (locations that set their own add_header repeat the essentials)
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; connect-src 'self' https: http://localhost:* http://127.0.0.1:*; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000" always;

    # encrypted sync (WebSocket); connections are long-lived
    location /ws/ {
        limit_conn texloom_ws 30;
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
    # encrypted file blobs (images, PDFs)
    location /blob/ {
        limit_req zone=texloom_upload burst=40;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        client_max_body_size 75m;
        proxy_request_buffering off;
    }
    # server info and creation-code checks
    location = /api/code {
        limit_req zone=texloom_code burst=5 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
    }
    # importing an arXiv paper's source: the relay fetches it (rate-limited there too)
    location /api/arxiv/ {
        limit_req zone=texloom_code burst=3 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 120s;
    }
    # project lists: slows anyone trying passwords or creation codes
    location /api/list/ {
        limit_req zone=texloom_list burst=60 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
    }
    # TeX Live files on demand (a package is fetched from a mirror the first time)
    location /texlive/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_read_timeout 60s;
    }
    # read-only admin page (HTTP basic auth against /etc/texloom/admin-token)
    location = /admin {
        limit_req zone=texloom_code burst=5 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
    }

    # the in-browser engine's scripts: revalidated (deploys can patch them; cheap with ETags)
    location ~ ^/core/(.+\.js)\$ {
        alias $APP_DIR/tex-assets/\$1;
        add_header Cache-Control "no-cache";
        add_header X-Content-Type-Options nosniff always;
    }
    # in-browser TeX engine and packages: big, immutable per version
    location /core/ {
        alias $APP_DIR/tex-assets/;
        expires 30d;
        add_header Cache-Control "public";
        add_header X-Content-Type-Options nosniff always;
    }
    # hashed build output (.mjs is missing from nginx's default MIME table)
    location /assets/ {
        types { application/javascript js mjs; text/css css; }
        expires 1y;
        add_header Cache-Control "public, immutable";
        add_header X-Content-Type-Options nosniff always;
    }
    location / {
        try_files \$uri /index.html;
    }
    location ~ /\. { return 404; }
}
NGINX
  ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
else
  # Updating: point it at this install's folder, and add routes introduced since the site
  # was first written, before the catch-all /api/ location (certbot's lines stay as they are).
  cp "$SITE" "/root/texloom-backups/nginx-$DOMAIN.bak"
  sed -i "s#$OLD/#$APP_DIR/#g" "$SITE"
  python3 - "$SITE" "$APP_DIR" <<'PY'
import sys
path, app_dir = sys.argv[1], sys.argv[2]
site = open(path).read()
blocks = {
    "location /api/list/": """    # project lists: slows anyone trying passwords or creation codes
    location /api/list/ {
        limit_req zone=texloom_list burst=60 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
    }
""",
    "location /api/arxiv/": """    # importing an arXiv paper's source: the relay fetches it (rate-limited there too)
    location /api/arxiv/ {
        limit_req zone=texloom_code burst=3 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
""",
    "location /texlive/": """    # TeX Live files on demand (a package is fetched from a mirror the first time)
    location /texlive/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_read_timeout 60s;
    }
""",
    "location ~ ^/core/": """    # the in-browser engine's scripts: revalidated (deploys can patch them; cheap with ETags)
    location ~ ^/core/(.+\\.js)$ {
        alias APP_DIR/tex-assets/$1;
        add_header Cache-Control "no-cache";
        add_header X-Content-Type-Options nosniff always;
    }
""".replace("APP_DIR", app_dir),
    "location = /admin": """    # read-only admin page (HTTP basic auth against /etc/texloom/admin-token)
    location = /admin {
        limit_req zone=texloom_code burst=5 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
    }
""",
}
if "zone=texloom_list:" not in site:
    zone = "limit_req_zone $binary_remote_addr zone=texloom_list:1m rate=1r/s;\n"
    code_zone = site.index("limit_req_zone $binary_remote_addr zone=texloom_code:")
    site = site[:code_zone] + zone + site[code_zone:]
    open(path, "w").write(site)
anchor = "    location /api/ {"
missing = "".join(b for k, b in blocks.items() if k not in site)
if missing and anchor in site:
    site = site.replace(anchor, missing + anchor, 1)
    open(path, "w").write(site)
    print("    nginx: added", ", ".join(k for k in blocks if k in missing))
PY
fi
nginx -t -q && systemctl reload nginx || { cp "/root/texloom-backups/nginx-$DOMAIN.bak" "$SITE"; systemctl reload nginx; echo "nginx update failed: restored"; exit 1; }
rm -rf /root/texloom-backups  # only needed until nginx accepted the change

# TLS certificate (uses the server's existing Let's Encrypt account if there is one)
if [ -z "$SKIP_CERT" ] && [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --redirect --quiet
fi

sleep 1
systemctl is-active --quiet texloom && echo "    texloom service: running" || { journalctl -u texloom -n 30 --no-pager; exit 1; }

# The old install, once this one answers through nginx: remove it.
if [ "$APP_DIR" != "$OLD" ] && [ -d "$OLD" ]; then
  for i in 1 2 3 4 5; do curl -fsS -o /dev/null "http://127.0.0.1:8787/api/info" && break; sleep 1; done
  if curl -fsS -o /dev/null "http://127.0.0.1:8787/api/info" && ! grep -rqs "$OLD/" /etc/nginx/sites-enabled/ /etc/systemd/system/texloom.service; then
    rm -rf "$OLD" && echo "    removed the old install in $OLD"
  else
    echo "    kept $OLD: the new install in $APP_DIR didn't check out"
  fi
fi
REMOTE

echo "==> Done: https://$DOMAIN"
