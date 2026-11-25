#!/usr/bin/env bash
# Install or update texloom on an Ubuntu/Debian server, from this checkout.
#
#   deploy/install.sh root@server.example.org texloom.example.org
#
# Re-run it to update. Tested on Ubuntu 24.04 and Debian 13, alongside other nginx sites. It
# only adds things: a `texloom` system user, the app in /var/www/texloom (set
# TEXLOOM_DIR for elsewhere), its files and (unless PostgreSQL) SQLite database in /var/lib/texloom, settings
# in /etc/texloom (texloom.toml, written once: edit it there), a systemd service on 127.0.0.1:8787
# and an nginx site for the domain, plus a Let's Encrypt certificate via certbot. Other sites are
# left alone. It keeps no backups: see INSTALL.md. Extra ssh options can be passed in SSH_OPTS;
# set SKIP_CERT=1 to skip certbot. DATABASE=postgresql on the first install uses PostgreSQL (with a
# `texloom` database) instead of SQLite; afterwards, the settings file says which it is.
set -euo pipefail

TARGET=${1:?usage: deploy/install.sh user@host domain}
DOMAIN=${2:?usage: deploy/install.sh user@host domain}
SSH_OPTS=${SSH_OPTS:-}
DATABASE=${DATABASE:-sqlite}
case "$DATABASE" in sqlite|postgresql) ;; *) echo "DATABASE is sqlite or postgresql"; exit 1 ;; esac
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
(cd "$HERE" && uv export --frozen --no-dev --extra server --no-emit-project --format requirements-txt -q -o deploy/.requirements.txt)

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
  --exclude /.python-version --exclude /texloom.toml \
  "$HERE/" "$TARGET:$APP_DIR/"

echo "==> Installing on the server"
ssh_ DOMAIN="$DOMAIN" APP_DIR="$APP_DIR" DATABASE="$DATABASE" TEXLIVE_MIRROR="$TEXLIVE_MIRROR" BUSYTEX_VERSION="$BUSYTEX_VERSION" BUSYTEX_SHA256="$BUSYTEX_SHA256" SKIP_CERT="${SKIP_CERT:-}" bash -s <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1  # leave no caches behind

# the database: what the settings say, once there are settings (TEXLOOM_DATABASE in env overrides them)
CONF=/etc/texloom/texloom.toml
if [ -f "$CONF" ]; then
  if grep -qsE '^\s*(database\s*=\s*"postgres|TEXLOOM_DATABASE=postgres)' "$CONF" /etc/texloom/env; then
    DATABASE=postgresql
  elif ! grep -qsE '^\s*(database\s*=|TEXLOOM_DATABASE=)' "$CONF" /etc/texloom/env \
       && [ -n "$(runuser -u postgres -- psql -qtAX -c "SELECT 1 FROM pg_database WHERE datname='texloom'" 2>/dev/null)" ]; then
    # settings from before SQLite was the default, which used PostgreSQL's `texloom` database without saying so
    # (before any table such as [s3]: in TOML, the lines after one belong to it)
    awk 'BEGIN { line = "# (added by deploy/install.sh: the database this server has used all along)\ndatabase = \"postgresql:///texloom?host=/var/run/postgresql\"\n" }
         !done && /^[[:space:]]*\[/ { printf "%s\n", line; done = 1 } { print } END { if (!done) printf "\n%s", line }' "$CONF" > "$CONF.new"
    mv "$CONF.new" "$CONF"  # (its owner and mode are set below)
    echo "    settings: database = PostgreSQL's texloom database, as before"
    DATABASE=postgresql
  elif ! grep -qsE '^\s*(database\s*=|TEXLOOM_DATABASE=)' "$CONF" /etc/texloom/env && dpkg -s postgresql >/dev/null 2>&1; then
    # no database in the settings, PostgreSQL installed, but its texloom database couldn't be checked:
    # don't guess (the server would start on a new, empty SQLite file)
    echo "$CONF names no database, and PostgreSQL's texloom database couldn't be checked: add database = … there, then run this again"
    exit 1
  else
    DATABASE=sqlite
  fi
fi
echo "    database: $DATABASE"

# packages (only installs what's missing)
need=()
pkgs=(nginx python3-venv rsync curl)
[ "$DATABASE" = postgresql ] && pkgs+=(postgresql)
for p in "${pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
command -v certbot >/dev/null || need+=(certbot python3-certbot-nginx)
if ((${#need[@]})); then apt-get update -qq && apt-get install -y -qq "${need[@]}"; fi

# user, directories, Python environment
id texloom >/dev/null 2>&1 || useradd --system --home /var/lib/texloom --shell /usr/sbin/nologin texloom
install -d -o texloom -g texloom -m 750 /var/lib/texloom
install -d -m 700 /root/texloom-backups
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -q --no-cache-dir --upgrade pip
.venv/bin/pip install -q --no-cache-dir --require-hashes -r deploy/.requirements.txt
.venv/bin/pip install -q --no-cache-dir --no-deps .
chown -R root:root "$APP_DIR" && chmod -R a+rX "$APP_DIR"

# PostgreSQL: a `texloom` database, which the texloom user reaches over the local socket (no password)
if [ "$DATABASE" = postgresql ]; then
  systemctl enable -q --now postgresql
  pg() { runuser -u postgres -- psql -qtAX "$@"; }
  [ "$(pg -c "SELECT 1 FROM pg_roles WHERE rolname='texloom'")" = 1 ] || runuser -u postgres -- createuser texloom
  [ "$(pg -c "SELECT 1 FROM pg_database WHERE datname='texloom'")" = 1 ] || runuser -u postgres -- createdb -O texloom texloom
  AFTER_DB=" postgresql.service"
else
  AFTER_DB=""
fi

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

# settings: written once, then yours to edit (texloom-server --help lists every setting)
install -d -o root -g texloom -m 750 /etc/texloom
if [ ! -f /etc/texloom/texloom.toml ]; then  # from texloom.example.toml, with this install's paths
  sed -e "s#/var/www/texloom#$APP_DIR#g" texloom.example.toml > /etc/texloom/texloom.toml
  if [ "$DATABASE" = postgresql ]; then
    sed -i 's#^database = .*#database = "postgresql:///texloom?host=/var/run/postgresql"#' /etc/texloom/texloom.toml
  fi
  if [ -n "$TEXLIVE_MIRROR" ]; then
    sed -i "s#^\# texlive_mirror = .*#texlive_mirror = [\"$TEXLIVE_MIRROR\"]#" /etc/texloom/texloom.toml
  fi
fi
chown root:texloom /etc/texloom/texloom.toml && chmod 640 /etc/texloom/texloom.toml

# storage allowances: without a code nothing is stored (default 0); print the owner's code
if [ ! -s /etc/texloom/codes.txt ]; then
  echo "    owner's creation code (unlimited): $(.venv/bin/texloom-server --config /etc/texloom/texloom.toml --add-code owner --allowance unlimited)"
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
runuser -u texloom -- .venv/bin/texloom-server --config /etc/texloom/texloom.toml --texlive-init \
  || echo "    couldn't refresh the TeX Live index; keeping the one there is (if any)"

# systemd service
cat > /etc/systemd/system/texloom.service <<UNIT
[Unit]
Description=TeXLoom relay server
After=network-online.target$AFTER_DB
Wants=network-online.target$AFTER_DB

[Service]
User=texloom
WorkingDirectory=$APP_DIR
# settings: /etc/texloom/texloom.toml (TEXLOOM_<NAME> variables in /etc/texloom/env override it)
EnvironmentFile=-/etc/texloom/env
# buffers over 1 MB (catch-ups, uploads) go back to the system when freed, rather than staying
# in the heap, where MemoryMax would keep counting them
Environment=MALLOC_MMAP_THRESHOLD_=1048576
ExecStart=$APP_DIR/.venv/bin/texloom-server --config /etc/texloom/texloom.toml --host 127.0.0.1 --port 8787
Restart=on-failure
UMask=0077
MemoryMax=1G
LimitNOFILE=8192
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
# Rate limits (http context; names prefixed to avoid clashing with other sites). They count per
# client: an IPv4 address, or an IPv6 address's /64 (its first 8 bytes: one home or server gets a
# whole /64, so counting single IPv6 addresses would limit nothing)
map \$binary_remote_addr \$texloom_client { "~(?s)^(?<texloom_net>.{8}).{8}\$" \$texloom_net; default \$binary_remote_addr; }
limit_req_zone \$texloom_client zone=texloom_code64:1m rate=6r/m;
limit_req_zone \$texloom_client zone=texloom_list64:1m rate=1r/s;
limit_req_zone \$texloom_client zone=texloom_texlive64:1m rate=20r/s;
map \$request_method \$texloom_put_key { PUT \$texloom_client; default ""; }
limit_req_zone \$texloom_put_key zone=texloom_upload64:10m rate=10r/s;
limit_conn_zone \$texloom_client zone=texloom_ws64:10m;
limit_conn_zone \$texloom_client zone=texloom_blob64:10m;
# pages are always rechecked: an old copy would ask for build files a deploy has replaced (a blank page)
map \$sent_http_content_type \$texloom_html_expires { ~^text/html epoch; default off; }

server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    root $APP_DIR/web/dist;
    expires \$texloom_html_expires;
    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;

    # security headers (a location that sets its own add_header loses these: it repeats them all)
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; connect-src 'self' https: wss: http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:*; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header Cross-Origin-Opener-Policy same-origin always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()" always;

    # encrypted sync (WebSocket); connections are long-lived
    location /ws/ {
        limit_conn texloom_ws64 30;
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
    # encrypted file blobs (images, PDFs)
    location /blob/ {
        limit_conn texloom_blob64 20;
        limit_req zone=texloom_upload64 burst=40;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        client_max_body_size 75m;
        proxy_request_buffering off;
    }
    # server info and creation-code checks
    location = /api/code {
        limit_req zone=texloom_code64 burst=5 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    # importing an arXiv paper's source: the relay fetches it (rate-limited there too)
    location /api/arxiv/ {
        limit_req zone=texloom_code64 burst=3 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 120s;
    }
    # project lists: slows anyone trying passwords or creation codes
    location /api/list/ {
        limit_req zone=texloom_list64 burst=60 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    # TeX Live files on demand (a package is fetched from a mirror the first time)
    location /texlive/ {
        limit_req zone=texloom_texlive64 burst=1000 delay=400;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 60s;
    }
    # read-only admin page (HTTP basic auth against /etc/texloom/admin-token)
    location = /admin {
        limit_req zone=texloom_code64 burst=5 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }

    # the in-browser engine's scripts: revalidated (deploys can patch them; cheap with ETags)
    location ~ ^/core/(.+\.js)\$ {
        alias $APP_DIR/tex-assets/\$1;
        add_header Cache-Control "no-cache";
        add_header X-Content-Type-Options nosniff always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'; worker-src 'self' blob:; connect-src 'self' https: wss: http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:*; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;
        add_header Referrer-Policy no-referrer always;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header Cross-Origin-Opener-Policy same-origin always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()" always;
    }
    # in-browser TeX engine and packages: big, immutable per version
    location /core/ {
        alias $APP_DIR/tex-assets/;
        expires 30d;
        add_header Cache-Control "public";
        add_header X-Content-Type-Options nosniff always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'; worker-src 'self' blob:; connect-src 'self' https: wss: http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:*; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;
        add_header Referrer-Policy no-referrer always;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header Cross-Origin-Opener-Policy same-origin always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()" always;
    }
    # hashed build output (.mjs is missing from nginx's default MIME table)
    location /assets/ {
        types { application/javascript js mjs; text/css css; }
        expires 1y;
        add_header Cache-Control "public, immutable";
        add_header X-Content-Type-Options nosniff always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; connect-src 'self' https: wss: http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:*; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;
        add_header Referrer-Policy no-referrer always;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header Cross-Origin-Opener-Policy same-origin always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()" always;
    }
    location / {
        try_files \$uri /index.html;
    }
    location ~ /\. { return 404; }
}
NGINX
  ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
else
  # Updating: add routes introduced since the site
  # was first written, before the catch-all /api/ location (certbot's lines stay as they are).
  cp "$SITE" "/root/texloom-backups/nginx-$DOMAIN.bak"
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
        limit_req zone=texloom_texlive burst=1000 delay=400;
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
if "zone=texloom_list" not in site:  # (named texloom_list64 since it counts IPv6 /64s)
    zone = "limit_req_zone $binary_remote_addr zone=texloom_list:1m rate=1r/s;\n"
    code_zone = site.index("limit_req_zone $binary_remote_addr zone=texloom_code:")
    site = site[:code_zone] + zone + site[code_zone:]
    open(path, "w").write(site)
if "zone=texloom_texlive" not in site:  # TeX Live files: anyone can ask, each first ask fetches a package
    at = site.index("limit_req_zone $binary_remote_addr zone=texloom_code:")
    site = site[:at] + "limit_req_zone $binary_remote_addr zone=texloom_texlive:1m rate=20r/s;\n" + site[at:]
    site = site.replace("    location /texlive/ {\n", "    location /texlive/ {\n        limit_req zone=texloom_texlive burst=1000 delay=400;\n", 1)
    open(path, "w").write(site)
if "zone=texloom_blob" not in site:
    at = site.index("limit_conn_zone $binary_remote_addr zone=texloom_ws:")
    site = site[:at] + "limit_conn_zone $binary_remote_addr zone=texloom_blob:10m;\n" + site[at:]
    site = site.replace("    location /blob/ {\n", "    location /blob/ {\n        limit_conn texloom_blob 20;\n", 1)
    open(path, "w").write(site)
# the page's policy: connections to other servers' projects (wss:, and ws: on this computer for testing)
old_csp = "connect-src 'self' https: http://localhost:* http://127.0.0.1:*;"
if old_csp in site:
    site = site.replace(old_csp, "connect-src 'self' https: wss: http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:*;")
    open(path, "w").write(site)
    print("    nginx: page policy allows other servers' websockets")
if "$texloom_html_expires" not in site:  # pages always rechecked (else a stale page loads replaced build files)
    root = f"    root {app_dir}/web/dist;\n"
    if root in site:
        at = site.index("\nserver {")
        site = (site[:at] + "\n# pages are always rechecked: an old copy would ask for build files a deploy has replaced\n"
                "map $sent_http_content_type $texloom_html_expires { ~^text/html epoch; default off; }" + site[at:])
        site = site.replace(root, root + "    expires $texloom_html_expires;\n")
        open(path, "w").write(site)
        print("    nginx: pages are always rechecked")
anchor = "    location /api/ {"
missing = "".join(b for k, b in blocks.items() if k not in site)
if missing and anchor in site:
    site = site.replace(anchor, missing + anchor, 1)
    open(path, "w").write(site)
    print("    nginx: added", ", ".join(k for k in blocks if k in missing))
import re
before = site
# limits count an IPv6 address's /64, not each address (one home or server gets a whole /64). The
# zones are renamed: nginx won't reload a zone whose key changed
if "$texloom_client {" not in site:
    at = min(i for i in (site.find("limit_req_zone "), site.find("map $request_method")) if i >= 0)
    site = (site[:at] + "# limits count per client: an IPv4 address, or an IPv6 address's /64 (its first 8 bytes)\n"
            'map $binary_remote_addr $texloom_client { "~(?s)^(?<texloom_net>.{8}).{8}$" $texloom_net; default $binary_remote_addr; }\n'
            + site[at:])
site = re.sub(r"\btexloom_(code|list|texlive|upload|ws|blob)\b", r"texloom_\g<1>64", site)
site = (site.replace("limit_req_zone $binary_remote_addr ", "limit_req_zone $texloom_client ")
        .replace("limit_conn_zone $binary_remote_addr ", "limit_conn_zone $texloom_client ")
        .replace("{ PUT $binary_remote_addr;", "{ PUT $texloom_client;"))
# every proxied location says who the client is (the relay's own per-address limits); one a client sends is replaced
site = re.sub(r"(\n([ \t]*)proxy_set_header Host \$host;\n)(?![ \t]*proxy_set_header X-Real-IP)",
              lambda m: m.group(1) + m.group(2) + "proxy_set_header X-Real-IP $remote_addr;\n", site)
# security headers: two more for the server, and all of them in each location that sets its own
# add_header (nginx then drops the server's: the in-browser engine's worker scripts had no policy)
csp = re.search(r'^    add_header Content-Security-Policy (".*?") always;$', site, re.M)
perm = '"camera=(), microphone=(), geolocation=(), payment=(), usb=()"'
extra = ["add_header Cross-Origin-Opener-Policy same-origin always;", f"add_header Permissions-Policy {perm} always;"]
referrer = "    add_header Referrer-Policy no-referrer always;\n"
if "Cross-Origin-Opener-Policy" not in site and referrer in site:
    site = site.replace(referrer, referrer + "".join(f"    {h}\n" for h in extra), 1)
if csp:
    every = [f"add_header Content-Security-Policy {csp[1]} always;", "add_header X-Content-Type-Options nosniff always;",
             "add_header Referrer-Policy no-referrer always;", 'add_header Strict-Transport-Security "max-age=31536000" always;', *extra]

    def headers(m):
        body = m.group(2)
        if "add_header" not in body or "Content-Security-Policy" in body:
            return m.group(0)
        mine = every
        if "/core" in m.group(1):  # the engine's worker: eval too (its Perl may use it), still nothing else
            mine = [h.replace("script-src 'self' 'wasm-unsafe-eval'", "script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'") for h in every]
        add = "".join(f"        {h}\n" for h in mine if h.split()[1] not in body)
        return m.group(1) + body + add + m.group(3)
    site = re.sub(r"(\n    location [^\n{]*\{\n)(.*?\n)(    \}\n)", headers, site, flags=re.S)
if site != before:
    open(path, "w").write(site)
    print("    nginx: limits per IPv6 /64, X-Real-IP on every proxied location, security headers in every location")
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

REMOTE

echo "==> Done: https://$DOMAIN"
