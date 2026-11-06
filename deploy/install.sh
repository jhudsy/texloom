#!/usr/bin/env bash
# Install or update texloom on an Ubuntu/Debian server, from this checkout.
#
#   deploy/install.sh root@server.example.org texloom.example.org
#
# Re-run it to update. Used for https://texloom.jhudsy.org (Ubuntu 24.04, aarch64,
# alongside other nginx sites). It only adds things: a `texloom` system user, /opt/texloom,
# /var/lib/texloom, a systemd service on 127.0.0.1:8787 and an nginx site for the
# domain, plus a Let's Encrypt certificate via certbot. Other sites are left alone.
# Extra ssh options can be passed in SSH_OPTS. Set SKIP_CERT=1 to skip certbot.
set -euo pipefail

TARGET=${1:?usage: deploy/install.sh user@host domain}
DOMAIN=${2:?usage: deploy/install.sh user@host domain}
SSH_OPTS=${SSH_OPTS:-}
HERE=$(cd "$(dirname "$0")/.." && pwd)
BUSYTEX_VERSION=$(node -p "require('$HERE/web/node_modules/texlyre-busytex/package.json').version")
ssh_() { ssh $SSH_OPTS "$TARGET" "$@"; }

echo "==> Building the web app locally"
(cd "$HERE/web" && npx tsc && npx vite build >/dev/null)
(cd "$HERE" && uv export --frozen --no-dev --no-emit-project --format requirements-txt -q -o deploy/.requirements.txt)

echo "==> Copying code to $TARGET:/opt/texloom"
ssh_ 'mkdir -p /opt/texloom'
rsync -az --delete -e "ssh $SSH_OPTS" \
  --exclude .git --exclude .venv --exclude node_modules --exclude tex-assets --exclude data \
  --exclude .claude --exclude '__pycache__' --exclude .DS_Store --exclude build --exclude '*.egg-info' \
  "$HERE/" "$TARGET:/opt/texloom/"

echo "==> Installing on the server"
ssh_ DOMAIN="$DOMAIN" BUSYTEX_VERSION="$BUSYTEX_VERSION" SKIP_CERT="${SKIP_CERT:-}" bash -s <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# packages (only installs what's missing)
need=()
for p in nginx python3-venv rsync curl; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
command -v certbot >/dev/null || need+=(certbot python3-certbot-nginx)
if ((${#need[@]})); then apt-get update -qq && apt-get install -y -qq "${need[@]}"; fi

# user, directories, Python environment
id texloom >/dev/null 2>&1 || useradd --system --home /var/lib/texloom --shell /usr/sbin/nologin texloom
install -d -o texloom -g texloom -m 750 /var/lib/texloom
cd /opt/texloom
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q --require-hashes -r deploy/.requirements.txt
.venv/bin/pip install -q --no-deps .
chown -R root:root /opt/texloom && chmod -R a+rX /opt/texloom

# in-browser TeX (WebAssembly TeX Live): downloaded once, ~700 MB unpacked
if [ "$(cat tex-assets/.version 2>/dev/null)" != "$BUSYTEX_VERSION" ]; then
  echo "    downloading TeX assets v$BUSYTEX_VERSION"
  rm -rf tex-assets.new && mkdir -p tex-assets.new
  curl -fsSL "https://github.com/TeXlyre/texlyre-busytex/releases/download/assets-v$BUSYTEX_VERSION/busytex-assets.tar.gz" \
    | tar -xz -C tex-assets.new
  [ -f tex-assets.new/busytex/busytex.wasm ] || { echo "unexpected archive layout"; ls -R tex-assets.new | head; exit 1; }
  echo "$BUSYTEX_VERSION" > tex-assets.new/.version
  # the archive unpacks as uid 1000 with a world-writable directory: make it root-owned, read-only
  chown -R root:root tex-assets.new && chmod -R u=rwX,go=rX tex-assets.new
  rm -rf tex-assets && mv tex-assets.new tex-assets
fi

# creation codes: storing a new project needs one (see README); print the first one
install -d -o root -g texloom -m 750 /etc/texloom
if [ ! -s /etc/texloom/codes.txt ]; then
  echo "    first creation code: $(.venv/bin/texloom-server --codes-file /etc/texloom/codes.txt --db /var/lib/texloom/texloom.db --add-code owner)"
fi
chown root:texloom /etc/texloom/codes.txt && chmod 640 /etc/texloom/codes.txt

# systemd service
cat > /etc/systemd/system/texloom.service <<UNIT
[Unit]
Description=texloom relay server
After=network-online.target
Wants=network-online.target

[Service]
User=texloom
WorkingDirectory=/opt/texloom
ExecStart=/opt/texloom/.venv/bin/texloom-server --host 127.0.0.1 --port 8787 --db /var/lib/texloom/texloom.db --static /opt/texloom/web/dist --tex-assets /opt/texloom/tex-assets --codes-file /etc/texloom/codes.txt --max-bytes 2G --min-free 2G --project-max-bytes 200M
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
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
map \$request_method \$texloom_put_key { PUT \$binary_remote_addr; default ""; }
limit_req_zone \$texloom_put_key zone=texloom_upload:10m rate=10r/s;
limit_conn_zone \$binary_remote_addr zone=texloom_ws:10m;

server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    root /opt/texloom/web/dist;
    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;

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
    location /api/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host \$host;
    }

    # in-browser TeX engine and packages: big, immutable per version
    location /core/ {
        alias /opt/texloom/tex-assets/;
        expires 30d;
        add_header Cache-Control "public";
    }
    # hashed build output (.mjs is missing from nginx's default MIME table)
    location /assets/ {
        types { application/javascript js mjs; text/css css; }
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
    location / {
        try_files \$uri /index.html;
    }
    location ~ /\. { return 404; }
}
NGINX
  ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
fi
nginx -t -q && systemctl reload nginx

# TLS certificate (uses the server's existing Let's Encrypt account if there is one)
if [ -z "$SKIP_CERT" ] && [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --redirect --quiet
fi

sleep 1
systemctl is-active --quiet texloom && echo "    texloom service: running" || { journalctl -u texloom -n 30 --no-pager; exit 1; }
REMOTE

echo "==> Done: https://$DOMAIN"
