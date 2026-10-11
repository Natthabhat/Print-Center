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

## Checks

```sh
python3 -m unittest test_dashboard test_tools test_live_status test_upload test_plate_start test_box_control test_print_mapping test_camera_monitor -q
node --check app.js
```

Tests use mocks and do not operate printer hardware. Temperature graphs are browser-session history. Filament weights and remaining times may be estimates. Model previews require slicer thumbnails. Hardware capabilities vary by printer firmware.
