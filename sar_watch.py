#!/usr/bin/python3 -I
"""SAR Watch: a small top-bar app that tells you when SAR train seats open.

Unofficial. Not affiliated with Saudi Arabia Railways (SAR).

How it works
  * SAR sells the North and East trains in the Masi app (masiapp.com).
    SAR Watch asks the open Masi trip search (the same search the Masi website
    uses) for one day and one route, and reads the free seats of each class.
    No login, no token, no booking, no browser.
  * It checks slowly: 60 s minimum, a random +-20 % gap, and longer waits
    after errors (2, 4, 8, then 15 minutes).
Security
  * It runs as your user. No root, no open ports, no passwords, no shell.
  * The settings files are private (mode 600). The URL uses fixed parts only.
iPhone (optional)
  * A push alert through the free "ntfy" app (ntfy.sh). Only the day and the
    train times are sent, to a random private topic.

Terminal use:
  sar_watch.py                     start the top-bar app
  sar_watch.py --once [YYYY-MM-DD] [--route RIY-QSM]   one check, print the result
  sar_watch.py --stations          list the station codes
  sar_watch.py --install           copy the app to ~/.local/share and add launchers
  sar_watch.py --uninstall         remove the app, its settings and its cache
  sar_watch.py --test-phone        send a test alert to the iPhone
  sar_watch.py --update            update from GitHub (asks first)
Server (no desktop, for SSH). After --install, the short command is "sar-watch":
  sar-watch --setup                choose the trip and phone alerts (questions)
  sar-watch --run                  watch in this terminal (no desktop)
  sar-watch --service [off]        run in the background, also after a restart
  sar-watch --status               show the settings and the last check
"""
import argparse
import datetime as dt
import fcntl
import importlib
import json
import os
import pwd
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

__version__ = "0.3.0"
APP_ID = "sar-watch"
APP_NAME = "SAR Watch"
_HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or _HOME + "/.local/share", APP_ID)
CONF_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or _HOME + "/.config", APP_ID)
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or _HOME + "/.cache", APP_ID)
CONF_FILE = os.path.join(CONF_DIR, "config.json")
TOPIC_FILE = os.path.join(CONF_DIR, "ntfy-topic")
PROFILE_DIR = os.path.join(CACHE_DIR, "chrome")                                # old versions (0.2 and before)
SNAP_PROFILE_DIR = os.path.join(_HOME, "snap", "chromium", "common", APP_ID)  # old versions (0.2 and before)
ICON_DIR = os.path.join(DATA_DIR, "icons")
APPS_DIR = os.path.join(_HOME, ".local", "share", "applications")
AUTOSTART_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or _HOME + "/.config", "autostart")
SYSTEMD_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or _HOME + "/.config", "systemd", "user")
BIN_DIR = os.path.join(_HOME, ".local", "bin")
SERVICE = f"{APP_ID}.service"
SCRIPT = os.path.realpath(__file__)
NTFY_URL = "https://ntfy.sh/"
UPDATE_REPO = "abdulrahman-se/saudi-ticket-alert"
UPDATE_URL = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/sar_watch.py"
MAX_UPDATE_BYTES = 1_000_000

MIN_INTERVAL = 60      # seconds between checks (lowest allowed)
MAX_INTERVAL = 3600
DEFAULT_INTERVAL = 60
MAX_BACKOFF = 900      # after errors, wait up to 15 min
HTTP_TIMEOUT = 30      # seconds for one request
MAX_ANSWER_BYTES = 2_000_000

LINES = {"N": "North Line", "E": "East Line"}
# All SAR passenger stations, checked on tickets.sar.com.sa (Nov 2026).
# Riyadh has two stations: RIY (North Line) and RYD (East Line).
STATIONS = {
    "RIY": ("Riyadh", "N"), "MAJ": ("Majmaah", "N"), "QSM": ("Qassim", "N"),
    "HAI": ("Hail", "N"), "JAU": ("Al-Jouf", "N"), "QUR": ("Qurayyat", "N"),
    "RYD": ("Riyadh", "E"), "HAF": ("Hofuf", "E"), "ABQ": ("Abqaiq", "E"), "DMM": ("Dammam", "E"),
}
ROUTE_RE = re.compile(r"^([A-Z]{3})-([A-Z]{3})$")
SKY = "#38bdf8"

# --------------------------------------------------------------------------- settings

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def default_date(today=None):
    """Today if it is Thursday, else the next Thursday."""
    today = today or dt.date.today()
    return today + dt.timedelta(days=(3 - today.weekday()) % 7)


def clamp_interval(value):
    return max(MIN_INTERVAL, min(MAX_INTERVAL, int(value)))


def parse_times(text):
    out = []
    for tok in re.split(r"[,\s،]+", str(text)):
        m = TIME_RE.match(tok.strip())
        if m:
            t = f"{int(m.group(1)):02d}:{m.group(2)}"
            if t not in out:
                out.append(t)
    return out


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_config():
    cfg = {"route": "RIY-QSM", "date": default_date(), "interval": DEFAULT_INTERVAL,
           "times": [], "notify": True, "phone": False}
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if valid_route(raw.get("route")):
        cfg["route"] = raw["route"]
    try:
        d = dt.date.fromisoformat(str(raw.get("date")))
        if d >= dt.date.today():
            cfg["date"] = d
    except ValueError:
        pass
    try:
        cfg["interval"] = clamp_interval(raw.get("interval", DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get("times"), list):
        cfg["times"] = parse_times(",".join(map(str, raw["times"])))
    for key in ("notify", "phone"):
        if isinstance(raw.get(key), bool):
            cfg[key] = raw[key]
    return cfg


def save_config(cfg):
    _write_private(CONF_FILE, json.dumps(dict(cfg, date=cfg["date"].isoformat()), indent=2))


def split_route(route):
    """'RIY-QSM' -> ('RIY', 'QSM'). Both stations must be known, different, and on one line."""
    m = ROUTE_RE.match(str(route))
    if not m or m.group(1) == m.group(2) or m.group(1) not in STATIONS or m.group(2) not in STATIONS \
            or STATIONS[m.group(1)][1] != STATIONS[m.group(2)][1]:
        raise ValueError(f"Unknown route: {route}. Use two stations on one line, e.g. RIY-QSM.")
    return m.group(1), m.group(2)


def valid_route(route):
    try:
        split_route(route)
        return True
    except ValueError:
        return False


def route_label(route):
    dep, arr = split_route(route)
    return f"{STATIONS[dep][0]} → {STATIONS[arr][0]}"


MASI_SEARCH = "https://api.masiapp.com/api/trpc/journey.search"
BOOKING_URL = "https://masiapp.com/"
SAUDI_TIME = dt.timezone(dt.timedelta(hours=3))


def search_url(route, date):
    """The Masi trip search for one day and one route. Fixed parts only."""
    dep, arr = split_route(route)
    query = {"origin": dep, "destination": arr, "departureDate": date.isoformat(),
             "includeFareTiers": True, "passengers": {"adults": 1}}
    return MASI_SEARCH + "?" + urlencode({"input": json.dumps(query, separators=(",", ":"))})


def seats_text(trip):
    return ", ".join(f"{kind.capitalize()} {n}" for kind, n in (trip.get("seats") or {}).items())


def alert_text(cfg, trips):
    return (f"{route_label(cfg['route'])} · {cfg['date']:%a %d %b}\n"
            "Trains: " + "; ".join(f"{t['dep']} → {t['arr']}" + (f" ({seats_text(t)})" if t.get("seats") else "")
                                   for t in trips))


def next_delay(interval, errors, rand=random.uniform):
    """Seconds until the next check: the interval, doubled per error (max 15 min), +-20 %."""
    base = min(interval * 2 ** errors, max(interval, MAX_BACKOFF)) if errors else interval
    return base * rand(0.8, 1.2)

# --------------------------------------------------------------------------- iPhone (ntfy)

TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def phone_topic():
    """Private ntfy topic. Random, easy to type, made once and kept in a 600 file."""
    try:
        with open(TOPIC_FILE, encoding="utf-8") as f:
            topic = f.read().strip()
        if TOPIC_RE.match(topic):
            return topic
    except OSError:
        pass
    abc = "abcdefghjkmnpqrstuvwxyz23456789"                 # no 0/o, 1/l/i
    raw = "".join(secrets.choice(abc) for _ in range(16))   # ~79 bits
    topic = "sar-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))
    _write_private(TOPIC_FILE, topic + "\n")
    return topic


def set_phone_topic(topic):
    topic = topic.strip()
    if not TOPIC_RE.match(topic):
        return False
    _write_private(TOPIC_FILE, topic + "\n")
    return True


def send_phone(title, message, click_url=None):
    """Send a push to the ntfy app. Returns "" when sent, else the error text."""
    body = {"topic": phone_topic(), "title": title, "message": message,
            "priority": 5, "tags": ["train2"]}
    if click_url:
        body["click"] = click_url
        body["actions"] = [{"action": "view", "label": "Open booking page", "url": click_url}]
    req = urllib.request.Request(
        NTFY_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"sar-watch/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # noqa: S310 - fixed https URL
            return "" if resp.status == 200 else f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return str(getattr(e, "reason", e))

# --------------------------------------------------------------------------- checker


class CheckError(Exception):
    pass


def fetch_search(route, date):
    """Ask the Masi trip search. Returns the JSON answer. Raises CheckError."""
    req = urllib.request.Request(search_url(route, date), headers={
        "Accept": "application/json", "x-api-version": "1",
        "User-Agent": f"sar-watch/{__version__} (+https://github.com/{UPDATE_REPO})"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # nosec B310  # noqa: S310 - fixed https URL
            data = resp.read(MAX_ANSWER_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise CheckError("Masi asks to wait (HTTP 429)" if e.code == 429 else f"Masi answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:          # also timeouts
        raise CheckError(f"No connection to Masi ({getattr(e, 'reason', e)})") from None
    if len(data) > MAX_ANSWER_BYTES:
        raise CheckError("The Masi answer is too big")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CheckError("The Masi answer is not JSON") from None


def _saudi_time(text):
    return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(SAUDI_TIME).strftime("%H:%M")


def parse_journeys(answer):
    """Masi answer -> trips, sorted by time. [] = no trains on this day. Raises CheckError."""
    try:
        journeys = answer["result"]["data"]["journeys"]
        if not isinstance(journeys, list):
            raise TypeError("journeys")
        trips = []
        for j in journeys:
            seats = {}
            if str(j.get("status", "")).lower() not in ("cancelled", "canceled"):
                for c in j.get("classAvailability") or []:
                    n = c.get("availableSeats")
                    if isinstance(n, int) and not isinstance(n, bool) and n > 0 and c.get("status") != "soldout":
                        kind = str(c.get("type") or "seat")
                        seats[kind] = seats.get(kind, 0) + n
            trips.append({"dep": _saudi_time(j["departureTime"]), "arr": _saudi_time(j["arrivalTime"]),
                          "train": str(j.get("trainNumber") or "?"), "available": bool(seats), "seats": seats})
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CheckError("Masi sent an answer that SAR Watch does not know (the site may have changed)") from None
    return sorted(trips, key=lambda t: t["dep"])


def _save_failed_answer(answer):
    """Keep the last answer that did not parse, for diagnosis (one file, overwritten)."""
    try:
        _write_private(os.path.join(CACHE_DIR, "last-error.json"), json.dumps(answer, ensure_ascii=False)[:200_000])
    except (OSError, TypeError, ValueError):
        pass


def remove_old_browser_data():
    """Version 0.2 and before used Chrome. Its data is not needed now."""
    for d in (PROFILE_DIR, SNAP_PROFILE_DIR):
        shutil.rmtree(d, ignore_errors=True)


def check(route, date):
    answer = None
    try:
        answer = fetch_search(route, date)
        return {"ok": True, "trips": parse_journeys(answer), "error": "", "at": dt.datetime.now()}
    except CheckError as e:
        err = str(e)
        if answer is not None:
            _save_failed_answer(answer)
    except Exception as e:  # never crash the app on a bad check
        err = f"{type(e).__name__}: {e}"
    return {"ok": False, "trips": [], "error": err, "at": dt.datetime.now()}


def wanted(trips, times):
    return [t for t in trips if t["available"] and (not times or t["dep"] in times)]

# --------------------------------------------------------------------------- update (only when you click)

VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.M)


def parse_version(text):
    m = VERSION_RE.search(text)
    return tuple(int(x) for x in m.groups()) if m else None


CURRENT_VERSION = parse_version(f'__version__ = "{__version__}"')


def fetch_update():
    """Download the newest sar_watch.py from GitHub. Returns (version, text). Runs no code."""
    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": f"sar-watch/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310  # noqa: S310 - fixed https URL
            data = resp.read(MAX_UPDATE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise CheckError(f"GitHub answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise CheckError(f"No connection to GitHub ({getattr(e, 'reason', e)})") from None
    if len(data) > MAX_UPDATE_BYTES:
        raise CheckError("The update file is too big")
    try:
        text = data.decode("utf-8")
        compile(text, "sar_watch.py", "exec")          # syntax check only; nothing runs
    except (UnicodeDecodeError, SyntaxError, ValueError):
        raise CheckError("The update file is broken") from None
    version = parse_version(text)
    if version is None or f'APP_ID = "{APP_ID}"' not in text:
        raise CheckError("The update file is not SAR Watch")
    return version, text


def installed_path():
    return os.path.join(DATA_DIR, "sar_watch.py")


def install_update(text):
    """Replace the installed app. The old file stays as sar_watch.py.bak."""
    target = installed_path()
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(target):
        shutil.copy2(target, target + ".bak")
    _write_private(target, text)
    os.chmod(target, 0o700)
    return target


def version_text(version):
    return ".".join(map(str, version))

# --------------------------------------------------------------------------- icons

_TRAIN = (
    '<path fill="#fff" fill-rule="evenodd" d="M5.5 1h5A2.5 2.5 0 0 1 13 3.5v6a2.5 2.5 0 0 1-2.5 2.5h-5'
    'A2.5 2.5 0 0 1 3 9.5v-6A2.5 2.5 0 0 1 5.5 1zM4.6 3.2v3.3h6.8V3.2zM5.6 8.4a.95.95 0 1 0 0 1.9'
    '.95.95 0 0 0 0-1.9zm4.8 0a.95.95 0 1 0 0 1.9.95.95 0 0 0 0-1.9z"/>'
    '<path fill="none" stroke="#fff" stroke-width="1.4" stroke-linecap="round" d="M5.6 12.6 4.2 15M10.4 12.6 11.8 15"/>'
)
_DOT = '<circle cx="12.6" cy="3.4" r="3.1" fill="{}" stroke="#1d1d1d" stroke-width="1"/>'


def _svg(body):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">{body}</svg>\n'


ICONS = {
    "off": _svg(f'<g opacity=".5">{_TRAIN}</g>'),     # not watching
    "on": _svg(_TRAIN),                               # watching, no seats
    "found": _svg(_TRAIN + _DOT.format(SKY)),         # seats found -> sky-blue dot
    "error": _svg(_TRAIN + _DOT.format("#f59e0b")),   # last check failed -> amber dot
    "app": _svg('<rect width="16" height="16" rx="3.6" fill="#1e293b"/>'
                f'<g transform="translate(2.4 2.6) scale(.7)">{_TRAIN}</g>'
                f'<circle cx="12.4" cy="3.6" r="2.2" fill="{SKY}"/>'),
}


def icon_path(name):
    return os.path.join(ICON_DIR, f"{APP_ID}-{name}.svg")


def ensure_icons():
    os.makedirs(ICON_DIR, exist_ok=True)
    for name, svg in ICONS.items():
        path = icon_path(name)
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == svg:
                    continue
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)

# --------------------------------------------------------------------------- install / cli


def _launchers():
    return [os.path.join(APPS_DIR, f"{APP_ID}.desktop"), os.path.join(AUTOSTART_DIR, f"{APP_ID}.desktop")]


def install():
    target = os.path.join(DATA_DIR, "sar_watch.py")
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.realpath(target) != SCRIPT:
        shutil.copyfile(SCRIPT, target)
    os.chmod(target, 0o700)   # only you can read or run it
    remove_old_browser_data()
    ensure_icons()
    entry = (
        "[Desktop Entry]\nType=Application\nName=SAR Watch\n"
        "Comment=See when SAR train seats open\n"
        f"Exec=/usr/bin/python3 -I {target}\nIcon={icon_path('app')}\n"
        "Terminal=false\nCategories=Utility;\nStartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    for path in _launchers():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(entry)
    link = os.path.join(BIN_DIR, APP_ID)
    if _our_link(link):
        os.remove(link)
    if not os.path.lexists(link):           # never replace a file that is not ours
        os.makedirs(BIN_DIR, exist_ok=True)
        os.symlink(target, link)
    print(f"Installed: {target}")
    print("Open 'SAR Watch' from your app menu. It also starts at login.")
    print(f"Terminal command: {APP_ID}  (if not found, use {link})")


def _our_link(link):
    return os.path.islink(link) and os.readlink(link) == installed_path()


def uninstall(assume_yes=False):
    unit = os.path.join(SYSTEMD_DIR, SERVICE)
    link = os.path.join(BIN_DIR, APP_ID)
    paths = [p for p in [*_launchers(), unit, DATA_DIR, CONF_DIR, CACHE_DIR, SNAP_PROFILE_DIR] if os.path.exists(p)]
    if _our_link(link):
        paths.insert(0, link)
    if not paths:
        print("Nothing to remove.")
        return 0
    print("These files and folders will be removed:")
    for p in paths:
        print("  " + p)
    if not assume_yes:
        if input("Remove them? [y/N] ").strip().lower() != "y":
            print("Nothing removed.")
            return 1
    if unit in paths and shutil.which("systemctl"):
        _systemctl("disable", "--now", SERVICE)
    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    print("Removed. If SAR Watch is open, click Quit in its menu.")
    return 0


def run_once(date_text, route):
    date = dt.date.fromisoformat(date_text) if date_text else default_date()
    res = check(route, date)
    print(f"{route_label(route)} · {date:%a %d %b %Y}  ({res['at']:%H:%M:%S})")
    if not res["ok"]:
        print("Error:", res["error"])
        return 2
    if not res["trips"]:
        print("No trains open for this day yet.")
        return 1
    for t in res["trips"]:
        print(f"  {t['dep']} → {t['arr']}  train {t['train']:>3}  " + (f"SEATS ({seats_text(t)})" if t["available"] else "full"))
    return 0 if any(t["available"] for t in res["trips"]) else 1

# --------------------------------------------------------------------------- server (no desktop, for SSH)

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _to_journal():
    """True when the output goes to the systemd journal (it adds its own time)."""
    try:
        st = os.fstat(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return False
    return os.environ.get("JOURNAL_STREAM") == f"{st.st_dev}:{st.st_ino}"


def log(text):
    stamp = "" if _to_journal() else f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  "
    print(stamp + text, flush=True)


def parse_day(text, today=None):
    """'2026-11-26', or a day name ('thu', 'Thursday') for the next such day. Raises ValueError."""
    today = today or dt.date.today()
    t = text.strip().lower()
    if len(t) >= 3 and t.isalpha() and t[:3] in DAY_NAMES:
        return today + dt.timedelta(days=(DAY_NAMES.index(t[:3]) - today.weekday()) % 7)
    d = dt.date.fromisoformat(t)
    if d < today:
        raise ValueError("That day has passed.")
    return d


def passed_date():
    """The saved day when it is in the past, else None."""
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            d = dt.date.fromisoformat(str(json.load(f).get("date")))
    except (OSError, ValueError, AttributeError):
        return None
    return d if d < dt.date.today() else None


def trips_line(trips, times):
    if not trips:
        return "No trains open for this day yet."
    mine = [t for t in trips if not times or t["dep"] in times]
    if not mine:
        return f"No train at {', '.join(times)} on this day."
    return " · ".join(f"{t['dep']} " + (f"SEATS ({seats_text(t)})" if t["available"] else "full") for t in mine)


def _state_file():
    return os.path.join(CACHE_DIR, "last.json")


def run_headless(max_checks=None, sleep=time.sleep):
    """Watch with no desktop. One log line per check. Alerts go to the phone (ntfy)."""
    log(f"SAR Watch {__version__} started (no desktop).")
    remove_old_browser_data()
    key, alerted, errors, told, checks = None, set(), 0, None, 0
    while max_checks is None or checks < max_checks:
        cfg = load_config()                     # read every time, so 'sar-watch --setup' changes apply
        old = passed_date()
        if old:
            if told != old:
                told = old
                msg = f"The day {old:%a %d %b} has passed. Choose a new day: sar-watch --setup"
                log(msg)
                if cfg["phone"]:
                    send_phone("SAR Watch", msg)
            checks += 1
            sleep(600)
            continue
        if (cfg["route"], cfg["date"]) != key:
            key, alerted, errors = (cfg["route"], cfg["date"]), set(), 0
            log(f"Watching {route_label(cfg['route'])} · {cfg['date']:%a %d %b %Y} · trains: "
                f"{', '.join(cfg['times']) or 'all'} · phone alerts: "
                f"{'on' if cfg['phone'] else 'OFF (turn on: sar-watch --setup)'}")
        res = check(cfg["route"], cfg["date"])
        checks += 1
        if res["ok"]:
            errors = 0
            text = trips_line(res["trips"], cfg["times"])
            got = wanted(res["trips"], cfg["times"])
            new = [t for t in got if t["dep"] not in alerted]
            alerted = {t["dep"] for t in got}
            if new and cfg["phone"]:
                err = send_phone("🚆 SAR: seats available!", alert_text(cfg, new), BOOKING_URL)
                if err:
                    alerted -= {t["dep"] for t in new}      # try again at the next check
                text += " · phone alert " + (f"FAILED ({err})" if err else "sent")
        else:
            errors += 1
            text = "Check failed: " + res["error"]
        log(text)
        _write_private(_state_file(), json.dumps(
            {"at": res["at"].isoformat(timespec="seconds"), "text": text}))
        if max_checks is None or checks < max_checks:
            sleep(next_delay(cfg["interval"], errors))
    return 0


def _ask(prompt, now):
    return input(f"{prompt} [{now}]: ").strip()


def _yes(prompt, default=True):
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer in ("y", "yes")


def find_station(text):
    """A number from the list, a code (QSM) or a name (Qassim). None when unknown."""
    codes = list(STATIONS)
    t = text.strip()
    if t.isdigit() and 1 <= int(t) <= len(codes):
        return codes[int(t) - 1]
    if t.upper() in STATIONS:
        return t.upper()
    names = [c for c, (name, _line) in STATIONS.items() if name.lower() == t.lower()]
    return names[0] if len(names) == 1 else None


def setup():
    """Questions in the terminal (for SSH). Enter keeps the value in [brackets]."""
    cfg = load_config()
    codes = list(STATIONS)
    print(f"SAR Watch {__version__} setup. Press Enter to keep the value in [brackets].\n")
    for line, line_name in LINES.items():
        print(f"{line_name}:  " + "   ".join(f"{i}) {name} ({code})" for i, (code, (name, ln))
                                            in enumerate(STATIONS.items(), 1) if ln == line))
    print()

    def label(code):
        return f"{codes.index(code) + 1} {STATIONS[code][0]}"

    dep, arr = split_route(cfg["route"])
    while True:
        answer = _ask("From", label(dep))
        pick = find_station(answer) if answer else dep
        if pick:
            dep = pick
            break
        print("  Type a number from the list, for example 1.")
    if not valid_route(f"{dep}-{arr}"):
        arr = next(c for c in codes if c != dep and STATIONS[c][1] == STATIONS[dep][1])
    while True:
        answer = _ask("To", label(arr))
        pick = find_station(answer) if answer else arr
        if pick and valid_route(f"{dep}-{pick}"):
            arr = pick
            break
        print(f"  Choose a station on the {LINES[STATIONS[dep][1]]}, not {STATIONS[dep][0]}.")
    date = cfg["date"]
    while True:
        answer = _ask("Day (YYYY-MM-DD, or a day name like thu)", f"{date:%Y-%m-%d %a}")
        try:
            date = parse_day(answer) if answer else date
            break
        except ValueError:
            print("  Use a day like 2026-11-26 or thu. The day must be today or later.")
    times = cfg["times"]
    while True:
        answer = _ask("Train times, for example 17:30 (all = every train)", ", ".join(times) or "all")
        if not answer or answer.lower() == "all" or parse_times(answer):
            times = times if not answer else [] if answer.lower() == "all" else parse_times(answer)
            break
        print("  Use HH:MM, for example 17:30. Put a comma between two times.")
    interval = cfg["interval"]
    while True:
        answer = _ask(f"Seconds between checks ({MIN_INTERVAL}-{MAX_INTERVAL})", interval)
        try:
            interval = clamp_interval(answer) if answer else interval
            break
        except ValueError:
            print("  Type a number, for example 60.")
    phone = _yes("Send alerts to your phone with the free ntfy app?")
    if phone:
        print("  In the ntfy app: tap +, type the topic, tap Subscribe.")
        print("  To get the same alerts as another computer, type its topic here.")
        while True:
            answer = _ask("  Phone topic", phone_topic())
            if not answer or set_phone_topic(answer):
                break
            print("  Use 8 to 64 letters, numbers, - or _.")
    cfg.update(route=f"{dep}-{arr}", date=date, times=times, interval=interval, phone=phone)
    save_config(cfg)
    print(f"\nSaved: {route_label(cfg['route'])} · {date:%a %d %b %Y} · trains: {', '.join(times) or 'all'}"
          f" · every {interval} s · phone: {'on' if phone else 'off'}")
    if phone and _yes("Send a test alert to your phone now?"):
        err = send_phone("SAR Watch test", "It works. Seat alerts will come here.")
        print("  Sent. Look at your phone." if not err else "  Failed: " + err)
    state = service_state()
    if state == "active":
        _systemctl("restart", SERVICE)
        print("The background service uses the new settings now.")
    elif state != "none" and _yes("Run it in the background now, and after each restart?"):
        return service("on")
    else:
        print(f"Start it with: {APP_ID} --run   (background: {APP_ID} --service)")
    return 0


def _systemctl(*args):
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)  # nosec B603 B607
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _user_systemd():
    return bool(shutil.which("systemctl")) and _systemctl("show-environment")[0]


def service_state():
    """'active', 'inactive', 'failed', ... or 'none' when systemd for this user is not there."""
    if not _user_systemd():
        return "none"
    r = subprocess.run(["systemctl", "--user", "is-active", SERVICE],  # nosec B603 B607
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def service_unit(target):
    return ("[Unit]\nDescription=SAR Watch: SAR train seat alerts\n\n[Service]\n"
            f'ExecStart="{sys.executable or "/usr/bin/python3"}" -I "{target}" --run\n'
            "Restart=on-failure\nRestartSec=60\nNice=10\n\n[Install]\nWantedBy=default.target\n")


def _user_name():
    return pwd.getpwuid(os.getuid()).pw_name


def linger_on():
    return os.path.exists(f"/var/lib/systemd/linger/{_user_name()}")


def service(action="on"):
    """Run --run as a systemd user service (no root), or stop and remove it."""
    if not _user_systemd():
        print("systemd for your user is not there. Log in with SSH as this user (not with su or sudo),\n"
              f"then try again. Or run it in tmux or screen: {APP_ID} --run")
        return 2
    unit = os.path.join(SYSTEMD_DIR, SERVICE)
    if action == "off":
        _systemctl("disable", "--now", SERVICE)
        if os.path.exists(unit):
            os.remove(unit)
        _systemctl("daemon-reload")
        print("The background service is stopped and removed.")
        return 0
    if not os.path.exists(installed_path()):
        install()
    os.makedirs(SYSTEMD_DIR, exist_ok=True)
    with open(unit, "w", encoding="utf-8") as f:
        f.write(service_unit(installed_path()))
    for args in (("daemon-reload",), ("enable", SERVICE), ("restart", SERVICE)):
        ok, err = _systemctl(*args)
        if not ok:
            print(f"Error: systemctl --user {' '.join(args)}: {err}")
            return 2
    print("The background service runs now.")
    if not linger_on():
        r = subprocess.run(["loginctl", "--no-ask-password", "enable-linger"],  # nosec B603 B607
                           capture_output=True, text=True) if shutil.which("loginctl") else None
        if r is None or r.returncode != 0:
            print(f"To keep it running after you log out, run one time:\n  sudo loginctl enable-linger {_user_name()}")
    if linger_on():
        print("It keeps running after you log out, and it starts after a restart.")
    print(f"Status: {APP_ID} --status    Logs: journalctl --user -u {APP_ID} -f")
    return 0


def status():
    cfg = load_config()
    old = passed_date()
    day = f"{old:%a %d %b %Y} (passed: run {APP_ID} --setup)" if old else f"{cfg['date']:%a %d %b %Y}"
    print(f"SAR Watch {__version__}")
    print(f"Trip:     {route_label(cfg['route'])} · {day}")
    print(f"Trains:   {', '.join(cfg['times']) or 'all'}")
    print(f"Checks:   every {cfg['interval']} s (plus or minus 20 %)")
    print(f"Phone:    {'on · topic ' + phone_topic() if cfg['phone'] else 'off'}")
    state = service_state()
    if state != "none":
        note = "" if state != "active" or linger_on() else \
            f" (stops when you log out: sudo loginctl enable-linger {_user_name()})"
        print(f"Service:  {state}{note}")
    try:
        with open(_state_file(), encoding="utf-8") as f:
            last = json.load(f)
        print(f"Last:     {last['at'].replace('T', ' ')} · {last['text']}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Last:     no background check yet")
    return 0

# --------------------------------------------------------------------------- top-bar app

MISSING_LIBS = """SAR Watch needs GTK 3, AppIndicator and libnotify for Python.
Install them, then start SAR Watch again:
  Ubuntu / Debian:  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7
  Fedora:           sudo dnf install python3-gobject gobject-introspection gtk3 libayatana-appindicator-gtk3 libnotify
  Arch:             sudo pacman -S python-gobject gtk3 libayatana-appindicator libnotify"""


AppIndicator3 = Gio = GLib = Gtk = Notify = None   # set by _load_gi()


def _load_gi():
    global AppIndicator3, Gio, GLib, Gtk, Notify
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Notify", "0.7")
    errors = []
    indicator = None
    for name in ("AyatanaAppIndicator3", "AppIndicator3"):   # Ayatana first: Ubuntu, Debian, Arch
        try:
            gi.require_version(name, "0.1")
            indicator = importlib.import_module("gi.repository." + name)
            break
        except (ValueError, ImportError) as e:
            errors.append(f"{name}: {e}")
    if indicator is None:
        raise ImportError("; ".join(errors))
    from gi.repository import Gio as gio, GLib as glib, Gtk as gtk, Notify as notify
    AppIndicator3, Gio, GLib, Gtk, Notify = indicator, gio, glib, gtk, notify


class Tray:
    """The top-bar app: icon, menu, settings window and the check loop."""

    def __init__(self):
        self.cfg = load_config()
        self.watching = False
        self.busy = False
        self.timer = None
        self.next_at = None
        self.errors = 0
        self.last = None
        self.alerted = set()
        self.note = None
        self.win = None
        self.menu = None
        self.updating = False
        Notify.init(APP_NAME)
        self.ind = AppIndicator3.Indicator.new(
            APP_ID, icon_path("off"), AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_title(APP_NAME)
        self.ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.refresh()

    # ---- view
    def found(self):
        return bool(self.last and self.last["ok"] and wanted(self.last["trips"], self.cfg["times"]))

    def refresh(self):
        if self.found():
            state, desc = "found", "SAR: seats found"
        elif self.last and not self.last["ok"]:
            state, desc = "error", "SAR: check failed"
        elif self.watching:
            state, desc = "on", "SAR: watching"
        else:
            state, desc = "off", "SAR: idle"
        self.ind.set_icon_full(icon_path(state), desc)
        old, self.menu = self.menu, self.build_menu()
        self.ind.set_menu(self.menu)
        if old is not None:
            old.destroy()   # no slow leak over days of watching

    def status_text(self):
        if self.busy:
            return "Checking…"
        r = self.last
        if r is None:
            return "Not checked yet"
        if not r["ok"]:
            return f"⚠ {r['error']}"
        if not r["trips"]:
            return "No trains open for this day yet"
        got = wanted(r["trips"], self.cfg["times"])
        if got:
            return "✓ Seats found: " + ", ".join(t["dep"] for t in got)
        return "Your trains are full" if self.cfg["times"] else "All trains are full"

    def time_text(self):
        parts = []
        if self.last:
            parts.append(f"Last check {self.last['at']:%H:%M:%S}")
        if self.watching and self.next_at:
            parts.append(f"next ~{dt.datetime.fromtimestamp(self.next_at):%H:%M:%S}")
        elif not self.watching:
            parts.append("not watching")
        return "  ·  ".join(parts)

    def build_menu(self):
        menu = Gtk.Menu()

        def info(text):
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            menu.append(item)

        def action(text, callback, sensitive=True):
            item = Gtk.MenuItem(label=text)
            item.connect("activate", lambda *_: callback())
            item.set_sensitive(sensitive)
            menu.append(item)

        cfg = self.cfg
        info(f"{route_label(cfg['route'])}  ·  {cfg['date']:%a %d %b}")
        info(self.status_text())
        if self.last and self.last["ok"]:
            for t in self.last["trips"]:
                mark = "✓ seats" if t["available"] else "✗ full"
                skip = "" if not cfg["times"] or t["dep"] in cfg["times"] else "   (not watched)"
                info(f"     {t['dep']} → {t['arr']}   {mark}{skip}")
        info(self.time_text())
        menu.append(Gtk.SeparatorMenuItem())
        action("Stop watching" if self.watching else "Start watching", self.toggle_watch)
        action("Check now", self.check_now, not self.busy)
        action("Choose day & settings…", self.open_settings)
        action("Open booking page", self.open_site)
        menu.append(Gtk.SeparatorMenuItem())
        action("Check for updates", self.check_updates, not self.updating)
        action("Quit", self.quit)
        menu.show_all()
        return menu

    # ---- checking
    def cancel_timer(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    def toggle_watch(self):
        if self.watching:
            self.watching = False
            self.cancel_timer()
            self.next_at = None
            self.refresh()
        else:
            self.start_watch()

    def start_watch(self):
        self.watching = True
        self.errors = 0
        if self.busy:
            self.refresh()   # the running check schedules the next one
        else:
            self.check_now()

    def check_now(self):
        if self.busy:
            return
        self.cancel_timer()
        self.next_at = None
        if self.cfg["date"] < dt.date.today():
            self.watching = False
            self.last = {"ok": False, "trips": [], "at": dt.datetime.now(),
                         "error": "That day has passed. Choose a new day."}
            self.refresh()
            return
        self.busy = True
        self.refresh()
        route, date = self.cfg["route"], self.cfg["date"]

        def work():
            res = check(route, date)
            GLib.idle_add(self.on_result, res, route, date)

        threading.Thread(target=work, daemon=True).start()

    def on_result(self, res, route, date):
        self.busy = False
        if (route, date) == (self.cfg["route"], self.cfg["date"]):
            self.last = res
            if res["ok"]:
                self.errors = 0
                got = wanted(res["trips"], self.cfg["times"])
                new = [t for t in got if t["dep"] not in self.alerted]
                self.alerted = {t["dep"] for t in got}
                if new and self.cfg["notify"]:
                    self.notify(new)
                if new and self.cfg["phone"]:
                    threading.Thread(target=send_phone, daemon=True, args=(
                        "🚆 SAR: seats available!", alert_text(self.cfg, new),
                        BOOKING_URL)).start()
            else:
                self.errors += 1
        self.schedule()
        return False

    def schedule(self):
        self.cancel_timer()
        self.next_at = None
        if self.watching:
            delay = next_delay(self.cfg["interval"], self.errors)
            self.next_at = time.time() + delay
            self.timer = GLib.timeout_add(int(delay * 1000), self.on_timer)
        self.refresh()

    def on_timer(self):
        self.timer = None
        self.check_now()
        return False

    # ---- actions
    def notify(self, trips):
        n = Notify.Notification.new("🚆 SAR: seats available!", alert_text(self.cfg, trips),
                                    icon_path("app"))
        n.set_urgency(Notify.Urgency.CRITICAL)          # stays until you close it
        n.set_hint("sound-name", GLib.Variant("s", "complete"))
        n.set_hint("desktop-entry", GLib.Variant("s", APP_ID))
        n.add_action("default", "Open booking page", lambda *_: self.open_site())
        n.add_action("open", "Open booking page", lambda *_: self.open_site())
        try:
            n.show()
        except GLib.Error:
            pass
        self.note = n

    def open_site(self):
        try:
            Gio.AppInfo.launch_default_for_uri(BOOKING_URL, None)
        except GLib.Error:
            pass

    def open_settings(self):
        if self.win:
            self.win.present()
            return
        cfg = self.cfg
        w = Gtk.Window(title="SAR Watch")
        w.set_border_width(16)
        w.set_resizable(False)
        w.set_position(Gtk.WindowPosition.CENTER)
        w.set_keep_above(True)
        try:
            w.set_icon_from_file(icon_path("app"))
        except GLib.Error:
            pass
        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        w.add(grid)

        def label(text, row, top=False):
            lab = Gtk.Label(label=text, xalign=1)
            if top:
                lab.set_valign(Gtk.Align.START)
            grid.attach(lab, 0, row, 1, 1)

        label("From", 0)
        src, dst = Gtk.ComboBoxText(), Gtk.ComboBoxText()
        for code, (name, line) in STATIONS.items():
            src.append(code, f"{name} · {LINES[line]}")
        dep, arr = split_route(cfg["route"])
        src.set_active_id(dep)

        def fill_to(keep=None):   # "To" lists only the stations on the same line
            here = src.get_active_id()
            dst.remove_all()
            for code, (name, line) in STATIONS.items():
                if line == STATIONS[here][1] and code != here:
                    dst.append(code, name)
            if not (keep and dst.set_active_id(keep)):
                dst.set_active(0)

        def swap(*_):
            a, b = src.get_active_id(), dst.get_active_id()
            src.set_active_id(b)
            fill_to(a)

        fill_to(arr)
        src.connect("changed", lambda *_: fill_to(dst.get_active_id()))
        swap_btn = Gtk.Button(label="⇄")
        swap_btn.set_tooltip_text("Swap From and To")
        swap_btn.connect("clicked", swap)
        route_row = Gtk.Box(spacing=6)
        route_row.pack_start(src, True, True, 0)
        route_row.pack_start(Gtk.Label(label="to"), False, False, 0)
        route_row.pack_start(dst, True, True, 0)
        route_row.pack_start(swap_btn, False, False, 0)
        grid.attach(route_row, 1, 0, 1, 1)

        label("Day", 1, top=True)
        cal = Gtk.Calendar()

        def set_cal(d):
            cal.select_month(d.month - 1, d.year)
            cal.select_day(d.day)

        set_cal(cfg["date"])
        grid.attach(cal, 1, 1, 1, 1)
        thu = Gtk.Button(label="Next Thursday", halign=Gtk.Align.START)
        thu.connect("clicked", lambda *_: set_cal(default_date()))
        grid.attach(thu, 1, 2, 1, 1)

        label("Check every", 3)
        spin = Gtk.SpinButton.new_with_range(MIN_INTERVAL, MAX_INTERVAL, 10)
        spin.set_value(cfg["interval"])
        row = Gtk.Box(spacing=8)
        row.pack_start(spin, False, False, 0)
        row.pack_start(Gtk.Label(label=f"seconds  (minimum {MIN_INTERVAL})"), False, False, 0)
        grid.attach(row, 1, 3, 1, 1)

        label("Only times", 4)
        times = Gtk.Entry()
        times.set_text(", ".join(cfg["times"]))
        times.set_placeholder_text("e.g. 17:30, 21:00   (empty = any train)")
        grid.attach(times, 1, 4, 1, 1)

        notify = Gtk.CheckButton(label="Notify me when seats are found")
        notify.set_active(cfg["notify"])
        grid.attach(notify, 1, 5, 1, 1)
        hint = Gtk.Label(xalign=0)
        hint.set_markup(f'<small>With or without notify, the top-bar icon gets a '
                        f'<span foreground="{SKY}">●</span> sky-blue dot when seats are found.</small>')
        grid.attach(hint, 1, 6, 1, 1)

        phone = Gtk.CheckButton(label="Send to iPhone or Android (ntfy app)")
        phone.set_active(cfg["phone"])
        grid.attach(phone, 1, 7, 1, 1)
        label("Phone topic", 8)
        topic_row = Gtk.Box(spacing=8)
        topic = Gtk.Entry(width_chars=26)
        topic.set_text(phone_topic())
        test = Gtk.Button(label="Send test")
        topic_row.pack_start(topic, True, True, 0)
        topic_row.pack_start(test, False, False, 0)
        grid.attach(topic_row, 1, 8, 1, 1)
        test_msg = Gtk.Label(xalign=0)
        grid.attach(test_msg, 1, 9, 1, 1)

        def test_done(err):
            if self.win is w:   # window still open
                test.set_sensitive(True)
                test_msg.set_markup("<small>Sent. Look at your phone.</small>" if not err else
                                    f"<small>Failed: {GLib.markup_escape_text(err)}</small>")
            return False

        def on_test(*_):
            if not set_phone_topic(topic.get_text()):
                test_msg.set_markup("<small>Topic: use 8–64 letters, digits, - or _.</small>")
                return
            test.set_sensitive(False)
            test_msg.set_markup("<small>Sending…</small>")
            threading.Thread(daemon=True, target=lambda: GLib.idle_add(test_done, send_phone(
                "SAR Watch test", "It works. Seat alerts will come here."))).start()

        test.connect("clicked", on_test)

        buttons = Gtk.Box(spacing=8, margin_top=6)
        b_close = Gtk.Button(label="Close")
        b_once = Gtk.Button(label="Check once")
        b_watch = Gtk.Button(label="Restart watching" if self.watching else "Start watching")
        b_watch.get_style_context().add_class("suggested-action")
        version = Gtk.Label(label=f"v{__version__}")
        version.get_style_context().add_class("dim-label")
        b_update = Gtk.Button(label="Check for updates")
        b_update.get_style_context().add_class("flat")
        b_update.connect("clicked", lambda *_: self.check_updates())
        buttons.pack_start(version, False, False, 0)
        buttons.pack_start(b_update, False, False, 0)
        for b in (b_watch, b_once, b_close):        # pack_end: right to left
            buttons.pack_end(b, False, False, 0)
        grid.attach(buttons, 0, 10, 2, 1)

        def apply():
            y, m0, d = cal.get_date()
            chosen = f"{src.get_active_id()}-{dst.get_active_id()}"
            new = dict(self.cfg, route=chosen if valid_route(chosen) else self.cfg["route"],
                       date=dt.date(y, m0 + 1, d),
                       interval=clamp_interval(spin.get_value_as_int()),
                       times=parse_times(times.get_text()), notify=notify.get_active(),
                       phone=phone.get_active())
            if (new["route"], new["date"]) != (self.cfg["route"], self.cfg["date"]):
                self.last, self.alerted, self.errors = None, set(), 0
            self.cfg = new
            set_phone_topic(topic.get_text())
            try:
                save_config(new)
            except OSError:
                pass

        def close(*_):
            apply()
            self.win = None
            w.destroy()
            self.refresh()
            return True

        def once(*_):
            close()
            self.check_now()

        def watch(*_):
            close()
            self.start_watch()

        b_close.connect("clicked", close)
        b_once.connect("clicked", once)
        b_watch.connect("clicked", watch)
        w.connect("delete-event", close)
        self.win = w
        w.show_all()

    # ---- update
    def message(self, title, text, ask=None):
        """A small dialog. With ask=<button label> it returns True when that button is clicked."""
        d = Gtk.MessageDialog(transient_for=self.win, modal=True, text=title, secondary_text=text,
                              message_type=Gtk.MessageType.QUESTION if ask else Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.NONE)
        d.set_title(APP_NAME)
        if ask:
            d.add_button("Later", Gtk.ResponseType.CANCEL)
            d.add_button(ask, Gtk.ResponseType.OK).get_style_context().add_class("suggested-action")
        else:
            d.add_button("OK", Gtk.ResponseType.OK)
        answer = d.run() == Gtk.ResponseType.OK
        d.destroy()
        return answer

    def check_updates(self):
        if self.updating:
            return
        self.updating = True
        self.refresh()

        def work():
            try:
                version, text = fetch_update()
                err = ""
            except CheckError as e:
                version, text, err = None, None, str(e)
            except Exception as e:   # never crash the app on a bad update
                version, text, err = None, None, f"{type(e).__name__}: {e}"
            GLib.idle_add(self.on_update_info, version, text, err)

        threading.Thread(target=work, daemon=True).start()

    def on_update_info(self, version, text, err):
        self.updating = False
        self.refresh()
        if err:
            self.message("Could not check for updates", err)
        elif version <= CURRENT_VERSION:
            self.message("SAR Watch is up to date", f"You have the newest version ({__version__}).")
        elif os.path.realpath(installed_path()) != SCRIPT:
            self.message(f"Version {version_text(version)} is ready",
                         "You started SAR Watch from a folder. Run “git pull” in that folder.")
        elif self.message(f"Version {version_text(version)} is ready",
                          f"You have {__version__}. Update now? SAR Watch restarts.", ask="Update"):
            try:
                install_update(text)
            except OSError as e:
                self.message("Update failed", str(e))
                return False
            self.restart()
        return False

    def restart(self):
        args = [sys.executable, "-I", installed_path()] + (["--watch"] if self.watching else [])
        self.watching = False
        self.cancel_timer()
        os.execv(sys.executable, args)   # the lock file closes on exec, so the new app can start

    def quit(self):
        self.watching = False
        self.cancel_timer()
        if self.note:
            try:
                self.note.close()
            except GLib.Error:
                pass
        Gtk.main_quit()


def run_gui(selftest=False, watch=False):
    try:
        _load_gi()
    except (ImportError, ValueError) as e:
        print(f"{MISSING_LIBS}\n\n({e})", file=sys.stderr)
        return 3

    lock_dir = os.environ.get("XDG_RUNTIME_DIR") or CACHE_DIR
    os.makedirs(lock_dir, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.path.join(lock_dir, f"{APP_ID}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("SAR Watch is already running.")
        return 0

    ensure_icons()
    remove_old_browser_data()

    tray = Tray()
    if watch:                        # after an update: keep watching
        GLib.idle_add(lambda: (tray.start_watch(), False)[1])
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (tray.quit(), False)[1])
    if selftest:   # build every view once, then exit (no network)
        tray.last = {"ok": True, "at": dt.datetime.now(), "error": "",
                     "trips": [{"dep": "17:30", "arr": "19:57", "train": "36", "available": True}]}
        for _ in range(50):          # many refreshes: menus must be replaced, not piled up
            tray.refresh()
        tray.open_settings()

        def press_close(widget):
            if isinstance(widget, Gtk.Button) and widget.get_label() == "Close":
                widget.clicked()
            elif isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    press_close(child)

        def widgets(widget, kind, out):
            if isinstance(widget, kind):
                out.append(widget)
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    widgets(child, kind, out)
            return out

        def route_test():
            src, dst = widgets(tray.win, Gtk.ComboBoxText, [])[:2]
            ids = lambda combo: [row[1] for row in combo.get_model()]  # noqa: E731
            src.set_active_id("DMM")
            east_ok = ids(dst) == ["RYD", "HAF", "ABQ"] and dst.get_active_id() in ("RYD", "HAF", "ABQ")
            dst.set_active_id("HAF")
            [b for b in widgets(tray.win, Gtk.Button, []) if b.get_label() == "⇄"][0].clicked()
            swap_ok = (src.get_active_id(), dst.get_active_id()) == ("HAF", "DMM")
            src.set_active_id("RIY")
            dst.set_active_id("QSM")
            return east_ok and swap_ok

        def finish():
            routes_ok = route_test()
            press_close(tray.win)
            ok = routes_ok and os.path.exists(CONF_FILE) and tray.win is None and tray.found()
            print("selftest:", "PASS" if ok else "FAIL", "| from/to =", routes_ok, "| saved =", os.path.exists(CONF_FILE),
                  "| window closed =", tray.win is None, "| status =", tray.status_text())
            tray.quit()
            if not ok:
                sys.exit(1)
            return False

        GLib.timeout_add(1500, finish)
    Gtk.main()
    return 0


def main():
    ap = argparse.ArgumentParser(description="SAR Watch: see when SAR train seats open.")
    ap.add_argument("--once", nargs="?", const="", metavar="YYYY-MM-DD", help="check one day and print the result")
    ap.add_argument("--route", default="RIY-QSM", metavar="FROM-TO", help="station codes, e.g. RIY-QSM or DMM-RYD")
    ap.add_argument("--stations", action="store_true", help="list the station codes")
    ap.add_argument("--install", action="store_true", help="copy the app to ~/.local/share and add launchers")
    ap.add_argument("--uninstall", action="store_true", help="remove the app, its settings and its cache")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--test-phone", action="store_true", help="send a test alert to the phone (ntfy)")
    ap.add_argument("--update", action="store_true", help="update from GitHub (asks first)")
    ap.add_argument("--setup", action="store_true", help="server: choose the trip and phone alerts (questions)")
    ap.add_argument("--run", action="store_true", help="server: watch in this terminal, no desktop")
    ap.add_argument("--service", nargs="?", const="on", choices=("on", "off"),
                    help="server: run in the background, also after a restart (off: stop and remove)")
    ap.add_argument("--status", action="store_true", help="show the settings and the last check")
    ap.add_argument("--checks", type=int, help=argparse.SUPPRESS)   # tests: stop after N checks
    ap.add_argument("--watch", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"SAR Watch {__version__}")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.install:
        install()
        return 0
    if args.uninstall:
        return uninstall(assume_yes=args.yes)
    if args.test_phone:
        print("Phone topic:", phone_topic())
        err = send_phone("SAR Watch test", "It works. Seat alerts will come here.")
        print("Sent." if not err else "Failed: " + err)
        return 0 if not err else 2
    if args.update:
        try:
            version, text = fetch_update()
        except CheckError as e:
            print("Could not check for updates:", e)
            return 2
        if version <= CURRENT_VERSION:
            print(f"SAR Watch is up to date ({__version__}).")
            return 0
        if not args.yes and input(f"Version {version_text(version)} is ready (you have {__version__}). "
                                  "Install it? [y/N] ").strip().lower() != "y":
            print("Nothing changed.")
            return 1
        print(f"Installed {version_text(version)}: {install_update(text)}")
        if service_state() == "active":
            _systemctl("restart", SERVICE)
            print("The background service uses the new version now.")
        else:
            print("Quit SAR Watch and open it again.")
        return 0
    if args.setup or args.run or args.service or args.status:
        try:
            if args.setup:
                return setup()
            if args.service:
                return service(args.service)
            if args.status:
                return status()
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # stop cleanly
            return run_headless(max_checks=args.checks)
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            return 1
    if args.stations:
        for line, line_name in LINES.items():
            print(line_name + ":", ", ".join(f"{name} ({code})" for code, (name, ln) in STATIONS.items() if ln == line))
        return 0
    if args.once is not None:
        if not valid_route(args.route):
            print(f"Unknown route: {args.route}. Use two stations on one line. See --stations.", file=sys.stderr)
            return 2
        try:
            return run_once(args.once, args.route)
        except ValueError:
            print("Use the date format YYYY-MM-DD.", file=sys.stderr)
            return 2
    return run_gui(selftest=args.selftest, watch=args.watch)


if __name__ == "__main__":
    sys.exit(main())
