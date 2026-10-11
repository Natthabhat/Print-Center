# Print Center

A self-hosted dashboard for managing multiple Klipper 3D printers through Moonraker. Designed for an Ubuntu server on a private LAN or Tailscale network.

## Features

- Printer overview with camera thumbnails, print status, progress and layers.
- Add, edit and remove printer connections, with optional Moonraker API keys.
- MJPEG cameras and Snapmaker U1 LAN camera support with freshness checks.
- Current-print model preview, elapsed time, remaining-time estimates and filament grams.
- Editable heater targets, selectable temperature sensors and a five-minute graph with hover readings.
- Toolhead movement, homing, extrusion, tuning and customizable fans/output controls, including U1 caselight.
- Printer files in grid or detail view, model previews and client uploads.
- Qidi multimaterial box configuration and filament-to-slot selection before printing.
- Printer statistics, paginated print history and console logs.
- Shared Moonraker WebSocket subscriptions and browser SSE updates, with HTTP fallback.

## Run on Ubuntu

Requires Python 3.10 or later and `websocket-client`.

```sh
git clone https://github.com/Natthabhat/Print-Center.git
cd Print-Center
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PRINTER_HOST=192.168.1.100 DASHBOARD_BIND=127.0.0.1 .venv/bin/python app.py
```

Open `http://127.0.0.1:8787`. In **Manage printers**, enter each printer's Moonraker IP and port (usually 7125). The server must be able to reach the printer and camera.

Set `DASHBOARD_BIND` to the server's Tailscale IP for remote access over your private network. `DASHBOARD_PORT` defaults to 8787. There is no separate website login; restrict access with your network's access rules. Do not expose the service directly to the public internet.

Printer connections and API keys are saved locally in `printers.json` with owner-only permissions. Keys are not returned to the browser. This file is excluded from version control.

## Optional user service

Copy `print-center.service` to `~/.config/systemd/user/`. Edit its bind address to your server's private IP. The example expects this project at `~/Print-Center` with a `.venv` there.

```sh
systemctl --user daemon-reload
systemctl --user enable --now print-center
journalctl --user -u print-center -n 50
```

For startup without a GUI login, configure systemd user lingering for your account if needed.

## Docker on Ubuntu

For an HTTPS reverse proxy, configure `DASHBOARD_PUBLIC_HOST` with the exact hostname, `DASHBOARD_AUTH_USER`, and `DASHBOARD_AUTH_HASH`. The hash format is `hex_salt:hex_digest`, using PBKDF2-HMAC-SHA256 with 200,000 iterations. Public-host requests use the website login form over HTTPS, with a Secure, HttpOnly, SameSite session cookie that expires after eight hours. Sign out revokes the session. Missing credentials disable public access. The proxy must preserve the public Host header. Explicit LAN and Tailscale addresses retain private-network access without this login. Settings and hashes belong in the excluded `.env` file.

To serve both Tailscale and LAN, set `DASHBOARD_BIND` to the server's Tailscale IP and `DASHBOARD_EXTRA_BIND` to its LAN IP in `.env`. Both use `DASHBOARD_PORT`; other interfaces are not bound. Recreate the container after changing these values.

Docker Compose builds the image from this repository. It uses host networking for LAN printers and binds the web service only to `DASHBOARD_BIND`.

```sh
mkdir -p data
sudo chown 1000:1000 data
printf 'DASHBOARD_BIND=YOUR_TAILSCALE_IP\nDASHBOARD_PORT=8787\n' > .env
docker compose build --pull
docker compose up -d
docker compose ps
```

To migrate an existing installation, copy its `printers.json` into `data/`, owned by UID/GID 1000, before starting the container. Stop the previous service before using the same port. Private settings stay in the mounted data directory across rebuilds.

For updates after authenticating Git to this private repository, run `git pull`, `docker compose build --pull`, then `docker compose up -d`. Browser-downloaded source archives can also be used as the build context. The image is built locally; no registry image is published.

## Checks

```sh
python3 -m unittest test_dashboard test_tools test_live_status test_upload test_plate_start test_box_control test_print_mapping test_camera_monitor test_web_auth -q
node --check app.js
```

Tests use mocks and do not operate printer hardware. Temperature graphs are browser-session history. Filament weights and remaining times may be estimates. Model previews require slicer thumbnails. Hardware capabilities vary by printer firmware.
