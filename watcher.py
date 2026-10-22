"""Tee time cancellation watcher.

Polls the public (non-cardholder) tee sheets for the Essex County courses
(foreUP) and Galloping Hill (GolfNow) and alerts when a tee time matching
config.json opens up. It only reads tee sheets; booking is left to you.

Usage:
    .venv/bin/python watcher.py          # run forever
    .venv/bin/python watcher.py --once   # single check, then exit
"""

import argparse
import json
import os
import random
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import certifi

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
CONTROL_PATH = HERE / "control.json"  # paused flag + last control message seen

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

# foreUP: Essex County. booking_class is the public "Non-Cardholders" class;
# window_days is how far out that class can book.
FOREUP_COURSES = {
    "francis_byrne": {"name": "Francis A. Byrne", "schedule_id": 11078, "booking_class": 49771, "window_days": 8},
    "hendricks_field": {"name": "Hendricks Field", "schedule_id": 11075, "booking_class": 49493, "window_days": 8},
    "weequahic": {"name": "Weequahic", "schedule_id": 11077, "booking_class": 49424, "window_days": 7},
}
FOREUP_BOOK_URL = "https://foreupsoftware.com/index.php/booking/22527/{schedule_id}#teetimes"

# GolfNow lists the same Galloping Hill public tee sheet as the course's own
# site (which blocks AWS), and answers plain HTTPS requests.
GOLFNOW_BASE = "https://www.golfnow.com"
GOLFNOW_GALLOPING_HILL_ID = 5095
GOLFNOW_WINDOW_DAYS = 7
GOLFNOW_PLAYER_WORDS = {"One": 1, "Two": 2, "Three": 3, "Four": 4}

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# Private values (anyone who knows them can read alerts, pause the watcher or
# fake heartbeats). On the server, systemd passes them in as credentials (see
# teetime-watcher.service) so they never sit in config.json. When running
# locally without systemd, they can be set in config.json's "notify" section.
SECRET_NAMES = ("ntfy_topic", "control_topic", "heartbeat_url")


def load_secrets():
    """Read secrets from systemd's credentials directory, if we have one."""
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not cred_dir:
        return {}
    secrets = {}
    for name in SECRET_NAMES:
        path = Path(cred_dir) / name
        if path.exists():
            secrets[name] = path.read_text().strip()
    return secrets


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text())
    cfg.setdefault("notify", {}).update(load_secrets())
    return cfg


def target_dates(cfg, window_days):
    wanted = {DAY_NAMES.index(d.capitalize()) for d in cfg["days_of_week"]}
    today = date.today()
    return [today + timedelta(days=i) for i in range(window_days + 1)
            if (today + timedelta(days=i)).weekday() in wanted]


def in_time_range(cfg, dt):
    hhmm = dt.strftime("%H:%M")
    return cfg["earliest_start"] <= hhmm <= cfg["latest_start"]


# --- foreUP -----------------------------------------------------------------

def check_foreup(cfg, key, course):
    found = []
    for d in target_dates(cfg, course["window_days"]):
        params = {
            "time": "all",
            "date": d.strftime("%m-%d-%Y"),
            "holes": "all",
            "players": cfg["players"],
            "booking_class": course["booking_class"],
            "schedule_id": course["schedule_id"],
            "schedule_ids[]": course["schedule_id"],
            "specials_only": 0,
            "api_key": "no_limits",
        }
        url = "https://foreupsoftware.com/index.php/api/booking/times?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Api-Key": "no_limits",
        })
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            times = json.load(r)
        if not isinstance(times, list):  # foreUP returns false/{} when nothing is bookable
            continue
        for t in times:
            dt = datetime.strptime(t["time"], "%Y-%m-%d %H:%M")
            if t.get("available_spots", 0) >= cfg["players"] and in_time_range(cfg, dt):
                found.append({
                    "course": course["name"],
                    "when": dt,
                    "spots": t["available_spots"],
                    "price": t.get("green_fee"),
                    "url": FOREUP_BOOK_URL.format(schedule_id=course["schedule_id"]),
                })
        time.sleep(random.uniform(1, 3))  # be gentle between requests
    return found


# --- GolfNow (Galloping Hill) ------------------------------------------------

def golfnow_max_players(rule):
    # playerRule looks like "One", "OneTwo", "OneTwoThreeFour" or "Any"
    if rule == "Any":
        return 4
    return max((n for w, n in GOLFNOW_PLAYER_WORDS.items() if w in rule), default=0)


def check_golfnow_galloping_hill(cfg):
    found = []
    for d in target_dates(cfg, GOLFNOW_WINDOW_DAYS):
        body = {
            "Radius": 35, "Latitude": 40.67886, "Longitude": -74.28269,
            "PageSize": 100, "PageNumber": 0, "SearchType": 1,
            "SortBy": "Date", "SortDirection": 0, "SortByRollup": "Date.MinDate",
            "Date": d.strftime("%b %d %Y"), "HotDealsOnly": False,
            "PriceMin": 0, "PriceMax": 10000, "Players": cfg["players"],
            "TimePeriod": 3, "Holes": 3, "RateType": "all", "TimeMin": 10, "TimeMax": 42,
            "FacilityId": GOLFNOW_GALLOPING_HILL_ID, "View": "Grouping",
            "ExcludeFeaturedFacilities": True, "TeeTimeCount": 50, "PromotedCampaignsOnly": False,
        }
        req = urllib.request.Request(
            GOLFNOW_BASE + "/api/tee-times/tee-time-search-results",
            data=json.dumps(body).encode(),
            headers={"User-Agent": UA, "Content-Type": "application/json; charset=UTF-8",
                     "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            data = json.load(r)
        for t in data["ttResults"].get("teeTimes") or []:
            if t.get("facilityId") != GOLFNOW_GALLOPING_HILL_ID or t.get("isReservationRestricted"):
                continue
            # The "+00:00" offset is wrong; the clock time is the course's local time.
            dt = datetime.strptime(t["time"]["date"][:16], "%Y-%m-%dT%H:%M")
            spots = golfnow_max_players(t.get("playerRule", ""))
            if spots >= cfg["players"] and in_time_range(cfg, dt):
                found.append({
                    "course": "Galloping Hill",
                    "when": dt,
                    "spots": spots,
                    "price": (t.get("displayRate") or {}).get("value"),
                    "url": GOLFNOW_BASE + t["detailUrl"],
                })
        time.sleep(random.uniform(1, 3))
    return found


# --- notifications ---------------------------------------------------------

def notify(cfg, title, message, url=None, button=None):
    """button is an optional (label, command) shown on the phone notification;
    tapping it sends command to the control topic."""
    n = cfg.get("notify", {})
    if n.get("mac_notification"):
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], check=False)
    topic = n.get("ntfy_topic")
    if topic:
        headers = {"Title": title, "Priority": "high", "Tags": "golf"}
        if url:
            headers["Click"] = url
        control = n.get("control_topic")
        if button and control:
            label, command = button
            headers["Actions"] = f"http, {label}, https://ntfy.sh/{control}, body={command}, clear=true"
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode(), headers=headers)
        try:
            urllib.request.urlopen(req, timeout=15, context=SSL_CTX)
        except Exception as e:
            log(f"ntfy push failed: {e}")


def slot_key(s):
    return f"{s['course']}|{s['when']:%Y-%m-%d %H:%M}"


def describe(s):
    price = f" ${float(s['price']):.0f}" if s.get("price") not in (None, "") else ""
    return f"{s['course']}: {s['when']:%a %b %-d, %-I:%M %p} ({s['spots']} open){price}"


# --- pause / resume -------------------------------------------------------
# Send "stop" or "start" to the ntfy control topic (the buttons on alerts do
# this) to pause or resume checking without logging into the server.

PAUSE_WORDS = {"stop", "pause"}
RESUME_WORDS = {"start", "resume"}


def load_control():
    try:
        return json.loads(CONTROL_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {"paused": False, "last_id": None}


def control_url(cfg, ctl, poll):
    topic = cfg.get("notify", {}).get("control_topic")
    if not topic:
        return None
    since = ctl.get("last_id") or "all"
    return f"https://ntfy.sh/{topic}/json?since={since}" + ("&poll=1" if poll else "")


def apply_control(cfg, ctl, messages):
    """Apply stop/start commands in order. Returns True if paused/resumed changed."""
    was_paused = ctl["paused"]
    for msg in messages:
        if msg.get("event") != "message":
            continue
        ctl["last_id"] = msg["id"]
        word = msg.get("message", "").strip().lower()
        if word in PAUSE_WORDS:
            ctl["paused"] = True
        elif word in RESUME_WORDS:
            ctl["paused"] = False
    CONTROL_PATH.write_text(json.dumps(ctl))
    if ctl["paused"] == was_paused:
        return False
    if ctl["paused"]:
        log("PAUSED by control command; not checking until resumed")
        notify(cfg, "Tee time watcher paused", "No checks or alerts until you resume.",
               button=("Resume", "start"))
    else:
        log("RESUMED by control command")
        notify(cfg, "Tee time watcher resumed", "Checking for weekend tee times again.",
               button=("Pause alerts", "stop"))
    return True


def poll_control(cfg, ctl):
    """Pick up any commands sent since we last looked (e.g. during a check)."""
    url = control_url(cfg, ctl, poll=True)
    if not url:
        return False
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                    timeout=15, context=SSL_CTX) as r:
            messages = [json.loads(line) for line in r.read().decode().splitlines()]
    except Exception as e:
        log(f"ERROR control topic: {e}")
        return False
    return apply_control(cfg, ctl, messages)


# --- health alerts -----------------------------------------------------------
# heartbeat_url (e.g. a healthchecks.io check) gets pinged after every loop. If
# the server freezes or loses its network, the pings stop and that outside
# service alerts you; the watcher itself can't, since it can't reach ntfy then.
# Failures of individual sources are reported by the watcher directly.

FAIL_ALERT_AFTER = 5  # consecutive failed checks of one source (~20 min)


def heartbeat(cfg, ok, message):
    url = cfg.get("notify", {}).get("heartbeat_url")
    if not url:
        return
    req = urllib.request.Request(url.rstrip("/") + ("" if ok else "/fail"),
                                 data=message.encode()[:10000], headers={"User-Agent": UA})
    try:
        urllib.request.urlopen(req, timeout=15, context=SSL_CTX)
    except Exception as e:
        log(f"ERROR heartbeat: {e}")


def enabled_sources(cfg):
    names = [c["name"] for k, c in FOREUP_COURSES.items() if cfg["courses"].get(k)]
    if cfg["courses"].get("galloping_hill_golfnow"):
        names.append("Galloping Hill")
    return names


def report_source_health(cfg, errors, streaks):
    """Alert once when a source keeps failing, and once when it recovers."""
    failed = {e.split(":")[0]: e for e in errors}
    for source in enabled_sources(cfg):
        if source in failed:
            streaks[source] = streaks.get(source, 0) + 1
            if streaks[source] == FAIL_ALERT_AFTER:
                notify(cfg, f"Watcher problem: {source}",
                       f"Failing for the last {FAIL_ALERT_AFTER} checks. {failed[source]}")
        else:
            if streaks.get(source, 0) >= FAIL_ALERT_AFTER:
                notify(cfg, f"Watcher recovered: {source}", "Checks are working again.")
            streaks[source] = 0


# --- main loop -------------------------------------------------------------

def check_once(cfg):
    slots, errors = [], []
    for key, course in FOREUP_COURSES.items():
        if cfg["courses"].get(key):
            try:
                slots += check_foreup(cfg, key, course)
            except Exception as e:
                errors.append(f"{course['name']}: {e}")
    if cfg["courses"].get("galloping_hill_golfnow"):
        try:
            slots += check_golfnow_galloping_hill(cfg)
        except Exception as e:
            errors.append(f"Galloping Hill: {e}")
    return slots, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check once and exit")
    args = ap.parse_args()

    previous = set(json.loads(STATE_PATH.read_text())) if STATE_PATH.exists() else set()
    ctl = load_control()
    streaks = {}
    secrets = load_secrets()
    if secrets:
        log(f"Loaded secrets from systemd credentials: {', '.join(sorted(secrets))}")
    if ctl["paused"]:
        log("Starting PAUSED (send \"start\" to the control topic to resume)")
    while True:
        cfg = load_config()
        poll_control(cfg, ctl)
        if ctl["paused"] and not args.once:
            heartbeat(cfg, True, "paused")
            sleep_watching_control(cfg, ctl)
            continue

        slots, errors = check_once(cfg)
        for e in errors:
            log(f"ERROR {e}")

        current = {slot_key(s): s for s in slots}
        new = [s for k, s in current.items() if k not in previous]
        log(f"{len(current)} matching tee time(s), {len(new)} new")
        for s in sorted(new, key=lambda s: s["when"]):
            log(f"  NEW  {describe(s)}  {s['url']}")
            notify(cfg, f"Tee time open: {s['course']}", describe(s), s["url"],
                   button=("Pause alerts", "stop"))

        # Keep what we knew about courses whose check failed, so their slots
        # don't all look "new" once the check succeeds again.
        failed = {e.split(":")[0] for e in errors}
        previous = set(current) | {k for k in previous if k.split("|")[0] in failed}
        STATE_PATH.write_text(json.dumps(sorted(previous)))

        if args.once:
            return
        report_source_health(cfg, errors, streaks)
        all_failed = bool(errors) and failed >= set(enabled_sources(cfg))
        heartbeat(cfg, not all_failed,
                  "\n".join(errors) if all_failed else f"{len(current)} matching, {len(new)} new")
        sleep_watching_control(cfg, ctl)


def sleep_watching_control(cfg, ctl):
    """Wait until the next check while listening to the control topic, so a
    stop/start (and its confirmation notification) happens within seconds.
    Returns early if paused/resumed changes."""
    end = time.time() + cfg["check_every_seconds"] + random.uniform(0, 30)
    while (remaining := end - time.time()) > 0:
        url = control_url(cfg, ctl, poll=False)
        if not url:
            time.sleep(remaining)
            return
        try:
            # A streaming subscription: ntfy pushes each message (and a
            # keepalive every ~45s) as it happens. The timeout ends the read
            # when it's time for the next check.
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                        timeout=min(remaining, 50) + 1, context=SSL_CTX) as r:
                for line in r:
                    if apply_control(cfg, ctl, [json.loads(line)]):
                        return
                    if time.time() >= end:
                        return
        except TimeoutError:
            continue
        except Exception as e:
            log(f"ERROR control topic: {e}")
            time.sleep(min(30, max(0, end - time.time())))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
