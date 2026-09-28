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

Python 3.11+ is recommended. From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp rover_config.example.json rover_config.json
```

Edit `.env` with the credentials needed by your installation, then edit `rover_config.json` for the rover you have permission to monitor. Keep both files private.

At minimum, APRS polling requires `APRSFI_API_KEY`. X publishing uses the four X OAuth values and `X_POSTING_ENABLED`. Protected-X intent classification also requires `OPENAI_API_KEY`.

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
