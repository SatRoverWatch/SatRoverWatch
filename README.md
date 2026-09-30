# SatRoverWatch

SatRoverWatch is an amateur-radio satellite rover monitoring and alert service. It monitors an opted-in rover through APRS.fi, calculates Maidenhead locators, records position history, detects useful rover events, calculates amateur-satellite passes, generates deterministic OpenStreetMap graphics, and can publish alerts to X.

The current watcher intentionally processes **one enabled rover per run**. The configuration format already uses a rover list so the software can be extended to simultaneous multi-rover operation later without putting rover-specific data in the public source.

## What it does

- Polls APRS.fi for the configured rover's latest position, speed, course, and timestamp.
- Calculates 4- and 6-character Maidenhead locators locally.
- Stores historical APRS positions in SQLite for recent-track reconstruction.
- Detects grid changes, returns after long reporting gaps, stationary/idle stops, and a special final-packet/silence case near mapped lodging.
- Uses OpenStreetMap/Nominatim/Overpass data for map and nearby-place context.
- Uses U.S. National Weather Service data where available for return-event weather context.
- Calculates qualifying passes for AO-73, AO-7, FO-29, ISS, JO-97, RS-44, and SO-50 using CelesTrak GP data and Skyfield.
- Generates deterministic location and recent-track maps; AI is not used to determine geography, rover position, grid boundaries, or satellite visibility.
- Can inspect an opted-in rover's X timeline and use OpenAI only to conservatively classify whether a post explicitly describes a current or future satellite operation. Deterministic pass validation remains authoritative and ambiguous cases fail closed.
- Can publish event alerts to X. Posting has a separate global safety switch in `.env`.

## Planned enhancements

- **Rover-configurable direct notifications** — allow an opted-in rover to select which events should generate a direct phone notification, such as a Maidenhead grid change or an approaching pass of a preferred satellite. A dedicated SMS provider such as Twilio is being considered so notifications can reach the rover through the phone's normal messaging system and be available to hands-free systems such as CarPlay.
- **Preferred-satellite alerts** — allow each rover to maintain its own preferred satellite list (for example, FO-29 or RS-44) and request advance notification when a qualifying pass is approaching.
- Notification destinations, phone numbers, provider credentials, and rover preferences will remain private configuration and will not be stored in the public repository.

## Files

- `satroverwatch.py` — main watcher and event logic.
- `map_generator.py` — deterministic OpenStreetMap and recent-track map generation.
- `pass_predictor.py` — satellite pass calculations.
- `messages.json` — editable public message pools.
- `rover_config.example.json` — safe example of the private rover configuration schema.
- `.env.example` — safe credential/configuration template.
- `requirements.txt` — Python dependencies.

Runtime files such as `.env`, `rover_config.json`, `state.json`, `satroverwatch.db`, `logs/`, and `cache/` are intentionally excluded from Git.

## Installation

Python 3.11+ is recommended. The dedicated Raspberry Pi deployment has been tested on a Raspberry Pi 5 with Python 3.13. The examples below install SatRoverWatch in `/opt/satroverwatch` and run it as a dedicated `satroverwatch` user.

### 1. Clone the repository

Clone the repository into `/opt/satroverwatch`, then make the dedicated service account the owner of the working tree. Substitute the repository URL for your installation as needed.

```bash
sudo mkdir -p /opt/satroverwatch
sudo chown satroverwatch:satroverwatch /opt/satroverwatch
git clone <repository-url> /opt/satroverwatch
cd /opt/satroverwatch
```

### 2. Create the Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

An optional import test verifies the principal dependencies:

```bash
python -c "from PIL import Image; from dotenv import load_dotenv; import requests, requests_oauthlib, skyfield; print('SatRoverWatch dependencies OK')"
```

The production `systemd` service invokes `.venv/bin/python` directly, so activating the virtual environment is not required when the service runs.

### 3. Create the private configuration

```bash
cp .env.example .env
cp rover_config.example.json rover_config.json
chmod 600 .env rover_config.json
```

Edit `.env` with the credentials and settings needed by your installation, then edit `rover_config.json` for a rover you have permission to monitor. Both files are intentionally ignored by Git and must remain private.

At minimum, APRS polling requires `APRSFI_API_KEY`. X publishing uses the four X OAuth values and `X_POSTING_ENABLED`. Protected-X intent classification also requires `OPENAI_API_KEY`; `OPENAI_INTENT_MODEL` selects the model used for that classification.

Before commissioning, confirm the private files are ignored:

```bash
git status --short --ignored
```

The output should show `.env` and `rover_config.json` with the `!!` ignored-file marker. Do not commit either file.

### 4. Commission the installation safely

Keep X publishing disabled during initial testing:

```text
X_POSTING_ENABLED=false
```

First run the watcher with all rover entries set to `"enabled": false`. A healthy installation will load the configuration and exit safely with:

```text
ROVER TRACKING: No rover is enabled in rover_config.json.
```

For a live end-to-end test, enable one authorized rover while leaving `X_POSTING_ENABLED=false`, then run:

```bash
./.venv/bin/python satroverwatch.py
```

A successful live run should query APRS.fi, process the rover state, create or update `satroverwatch.db`, and save `state.json` without publishing to X. Run it a second time to verify persistent state and duplicate APRS-position handling. Restore the rover's intended enable state and production X-posting setting after commissioning.

For a syntax-only check, use:

```bash
./.venv/bin/python -m py_compile satroverwatch.py map_generator.py pass_predictor.py
```

### 5. Install the `systemd` service

SatRoverWatch performs one monitoring cycle and exits, so the Raspberry Pi deployment uses a `Type=oneshot` service rather than a continuously running process.

Create `/etc/systemd/system/satroverwatch.service`:

```ini
[Unit]
Description=SatRoverWatch amateur radio satellite rover watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=satroverwatch
Group=satroverwatch
WorkingDirectory=/opt/satroverwatch
ExecStart=/opt/satroverwatch/.venv/bin/python /opt/satroverwatch/satroverwatch.py

[Install]
WantedBy=multi-user.target
```

Reload and validate the unit:

```bash
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/satroverwatch.service
```

Test one service invocation before installing the timer:

```bash
sudo systemctl start satroverwatch.service
systemctl status satroverwatch.service --no-pager
journalctl -u satroverwatch.service -n 30 --no-pager
```

After a successful oneshot run, `systemctl status` normally reports the service as `inactive (dead)` because the program has completed and exited. The journal should show a successful execution rather than a service failure.

### 6. Install the five-minute `systemd` timer

Create `/etc/systemd/system/satroverwatch.timer`:

```ini
[Unit]
Description=Run SatRoverWatch every five minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=1s
Persistent=true
Unit=satroverwatch.service

[Install]
WantedBy=timers.target
```

Reload and validate both units:

```bash
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/satroverwatch.service /etc/systemd/system/satroverwatch.timer
```

Enable and start the **timer**, not the oneshot service:

```bash
sudo systemctl enable --now satroverwatch.timer
```

Verify the schedule and subsequent unattended runs:

```bash
systemctl status satroverwatch.timer --no-pager
systemctl list-timers satroverwatch.timer --no-pager
journalctl -u satroverwatch.service -n 30 --no-pager
```

The timer should report `active (waiting)` and trigger `satroverwatch.service` approximately every five minutes. When no rover is enabled, each scheduled run exits safely after reporting that no rover is enabled.

### Updating an existing Raspberry Pi installation

Runtime state and private configuration live outside Git tracking. When updating source, preserve `.env`, `rover_config.json`, `state.json`, and `satroverwatch.db`; do not delete the database merely to refresh the application code.

After pulling source changes, update dependencies if `requirements.txt` changed and restart/reload only what is necessary:

```bash
cd /opt/satroverwatch
git pull
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo systemctl daemon-reload
```

If the `systemd` unit files themselves have not changed, the enabled timer will continue launching the updated code on its normal schedule.

## Accounts, credentials, and service costs

SatRoverWatch is open-source software, but it does not provide hosted accounts, API access, credentials, or paid third-party services. Anyone operating their own installation is responsible for obtaining and configuring their own accounts, API keys, access tokens, and other credentials, and for any fees associated with those services.

Credentials for the official SatRoverWatch installation, including its X, OpenAI/xAI, email, GitHub, and any future messaging-provider accounts, are private and are not included with the software. Do not configure another installation to use SatRoverWatch's accounts, identity, or credentials.

Private credentials belong in `.env` or other private configuration files and must not be committed to Git.

## Rover configuration

`rover_config.json` contains rover-specific settings and is never intended for Git. A rover entry includes its display callsign, APRS callsign/SSID, enable state, optional X identity and protected-timeline permission, and optional home geofence.

Only a literal JSON `true` for `enabled` activates a rover. If no rover is enabled, the watcher exits safely before normal monitoring. This version also exits with an error if more than one rover is enabled, because simultaneous multi-rover processing has not yet been implemented.

The optional home geofence should use an appropriately coarse center/radius. Public posts should not disclose a private residence or exact home coordinates.

## Publishing safety

`X_POSTING_ENABLED=false` is the safe default in `.env.example`. Keep it false while testing a new installation. Monitoring and X publishing are separate controls: `rover_config.json` determines whether the rover is monitored, while `.env` determines whether the installation may publish to X.

Protected X content is not copied into public posts. The intent classifier extracts only structured operational facts and does not calculate passes or geography. If classification or deterministic validation fails, no relay is attempted.

## Running

For a manual test:

```bash
./.venv/bin/python -m py_compile satroverwatch.py map_generator.py pass_predictor.py
./.venv/bin/python satroverwatch.py
```

A production installation can run the watcher every five minutes. The original deployment used cron; a dedicated Raspberry Pi deployment can use a `systemd` service/timer.

## Runtime data

`state.json` stores the current operational snapshot and event-deduplication state. `satroverwatch.db` stores historical rover positions. `cache/` contains generated maps, OpenStreetMap tiles, and CelesTrak data. `logs/` contains watcher logs. These files are local runtime data and are ignored by Git.

Do not delete an existing production `satroverwatch.db` merely when updating source files if you want to retain historical tracks.

## External data and attribution

Position data is obtained through the APRS.fi API. Base-map imagery is retrieved from OpenStreetMap and generated maps retain OpenStreetMap contributor attribution. Satellite GP data is obtained from CelesTrak. U.S. weather context uses the National Weather Service.

Review the terms, usage policies, and rate limits of external services before operating a public or higher-volume deployment.

## Project status

SatRoverWatch is still under active development. The current architecture has been field-tested as a single-rover watcher, but additional testing and cleanup should accompany any expansion to simultaneous multi-rover operation.

## License

SatRoverWatch source code is released under the MIT License. See `LICENSE` for the full license terms.

Copyright (c) 2026 Mitch Ahrenstorff (AD0HJ).
