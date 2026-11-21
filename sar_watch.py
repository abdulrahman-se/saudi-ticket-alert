#!/usr/bin/python3 -I
"""SAR Watch: a small top-bar app that tells you when SAR train seats open.

Unofficial. Not affiliated with Saudi Arabia Railways (SAR).

How it works
  * It opens the public SAR "select trip" page in a hidden Chrome or Chromium
    (own profile, sandbox on) and reads which trains are full.
    It uses the same page you open by hand. No private API, no login, no booking.
  * It checks slowly: 60 s minimum, a random +-20 % gap, and longer waits
    after errors. It skips images, fonts, maps and chat scripts.
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
"""
import argparse
import datetime as dt
import fcntl
import importlib
import json
import os
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

__version__ = "0.1.0"
APP_ID = "sar-watch"
APP_NAME = "SAR Watch"
_HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or _HOME + "/.local/share", APP_ID)
CONF_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or _HOME + "/.config", APP_ID)
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or _HOME + "/.cache", APP_ID)
CONF_FILE = os.path.join(CONF_DIR, "config.json")
TOPIC_FILE = os.path.join(CONF_DIR, "ntfy-topic")
PROFILE_DIR = os.path.join(CACHE_DIR, "chrome")
SNAP_PROFILE_DIR = os.path.join(_HOME, "snap", "chromium", "common", APP_ID)  # snap Chromium cannot write ~/.cache
ICON_DIR = os.path.join(DATA_DIR, "icons")
APPS_DIR = os.path.join(_HOME, ".local", "share", "applications")
AUTOSTART_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or _HOME + "/.config", "autostart")
SCRIPT = os.path.realpath(__file__)
NTFY_URL = "https://ntfy.sh/"
UPDATE_REPO = "abdulrahman-se/saudi-ticket-alert"
UPDATE_URL = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/sar_watch.py"
MAX_UPDATE_BYTES = 1_000_000

MIN_INTERVAL = 60      # seconds between checks (lowest allowed)
MAX_INTERVAL = 3600
DEFAULT_INTERVAL = 60
MAX_BACKOFF = 900      # after errors, wait up to 15 min
CHECK_TIMEOUT = 90     # kill the browser if one check takes longer

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


def build_url(route, date):
    dep, arr = split_route(route)
    query = {
        "ArrivalStation": arr, "DepartureStation": dep,
        "DepartureDateString": date.isoformat(),
        "ArrivalDateString": (date + dt.timedelta(days=7)).isoformat(),
        "AdultCount": 1, "ChildCount": 0, "InfantCount": 0, "DisabledCount": 0,
        "CarerCount": 0, "passengersCount": 1, "Lang": "ar", "serviceType": 1,
        "WithCarCargo": "false", "TripDirection": "N", "PromoCode": "undefined",
    }
    return "https://tickets.sar.com.sa/select-trip?" + urlencode(query)


def alert_text(cfg, trips):
    return (f"{route_label(cfg['route'])} · {cfg['date']:%a %d %b}\n"
            "Trains: " + ", ".join(f"{t['dep']} → {t['arr']}" for t in trips))


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


CHROME_NAMES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
# Third-party files the trip list does not need: maps, chat bot, tracker, icon font, web fonts.
SKIP_HOSTS = ("maps.googleapis.com", "*.labibabot.com", "cdn.appdynamics.com",
              "unpkg.com", "fonts.googleapis.com", "fonts.gstatic.com")

_proc_lock = threading.Lock()
_proc = None


def find_browser():
    """Return (browser path, profile dir). SAR_WATCH_BROWSER can name another Chromium-based browser."""
    custom = os.environ.get("SAR_WATCH_BROWSER")
    names = (custom,) if custom else CHROME_NAMES
    path = next((p for p in map(shutil.which, names) if p), None)
    if not path:
        raise CheckError("Chrome or Chromium not found. Install one of them.")
    is_snap = os.path.realpath(path).startswith("/snap/") or path.startswith("/snap/")
    return path, (SNAP_PROFILE_DIR if is_snap else PROFILE_DIR)


def _kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def stop_running_check():
    with _proc_lock:
        if _proc is not None:
            _kill_group(_proc)


def reset_browser_data():
    for d in (PROFILE_DIR, SNAP_PROFILE_DIR):
        shutil.rmtree(d, ignore_errors=True)


def fetch_dom(url):
    global _proc
    browser, profile = find_browser()
    os.makedirs(profile, mode=0o700, exist_ok=True)
    rules = ", ".join(f"MAP {h} ~NOTFOUND" for h in SKIP_HOSTS)
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions", "--disable-sync",
        "--disable-background-networking", "--disable-component-update",
        "--disable-default-apps", "--mute-audio", "--blink-settings=imagesEnabled=false",
        "--disk-cache-size=33554432", f"--host-resolver-rules={rules}",
        f"--user-data-dir={profile}", "--virtual-time-budget=25000", "--dump-dom", url,
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,  # nosec B603
                         stderr=subprocess.DEVNULL, start_new_session=True)
    with _proc_lock:
        _proc = p
    try:
        out, _ = p.communicate(timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        p.communicate()
        raise CheckError("Site too slow (timed out)") from None
    finally:
        _kill_group(p)  # make sure no browser helper stays behind
        with _proc_lock:
            _proc = None
    return out.decode("utf-8", "replace")


TRIP_RE = re.compile(r"<single-trip\b[^>]*>(.*?)</single-trip>", re.S)
DEP_RE = re.compile(r'class="(?:[^"]*\s)?from(?:\s[^"]*)?"[^>]*>\s*<h5[^>]*>\s*(\d{1,2}):(\d{2})')
ARR_RE = re.compile(r'class="(?:[^"]*\s)?to(?:\s[^"]*)?"[^>]*>\s*<h5[^>]*>\s*(\d{1,2}):(\d{2})')
NUM_RE = re.compile(r"(?:قطار رقم|Train No\.?)\s*(\d+)")


def parse_trips(html):
    """Return a list of trips. [] = no trains for this day. Raises if the page did not load."""
    trips = []
    for seg in TRIP_RE.findall(html):
        dep, arr, num = DEP_RE.search(seg), ARR_RE.search(seg), NUM_RE.search(seg)
        if not dep:
            continue
        trips.append({
            "dep": f"{int(dep.group(1)):02d}:{dep.group(2)}",
            "arr": f"{int(arr.group(1)):02d}:{arr.group(2)}" if arr else "?",
            "train": num.group(1) if num else "?",
            # The site shows a "waiting list" box only on fully booked trains (trip.fullyBooked).
            "available": "<waiting-list-widget" not in seg,
        })
    if trips:
        return trips
    if "<no-trips-found" in html:
        return []
    raise CheckError("Page did not load (site busy or changed)")


def _save_failed_page(html):
    """Keep the last page that did not parse, for diagnosis (one file, overwritten)."""
    try:
        _write_private(os.path.join(CACHE_DIR, "last-error.html"),
                       f"<!-- {dt.datetime.now():%Y-%m-%d %H:%M:%S} -->\n{html}")
    except OSError:
        pass


def check(route, date):
    html = ""
    try:
        html = fetch_dom(build_url(route, date))
        trips = parse_trips(html)
        return {"ok": True, "trips": trips, "error": "", "at": dt.datetime.now()}
    except CheckError as e:
        err = str(e)
        if html:
            _save_failed_page(html)
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
    print(f"Installed: {target}")
    print("Open 'SAR Watch' from your app menu. It also starts at login.")


def uninstall(assume_yes=False):
    paths = [p for p in [*_launchers(), DATA_DIR, CONF_DIR, CACHE_DIR, SNAP_PROFILE_DIR] if os.path.exists(p)]
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
        print(f"  {t['dep']} → {t['arr']}  train {t['train']:>3}  {'SEATS' if t['available'] else 'full'}")
    return 0 if any(t["available"] for t in res["trips"]) else 1

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
                        build_url(self.cfg["route"], self.cfg["date"]))).start()
            else:
                self.errors += 1
                if self.errors == 2:   # stale browser data can break the page; start fresh
                    reset_browser_data()
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
            Gio.AppInfo.launch_default_for_uri(build_url(self.cfg["route"], self.cfg["date"]), None)
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
        stop_running_check()
        os.execv(sys.executable, args)   # the lock file closes on exec, so the new app can start

    def quit(self):
        self.watching = False
        self.cancel_timer()
        stop_running_check()
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
        print("Quit SAR Watch and open it again.")
        return 0
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
