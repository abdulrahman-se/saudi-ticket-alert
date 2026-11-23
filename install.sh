#!/bin/sh
# SAR Watch installer for Linux
#
# Run (one line):
#   curl -fsSL https://raw.githubusercontent.com/abdulrahman-se/saudi-ticket-alert/main/install.sh | sh
#
# Read it first (safer):
#   curl -fsSLO https://raw.githubusercontent.com/abdulrahman-se/saudi-ticket-alert/main/install.sh
#   less install.sh
#   sh install.sh --check      check only, change nothing
#   sh install.sh              check, ask, install
#
# Server with no desktop (SSH): add --server. It installs no GTK parts.
#   curl -fsSL https://raw.githubusercontent.com/abdulrahman-se/saudi-ticket-alert/main/install.sh | sh -s -- --server
#
# What it does
#   1. It finds your Linux and its package manager (dnf, apt or pacman).
#   2. It checks Python 3.11+, the GTK parts for Python, and the top-bar icon
#      extension on GNOME. (A server needs only Python. No browser is needed.)
#   3. It shows the commands for the missing parts and asks you before it uses sudo.
#      It uses only the package repositories of your Linux. It adds no repositories and no keys.
#   4. It gets sar_watch.py (from this folder, or from GitHub over HTTPS), checks the file,
#      and installs it for your user only (~/.local/share/sar-watch).
#
# Exit codes: 0 = done or ready, 1 = error, 2 = you must do a step first (the message tells you).
#
# All the code is in functions and the last line starts it.
# If the download stops in the middle, nothing runs.

set -eu
set -f   # no file-name patterns: package lists split on spaces only

APP_URL="https://raw.githubusercontent.com/abdulrahman-se/saudi-ticket-alert/main/sar_watch.py"
EXT_UUID="appindicatorsupport@rgcjonas.gmail.com"
UBUNTU_EXT_UUID="ubuntu-appindicators@ubuntu.com"
EXT_NAME="AppIndicator and KStatusNotifierItem Support"

PY_VERSION='import sys; print("%d.%d" % sys.version_info[:2]); sys.exit(sys.version_info < (3, 11))'

# Prints the missing GTK parts, or nothing when all are there.
PY_GTK='
import importlib, sys
try:
    import gi
except ImportError:
    print("PyGObject")
    sys.exit()
def have(name, version):
    try:
        gi.require_version(name, version)
        importlib.import_module("gi.repository." + name)
        return True
    except (ValueError, ImportError):
        return False
missing = [label for name, version, label in (("Gtk", "3.0", "GTK 3"), ("Notify", "0.7", "libnotify"))
           if not have(name, version)]
if not (have("AyatanaAppIndicator3", "0.1") or have("AppIndicator3", "0.1")):
    missing.append("AppIndicator")
print(", ".join(missing))
'

# Gets sar_watch.py (HTTPS URL or local file), checks it, saves it (mode 600), prints its version.
PY_FETCH='
import os, re, sys, time, urllib.request
src, out = sys.argv[1], sys.argv[2]
limit = 1000000
def download(url):
    for attempt in range(3):                    # 3 tries: a short network problem does not stop the install
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sar-watch-installer"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if not r.geturl().startswith("https://"):
                    raise ValueError("the download left HTTPS")
                return r.read(limit + 1)
        except OSError:                         # no connection, timeout, HTTP error
            if attempt == 2:
                raise
            time.sleep(3)
try:
    if src.startswith("https://"):
        data = download(src)
    else:
        with open(src, "rb") as f:
            data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError("the file is too large")
    text = data.decode("utf-8")
    compile(text, "sar_watch.py", "exec")
    if "\nAPP_ID = \"sar-watch\"\n" not in text:
        raise ValueError("the file is not SAR Watch")
    version = re.search(r"^__version__ = \"(\d+\.\d+\.\d+)\"$", text, re.M)
    if not version:
        raise ValueError("the file has no version")
except Exception as e:
    sys.exit("Error: could not get SAR Watch: %s" % e)
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as f:
    f.write(data)
print(version.group(1))
'

# Turns on a GNOME extension for the next login (a new extension needs a new login on Wayland).
PY_EXT_ON='
import sys
from gi.repository import Gio
uuid = sys.argv[1]
s = Gio.Settings.new("org.gnome.shell")
on = s.get_strv("enabled-extensions")
if uuid not in on:
    s.set_strv("enabled-extensions", on + [uuid])
off = s.get_strv("disabled-extensions")
if uuid in off:
    s.set_strv("disabled-extensions", [u for u in off if u != uuid])
Gio.Settings.sync()
'

# ------------------------------------------------------------------ helpers

colors() {
  if [ -t 1 ] && [ -z "${NO_COLOR-}" ] && [ "${TERM-dumb}" != dumb ]; then
    B=$(printf '\033[1m') G=$(printf '\033[32m') Y=$(printf '\033[33m')
    R=$(printf '\033[31m') D=$(printf '\033[2m') N=$(printf '\033[0m')
  else
    B='' G='' Y='' R='' D='' N=''
  fi
}

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s[OK]%s    %s\n' "$G" "$N" "$*"; }
need() { printf '  %s[NEED]%s  %s\n' "$Y" "$N" "$*"; }
skip() { printf '  %s[--]%s    %s\n' "$D" "$N" "$*"; }
warn() { printf '%sWarning:%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%sError:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'EOF'
SAR Watch installer for Linux

  sh install.sh           check your system, ask, then install
  sh install.sh --check   only check and show the commands (change nothing)
  sh install.sh --server  for a server with no desktop (SSH): no GTK parts
  sh install.sh --yes     do not ask (for scripts and tests)

It asks before it uses sudo. It installs SAR Watch for your user only.
EOF
}

os_field() {
  sed -n "s/^$1=//p" /etc/os-release | head -n 1 | tr -d "\"'"
}

# Shows a command (mode "show"), or shows and runs it (mode "run").
step() {
  mode=$1
  shift
  if [ "$mode" = show ]; then
    printf '    %s\n' "$*"
    return 0
  fi
  printf '%s+ %s%s\n' "$D" "$*" "$N"
  if [ "$HAS_TTY" = 1 ]; then "$@" </dev/tty; else "$@" </dev/null; fi
}

pm_install() {
  mode=$1
  shift
  case $PM in
    dnf)        step "$mode" sudo dnf install -y "$@" ;;
    apt)        step "$mode" sudo apt-get install -y "$@" ;;
    pacman)     step "$mode" sudo pacman -S --needed --noconfirm "$@" ;;
    rpm-ostree) step "$mode" sudo rpm-ostree install --idempotent "$@" ;;
  esac
}

# ------------------------------------------------------------------ checks

detect_os() {
  OS_ID='' OS_LIKE='' OS_NAME=Linux PM=''
  if [ -r /etc/os-release ]; then
    OS_ID=$(os_field ID)
    OS_LIKE=$(os_field ID_LIKE)
    OS_NAME=$(os_field PRETTY_NAME)
  fi
  for x in $OS_ID $OS_LIKE; do
    case $x in
      fedora)        PM=dnf ;;
      debian|ubuntu) PM=apt ;;
      arch)          PM=pacman ;;
    esac
    [ -z "$PM" ] || break
  done
  if [ "$PM" = dnf ] && [ -e /run/ostree-booted ] && have rpm-ostree; then PM=rpm-ostree; fi
  case $PM in
    dnf|rpm-ostree) PY_PKG=python3
                    GTK_PKGS="python3-gobject gobject-introspection gtk3 libayatana-appindicator-gtk3 libnotify" ;;
    apt)            PY_PKG=python3
                    GTK_PKGS="python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7" ;;
    pacman)         PY_PKG=python
                    GTK_PKGS="python-gobject gtk3 libayatana-appindicator libnotify" ;;
    *)              PY_PKG='' GTK_PKGS='' ;;
  esac
  case $PM in apt) have apt-get || PM='' ;; '') ;; *) have "$PM" || PM='' ;; esac
}

check_all() {
  PY_VER='' PY_OK=0 GTK_MISSING='' EXT_STATE=na
  if have python3; then
    if PY_VER=$(python3 -I -c "$PY_VERSION" 2>/dev/null); then PY_OK=1; fi
  fi
  if [ "$SERVER" = 1 ]; then
    GTK_MISSING=''
  elif [ "$PY_OK" = 1 ]; then
    GTK_MISSING=$(python3 -I -c "$PY_GTK" 2>/dev/null) || GTK_MISSING="GTK parts"
  else
    GTK_MISSING="needs Python first"
  fi
  [ "$SERVER" != 1 ] || return 0
  case ":${XDG_CURRENT_DESKTOP-}:" in
    *GNOME*)
      EXT_STATE=missing
      if have gnome-extensions; then
        enabled=$(gnome-extensions list --enabled 2>/dev/null) || enabled=''
        all=$(gnome-extensions list 2>/dev/null) || all=''
        case $enabled in *"$EXT_UUID"*|*"$UBUNTU_EXT_UUID"*) EXT_STATE=on ;; esac
        if [ "$EXT_STATE" = missing ]; then
          case $all in *"$EXT_UUID"*|*"$UBUNTU_EXT_UUID"*) EXT_STATE=off ;; esac
        fi
      fi
      if [ "$EXT_STATE" = missing ]; then
        for d in /usr/share/gnome-shell/extensions "$HOME/.local/share/gnome-shell/extensions"; do
          if [ -d "$d/$EXT_UUID" ]; then EXT_STATE=off; fi
        done
      fi ;;
  esac
}

show_checks() {
  if [ "$PY_OK" = 1 ]; then ok "Python $PY_VER"
  elif [ -n "$PY_VER" ]; then need "Python 3.11 or newer (this system has $PY_VER)"
  else need "Python 3.11 or newer"; fi
  if [ "$SERVER" = 1 ]; then skip "GTK parts and top-bar icon: not needed on a server"
  elif [ -z "$GTK_MISSING" ]; then ok "GTK 3, AppIndicator and libnotify for Python"
  else need "GTK 3, AppIndicator and libnotify for Python ($GTK_MISSING)"; fi
  case $EXT_STATE in
    on)      ok "Top-bar icon: GNOME extension is on" ;;
    off)     need "Top-bar icon: turn on the GNOME extension \"$EXT_NAME\"" ;;
    missing) need "Top-bar icon: GNOME extension \"$EXT_NAME\"" ;;
    na)      [ "$SERVER" = 1 ] || skip "Top-bar icon: not GNOME, no check" ;;
  esac
  say ""
}

# Sets PKGS (sudo packages) and EXT_ENABLE.
plan() {
  PKGS='' EXT_ENABLE=0 CANNOT=''
  if [ "$PY_OK" != 1 ]; then PKGS=$PY_PKG; fi
  if [ -n "$GTK_MISSING" ]; then PKGS="$PKGS $GTK_PKGS"; fi
  if [ -z "$PM" ] && [ -n "$PKGS" ]; then
    CANNOT="Python 3.11+"
    if [ "$SERVER" != 1 ]; then CANNOT="$CANNOT, PyGObject, GTK 3, Ayatana AppIndicator 3, libnotify"; fi
  fi
  if [ "$EXT_STATE" = missing ] && [ -n "$PM" ]; then PKGS="$PKGS gnome-shell-extension-appindicator"; fi
  if [ "$EXT_STATE" = missing ] || [ "$EXT_STATE" = off ]; then EXT_ENABLE=1; fi
  PKGS=${PKGS# }
}

# Shows or runs the sudo commands.
commands() {
  mode=$1
  if [ "$PM" = apt ] && [ -n "$PKGS" ]; then
    step "$mode" sudo apt-get update || warn "apt-get update had errors. The script goes on."
  fi
  if [ -n "$PKGS" ]; then
    # shellcheck disable=SC2086 # split the list into package names
    pm_install "$mode" $PKGS || return 1
  fi
  return 0
}

# ------------------------------------------------------------------ install

enable_extension() {
  if have gnome-extensions && gnome-extensions enable "$EXT_UUID" 2>/dev/null; then
    say "Turned on the GNOME extension."
  elif python3 -I -c "$PY_EXT_ON" "$EXT_UUID" 2>/dev/null; then
    RELOGIN=1
  else
    warn "Could not turn on the GNOME extension. Open the Extensions app and turn on \"$EXT_NAME\"."
  fi
}

install_app() {
  TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sar-watch-install.XXXXXX") || die "Could not make a temporary folder."
  trap 'rm -rf -- "$TMP_DIR"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  src=$APP_URL dir=''
  case $0 in
    *install.sh)
      dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || dir=''
      if [ -n "$dir" ] && [ -f "$dir/sar_watch.py" ]; then src="$dir/sar_watch.py"; fi ;;
  esac
  if [ "$src" = "$APP_URL" ]; then say "Downloading SAR Watch from GitHub (HTTPS) ..."
  else say "Using sar_watch.py from $dir"; fi
  version=$(python3 -I -c "$PY_FETCH" "$src" "$TMP_DIR/sar_watch.py") || exit 1
  say "Installing SAR Watch $version for your user ..."
  out=$(python3 -I "$TMP_DIR/sar_watch.py" --install 2>&1) || { say "$out" >&2; die "The install step failed."; }
}

service_active() {
  have systemctl && systemctl --user is-active --quiet sar-watch.service 2>/dev/null
}

restart_service() {
  if systemctl --user restart sar-watch.service 2>/dev/null; then
    say "The background service uses the new version now."
  fi
}

finish_server() {
  cmd="$HOME/.local/bin/sar-watch"
  if have sar-watch; then cmd=sar-watch; fi
  say "${G}Done.${N} SAR Watch is installed for a server (no desktop)."
  if service_active; then
    restart_service
    say "Change the trip: $cmd --setup    Status: $cmd --status"
  elif [ "$HAS_TTY" = 1 ] && [ "$ASSUME_YES" != 1 ]; then
    say "Now choose your trip and the phone alerts."
    say ""
    "$cmd" --setup </dev/tty || say "Run it again later: $cmd --setup"
  else
    say "Next: $cmd --setup"
  fi
}

main() {
  colors
  CHECK_ONLY=0 ASSUME_YES=0 RELOGIN=0 SERVER=0
  SERVER_ARG=''
  for arg in "$@"; do
    case $arg in
      --check)    CHECK_ONLY=1 ;;
      --yes|-y)   ASSUME_YES=1 ;;
      --server)   SERVER=1 SERVER_ARG=' --server' ;;
      --help|-h)  usage; return 0 ;;
      *)          die "Unknown option: $arg. See: sh install.sh --help" ;;
    esac
  done
  [ "$(uname -s)" = Linux ] || die "This installer is for Linux. macOS and Windows: coming soon."
  [ "$(id -u)" != 0 ] || die "Do not run this as root or with sudo. Run it as your normal user. It asks for sudo only to install packages."
  HAS_TTY=0
  if (: </dev/tty) 2>/dev/null; then HAS_TTY=1; fi

  detect_os
  say "${B}SAR Watch installer${N}"
  say "System: $OS_NAME${PM:+, $PM}"
  if [ "$SERVER" = 1 ]; then say "Mode: server (no desktop)"
  elif [ -z "${DISPLAY-}${WAYLAND_DISPLAY-}" ] && [ -n "${SSH_CONNECTION-}" ]; then
    say "Tip: no desktop found. For a server, use: sh install.sh --server"
  fi
  say ""
  check_all
  show_checks
  if [ -n "$PY_VER" ] && [ "$PY_OK" != 1 ]; then die "SAR Watch needs Python 3.11 or newer. This system has Python $PY_VER."; fi
  plan

  if [ -n "$CANNOT" ]; then
    say "This installer does not know your Linux. Install these parts with your package manager:"
    say "  $CANNOT"
    say "Then run this script again."
    exit 2
  fi

  if [ -n "$PKGS" ]; then
    say "${B}To install the missing parts, it runs:${N}"
    commands show
    if [ "$EXT_ENABLE" = 1 ]; then say "Then it turns on the GNOME extension (no sudo)."; fi
    say ""
    if [ "$CHECK_ONLY" = 1 ]; then say "Check only. Nothing changed."; exit 2; fi
    if [ "$PM" = rpm-ostree ]; then
      say "Run these commands in your terminal. Restart the computer. Then run this script again."
      exit 2
    fi
    have sudo || { say "No sudo. Run these commands as root (without sudo). Then run this script again."; exit 2; }
    if [ "$ASSUME_YES" != 1 ]; then
      if [ "$HAS_TTY" != 1 ]; then
        say "Run these commands in your terminal. Then run this script again."
        exit 2
      fi
      printf 'Run them now? [y/N] '
      answer=''
      read -r answer </dev/tty || answer=''
      case $answer in
        y|Y|yes|Yes|YES) ;;
        *) say "Stopped. Nothing changed."; exit 1 ;;
      esac
    fi
    commands run || die "Could not install the packages. Read the messages above."
    say ""
    check_all
    if [ "$PY_OK" != 1 ] || [ -n "$GTK_MISSING" ]; then
      die "Some parts are still missing: Python 3.11+ or ${GTK_MISSING:-GTK parts}. Read the messages above."
    fi
  else
    if [ "$CHECK_ONLY" = 1 ]; then
      if [ "$EXT_ENABLE" = 1 ]; then say "Ready. It will turn on the GNOME extension (no sudo)."; exit 2; fi
      say "Ready. Nothing is missing. Run: sh install.sh${SERVER_ARG}"
      exit 0
    fi
  fi

  if [ "$EXT_ENABLE" = 1 ]; then enable_extension; fi
  install_app

  say ""
  if [ "$SERVER" = 1 ]; then
    finish_server
    return 0
  fi
  say "${G}Done.${N} Open \"SAR Watch\" from your app menu. It also starts when you log in."
  if [ "$RELOGIN" = 1 ]; then say "${B}Log out and log in one time.${N} Then the train icon shows in the top bar."; fi
  if service_active; then
    restart_service
  elif have pgrep && pgrep -u "$(id -u)" -f "sar-watch/sar_watch.py" >/dev/null 2>&1; then
    say "SAR Watch is open now. To use the new version: click Quit in its menu, then open it again."
  fi
}

main "$@"
