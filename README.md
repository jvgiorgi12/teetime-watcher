# Tee time cancellation watcher

Watches public golf tee sheets and sends a phone notification the moment a tee time matching your filters opens up,
usually because someone cancelled.

I built it because New Jersey county residents with county golf cards can book days before the general public,
so weekend morning tee times are gone by the time the public booking window opens. Cancellations inside the public
window are the realistic way in, and they get snapped up quickly.

It only **reads** tee sheets. It never books for you: each alert links straight to the booking page.

## What it watches

| Course | Booking platform | Public booking window |
| --- | --- | --- |
| Francis A. Byrne (Essex County) | foreUP | 8 days |
| Hendricks Field (Essex County) | foreUP | 8 days |
| Weequahic (Essex County) | foreUP | 7 days |
| Galloping Hill (Union County) | GolfNow | ~7 days |

Courses are read through the same JSON endpoints their public booking pages use. Course IDs and booking classes
are at the top of `watcher.py`.

## Features

- **Filters:** days of the week, a start-time range, and number of players.
- **Only new openings alert.** Each check is compared with the previous one (saved in `state.json`), so a time
  alerts once when it appears, and again only if it's booked and later freed up.
- **Phone alerts via [ntfy](https://ntfy.sh)**, with a tap-through link to the booking page.
- **Pause/resume from the notification.** Every alert has a "Pause alerts" button. The watcher keeps a streaming
  connection to a private ntfy control topic, so `stop` / `start` take effect within about a second and are
  confirmed with a notification. While paused it doesn't check the courses at all.
- **Health monitoring:**
  - Pings a [healthchecks.io](https://healthchecks.io) check after every cycle (a dead man's switch). If the pings
    stop, because the server froze or lost its network, healthchecks.io alerts you. If every course fails in one
    cycle, the watcher reports the failure right away.
  - If one course fails 5 checks in a row, it sends "Watcher problem: <course>", then "Watcher recovered" once it
    works again.
- **Polite polling:** about every 3 minutes, with random jitter and a 1–3 second pause between requests.
- **One dependency:** Python standard library plus `certifi`.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install certifi
cp config.example.json config.json   # then edit it
.venv/bin/python watcher.py          # run forever (Ctrl+C to stop)
.venv/bin/python watcher.py --once   # single check, then exit
```

The first run alerts on every matching time it finds; after that, only on newly opened ones.

## Settings (`config.json`)

| Key | Meaning |
| --- | --- |
| `days_of_week` | Days to watch, e.g. `["Saturday", "Sunday"]` |
| `earliest_start` / `latest_start` | 24h `HH:MM` range for start times |
| `players` | Open spots you need |
| `check_every_seconds` | Time between checks (plus up to 30s random). Keep it ≥120 to be polite. |
| `courses` | Turn individual courses on/off |
| `notify.mac_notification` | macOS banner and sound, for running on a Mac |
| `notify.ntfy_topic` | ntfy topic for alerts. Install the ntfy app, subscribe to a hard-to-guess topic name, and put the same name here. Anyone who knows it can read your alerts. |
| `notify.control_topic` | Second private ntfy topic used for pause/resume. Leave empty to disable. |
| `notify.heartbeat_url` | healthchecks.io ping URL. Leave empty to disable. |

Changes take effect on the next check, with no restart needed.

## Pausing and resuming

- Tap **Pause alerts** on any alert. You'll get a "paused" notification with a **Resume** button.
- Or send `stop` / `start` to the control topic yourself, e.g. by opening `https://ntfy.sh/<control_topic>` in a
  browser and typing the word.

The paused state is saved in `control.json`, so it survives restarts.

## Running it 24/7 on a server

I run it on a small AWS Lightsail instance (Ubuntu 24.04) as a systemd service.

1. Create an Ubuntu server and add an SSH host alias named `teetime` to `~/.ssh/config`:

   ```
   Host teetime
       HostName <your-server-ip>
       User ubuntu
       IdentityFile ~/.ssh/<your-key>.pem
   ```

2. `cp config.server.example.json config.server.json` and fill in your ntfy topics and healthchecks.io URL.
3. Run `./deploy.sh`.

`deploy.sh` is safe to re-run. On a fresh server it installs Python's venv and `certifi`, sets the timezone to
America/New_York, adds a 1 GB swap file, and disables the `fwupd` firmware updater. It then copies `watcher.py`, the
systemd unit and `config.server.json` (as the server's `config.json`), and restarts the service. It never copies
`state.json`, `control.json` or your local `config.json`.

On the server:

| Task | Command |
| --- | --- |
| Live log | `journalctl -u teetime-watcher -f` |
| Status | `systemctl status teetime-watcher --no-pager` |
| Restart | `sudo systemctl restart teetime-watcher` |

### Lessons learned

- **The courses' own booking sites may block cloud servers.** Galloping Hill's own booking site is behind
  Cloudflare and returns HTTP 403 to AWS IP addresses. GolfNow lists the same tee sheet and accepts plain HTTPS
  requests, so the watcher uses that. It doesn't try to get around bot protection.
- **Small servers need swap.** On a 512 MB instance, Ubuntu's nightly automatic updates ran out of memory. DNS and
  SSH stopped working, and every check failed silently for about 2½ days, since ntfy needs DNS too. A reboot,
  a 1 GB swap file and disabling `fwupd` fixed it. The healthchecks.io heartbeat was added so a silent failure
  like that gets noticed within about 35 minutes.

## Files

| File | Purpose |
| --- | --- |
| `watcher.py` | The watcher (single file) |
| `deploy.sh` | Sets up a server if needed, copies the watcher over and restarts it |
| `teetime-watcher.service` | systemd unit |
| `config.example.json` | Template for running locally |
| `config.server.example.json` | Template for the server config |
