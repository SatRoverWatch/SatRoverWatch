# SatRoverWatch

SatRoverWatch is an amateur-radio satellite rover monitoring and alert service. It monitors an opted-in rover through APRS.fi, calculates Maidenhead locators, records position history, detects useful rover events, calculates amateur-satellite passes, generates deterministic OpenStreetMap graphics, can publish alerts to X, and supports optional Textbelt SMS notifications for possible satellite activations.

The current watcher intentionally processes **one enabled rover per run**. The configuration format already uses a rover list so the software can be extended to simultaneous multi-rover operation later without putting rover-specific data in the public source.

## Why SatRoverWatch?

Satellite roving can be difficult for other operators to follow in real time. SatRoverWatch is intended to give an opted-in rover more visibility by turning APRS position data into useful, shareable context such as Maidenhead grid transitions, maps, weather context, and upcoming satellite passes when appropriate.

The project is also intended as a community experiment. Because the source is open, amateur-radio satellite operators can inspect how the system works, run their own instance, adapt it to their needs, suggest features, report problems, and contribute improvements. You do not need to be a programmer to contribute useful feedback.

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
- Can send optional Textbelt SMS notifications when a confirmed stationary stop is classified as a possible satellite activation and qualifying passes are available. Messages include the rover callsign, 6-character Maidenhead locator(s), and up to five satellite passes in UTC.

## Planned enhancements

- **Additional rover-configurable direct notifications** — expand beyond the initial Textbelt possible-activation SMS to support selectable events, such as Maidenhead grid changes or approaching passes of preferred satellites.
- **Preferred-satellite alerts** — allow each rover to maintain its own preferred satellite list (for example, FO-29 or RS-44) and request advance notification when a qualifying pass is approaching.
- Notification destinations, phone numbers, provider credentials, and rover preferences will remain private configuration and will not be stored in the public repository.

## Files

- `satroverwatch.py` — main watcher and event logic.
- `map_generator.py` — deterministic OpenStreetMap and recent-track map generation.
- `pass_predictor.py` — satellite pass calculations.
- `sms_notifier.py` — Textbelt SMS notification helper, including safe test-mode support.
- `messages.json` — editable public message pools.
- `rover_config.example.json` — safe example of the private rover configuration schema.
- `.env.example` — safe credential/configuration template.
- `requirements.txt` — Python dependencies.

Runtime files such as `.env`, `rover_config.json`, `state.json`, `satroverwatch.db`, `logs/`, and `cache/` are intentionally excluded from Git.

## Installation

Python 3.11+ is recommended. The reference deployment has been tested on a Raspberry Pi 5 with Python 3.13. The examples below install SatRoverWatch in `/opt/satroverwatch`. Substitute your own Linux username where `<linux-user>` appears.

### 1. Install Git and clone the repository

A clean Raspberry Pi OS Lite installation may not include Git. Install it first:

```bash
sudo apt update
sudo apt install -y git
```

Create the working directory and make your Linux account its owner. Replace `<linux-user>` with the account that will run SatRoverWatch:

```bash
sudo mkdir -p /opt/satroverwatch
sudo chown <linux-user>:<linux-user> /opt/satroverwatch
```

Clone the public SatRoverWatch repository and enter the working tree:

```bash
git clone https://github.com/SatRoverWatch/SatRoverWatch.git /opt/satroverwatch
cd /opt/satroverwatch
```

No GitHub account is required simply to clone the public repository. If you plan to contribute changes through GitHub, see **Contributing and feedback** below for optional Git identity and SSH setup.

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

At minimum, APRS polling requires `APRSFI_API_KEY`. X publishing uses the four X OAuth values and `X_POSTING_ENABLED`. Protected-X intent classification also requires `OPENAI_API_KEY`; `OPENAI_INTENT_MODEL` selects the model used for that classification. The current example configuration uses `OPENAI_INTENT_MODEL=gpt-5.6-luna`. Optional SMS delivery uses `TEXTBELT_API_KEY` and `SMS_LIVE_ENABLED`; see **SMS notifications** below.

Before commissioning, confirm the private files are ignored:

```bash
git status --short --ignored
```

The output should show `.env` and `rover_config.json` with the `!!` ignored-file marker. Do not commit either file.

### 4. Commission the installation safely

Keep X publishing and live SMS delivery disabled during initial testing:

```text
X_POSTING_ENABLED=false
SMS_LIVE_ENABLED=false
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
./.venv/bin/python -m py_compile satroverwatch.py map_generator.py pass_predictor.py sms_notifier.py
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
User=<linux-user>
Group=<linux-user>
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

Credentials for the official SatRoverWatch installation, including its X, OpenAI/xAI, email, GitHub, and Textbelt accounts, are private and are not included with the software. Do not configure another installation to use SatRoverWatch's accounts, identity, or credentials.

Private credentials belong in `.env` or other private configuration files and must not be committed to Git.

## Rover configuration

`rover_config.json` contains rover-specific settings and is never intended for Git. A rover entry includes its display callsign, APRS callsign/SSID, enable state, optional X identity and protected-timeline permission, optional home geofence, and optional SMS settings.

Only a literal JSON `true` for `enabled` activates a rover. If no rover is enabled, the watcher exits safely before normal monitoring. This version also exits with an error if more than one rover is enabled, because simultaneous multi-rover processing has not yet been implemented.

The optional home geofence should use an appropriately coarse center/radius. Public posts should not disclose a private residence or exact home coordinates.

## SMS notifications

SatRoverWatch supports optional **Textbelt SMS notifications** for an opted-in rover. SMS is considered only after the watcher confirms a stationary/idle stop, classifies it as `POSSIBLE_ACTIVATION` rather than an ordinary travel stop, and obtains qualifying satellite passes. A nearby food, fuel, lodging, or other recognized break stop does not trigger a possible-activation SMS.

An SMS includes the rover's callsign, its six-character Maidenhead locator, and up to five qualifying satellite passes with UTC timing and maximum elevation. A pass already in progress is marked `NOW` and includes its calculated LOS time. When the APRS coordinate is within **100 meters of a four-character Maidenhead grid boundary**, the message includes the six-character locators for both sides; near a four-grid intersection it can include four locators. The message uses plain ASCII to avoid Unicode SMS encoding penalties.

### Configure Textbelt and a rover

Obtain your own Textbelt API key and put it in the private `.env` file:

```dotenv
TEXTBELT_API_KEY=your-own-textbelt-key
SMS_LIVE_ENABLED=false
```

In the private `rover_config.json`, add the following fields **inside the opted-in rover's object** (as shown in `rover_config.example.json`):

```json
"sms": {
  "enabled": false,
  "phone_number": ""
}
```

Set `sms.enabled` to `true` and supply the authorized recipient's phone number when that rover should be eligible for SMS. Keep real phone numbers, API keys, and the private configuration out of Git.

### Test mode, live delivery, and duplicate protection

`SMS_LIVE_ENABLED=false` is the safe default, including when the variable is absent. With SMS enabled for a rover but live mode disabled, the watcher uses Textbelt's test mode rather than delivering a real text. **Only set `SMS_LIVE_ENABLED=true` in the private `.env` after testing and explicitly deciding to allow real, potentially billable SMS delivery.** Textbelt credits and provider limits are the operator's responsibility.

The watcher records `last_sms_event_key` in private `state.json` after Textbelt accepts a notification, to avoid repeating an SMS for the same detected stop. This is separate from X-post deduplication. SMS delivery is optional; a notification failure is logged without intentionally stopping the watcher.

## Publishing safety

`X_POSTING_ENABLED=false` is the safe default in `.env.example`. Keep it false while testing a new installation. Monitoring, X publishing, and SMS delivery have separate controls: `rover_config.json` determines whether a rover is monitored and eligible for SMS, while `.env` controls whether X posts and real SMS messages are permitted.

Protected X content is not copied into public posts. The intent classifier extracts only structured operational facts and does not calculate passes or geography. If classification or deterministic validation fails, no relay is attempted.

## Running

For a manual test:

```bash
./.venv/bin/python -m py_compile satroverwatch.py map_generator.py pass_predictor.py sms_notifier.py
./.venv/bin/python satroverwatch.py
```

A production installation can run the watcher every five minutes. The original deployment used cron; a dedicated Raspberry Pi deployment can use a `systemd` service/timer.

## Runtime data

`state.json` stores the current operational snapshot and event-deduplication state. `satroverwatch.db` stores historical rover positions. `cache/` contains generated maps, OpenStreetMap tiles, and CelesTrak data. `logs/` contains watcher logs. These files are local runtime data and are ignored by Git.

Do not delete an existing production `satroverwatch.db` merely when updating source files if you want to retain historical tracks.

## External data and attribution

Position data is obtained through the APRS.fi API. Base-map imagery is retrieved from OpenStreetMap and generated maps retain OpenStreetMap contributor attribution. Satellite GP data is obtained from CelesTrak. U.S. weather context uses the National Weather Service.

Review the terms, usage policies, and rate limits of external services before operating a public or higher-volume deployment.

## Contributing and feedback

SatRoverWatch welcomes community participation. Useful contributions include feature requests, bug reports, field-testing results, documentation corrections, review of APRS/grid/pass logic, and source-code improvements. You do not need to write code to help improve the project.

For code contributions, fork the repository on GitHub, make changes in your fork, and submit a pull request. Configure a Git author identity on systems where you create commits:

```bash
git config --global user.name "Your Name"
git config --global user.email "your-github-email@example.com"
```

If you prefer GitHub SSH authentication for pushing changes, generate a key, add the public key to your GitHub account, and verify authentication:

```bash
ssh-keygen -t ed25519 -C "your-github-email@example.com"
cat ~/.ssh/id_ed25519.pub
ssh -T git@github.com
```

Please keep credentials, private rover configuration, runtime databases, and other operational data out of commits and pull requests.

## Remove SatRoverWatch

To remove SatRoverWatch from Raspberry Pi OS, Debian, or Ubuntu, see [UNINSTALL.md](UNINSTALL.md).

## Project status

SatRoverWatch is still under active development. The current architecture has been field-tested as a single-rover watcher, but additional testing and cleanup should accompany any expansion to simultaneous multi-rover operation.

## License

SatRoverWatch source code is released under the MIT License. See `LICENSE` for the full license terms.

Copyright (c) 2026 Mitch Ahrenstorff (AD0HJ).
