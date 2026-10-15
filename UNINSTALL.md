# Uninstalling SatRoverWatch

These instructions apply to Raspberry Pi OS, Debian, and Ubuntu installations using `/opt/satroverwatch`.

The procedure removes the SatRoverWatch application and its scheduled execution. It does not remove system-wide Python, Git, or other packages that may be used by other software.

## 1. Stop and disable the systemd timer

```bash
sudo systemctl disable --now satroverwatch.timer
sudo systemctl stop satroverwatch.service
```

If systemd reports that a unit does not exist, continue; it may not have been installed or may already have been removed.

Check for a remaining timer:

```bash
systemctl list-timers --all | grep -i satroverwatch
```

No matching output is expected once the timer has been stopped and removed.

## 2. Remove the systemd unit files

The standard installation uses these files:

- `/etc/systemd/system/satroverwatch.service`
- `/etc/systemd/system/satroverwatch.timer`

Remove them and reload systemd:

```bash
sudo rm -f /etc/systemd/system/satroverwatch.service
sudo rm -f /etc/systemd/system/satroverwatch.timer
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

Verify that no SatRoverWatch unit files remain:

```bash
systemctl list-unit-files | grep -i satroverwatch
```

No matching output is expected.

## 3. Check for older cron jobs

An earlier deployment may have used cron. Check the current user's and root's crontabs:

```bash
crontab -l
sudo crontab -l
```

If you find a SatRoverWatch entry, edit the relevant crontab:

```bash
crontab -e
```

For a root-owned entry, use:

```bash
sudo crontab -e
```

Remove only the SatRoverWatch-related line, then save the file. Do not delete unrelated scheduled jobs.

## 4. Back up configuration and history (optional)

Runtime files may include private credentials and rover-location history. If you want to preserve the installation before deleting it, create a backup:

```bash
sudo tar -czf "$HOME/satroverwatch-backup.tar.gz" -C /opt satroverwatch
sudo chown "$USER":"$(id -gn)" "$HOME/satroverwatch-backup.tar.gz"
chmod 600 "$HOME/satroverwatch-backup.tar.gz"
```

This assumes the installation directory is `/opt/satroverwatch`. Store the backup securely: it may contain `.env` credentials and historical rover positions.

Files that may be included in the backup:

- `.env` — API keys and publishing settings
- `rover_config.json` — private rover configuration
- `state.json` — operational state and event-deduplication data
- `satroverwatch.db` — historical APRS positions
- `cache/` and `logs/` — cached data and logs

Skip this step if you do not need to retain the data.

## 5. Remove the application directory

Confirm the directory first:

```bash
ls -ld /opt/satroverwatch
```

If it is the correct installation, remove it:

```bash
sudo rm -rf -- /opt/satroverwatch
```

**Warning:** This permanently deletes the application directory and any remaining configuration, database, logs, cache, and Python virtual environment inside it. Check the path carefully before running the command.

## 6. Revoke dedicated credentials (optional)

If these credentials were used only for SatRoverWatch and are no longer needed, consider revoking them with their providers:

- APRS.fi API key
- X OAuth tokens, if X publishing was configured
- OpenAI API key, if protected-X intent classification was configured
- xAI API key, if one was configured

Do not revoke credentials shared with other applications unless you intend to affect those applications too.

## 7. Verify removal

Run:

```bash
echo "=== Application directory ==="
if [ -d /opt/satroverwatch ]; then
    echo "WARNING: Directory still exists"
else
    echo "OK: Application directory removed"
fi

echo
echo "=== systemd status ==="
systemctl is-enabled satroverwatch.timer 2>&1 || true
systemctl is-active satroverwatch.timer 2>&1 || true
systemctl is-active satroverwatch.service 2>&1 || true

echo
echo "=== Remaining systemd units ==="
systemctl list-unit-files | grep -i satroverwatch || true

echo
echo "=== Remaining scheduled timers ==="
systemctl list-timers --all | grep -i satroverwatch || true
```

The application directory should be absent, the timer should not be enabled or active, and no SatRoverWatch systemd units or timers should remain.

## What not to remove

Do not uninstall system-wide packages such as Python 3, `python3-venv`, Git, or systemd solely for SatRoverWatch. Other software may depend on them. Removing `/opt/satroverwatch` removes the project's own `.venv` and its installed Python dependencies.
