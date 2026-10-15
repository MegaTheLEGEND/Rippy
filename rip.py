#!/usr/bin/env python3
"""Rippy: auto DVD ripper with multi-drive support.

One main process watches every optical drive. When a disc shows up in a drive
that isn't already being ripped, it asks Movie/TV, then launches a separate
HandBrakeCLI subprocess for that drive and goes straight back to watching.
When a rip subprocess exits for any reason (finished, failed, or killed), the
main process ejects that drive.

TV shows (beta): the disc's titles are scanned, the show is looked up on TMDB, the episode-length
titles are picked and numbered, and each episode is ripped in turn into _Needs Review/<show name>/
(named "<show> - S01E03"). The only questions are the season and the disc number, and only when the
disc label doesn't say (a best guess is pre-filled; no answer in time = the guess).

DVDs are ripped to MP4 with HandBrakeCLI. Blu-rays are dumped to MKV with
MakeMKV's makemkvcon (HandBrake can't decrypt most Blu-rays).

Finished rips can be uploaded to a NAS over SMB (no drive mounting needed): put a
nas.json next to this script/exe (or embed it with the build .bat; see nas.json.example). Rips are made in a local
staging folder first; each finished file is uploaded, verified, then deleted locally.
If the NAS is unreachable the file stays local and is retried on the next start.
Requires:  pip install smbprotocol

Usage:
    python rip.py            run the ripper (window; minimizes to the taskbar)
    python rip.py --nogui    run it in a plain console instead
    python rip.py --list     show detected drives/discs and exit (for testing)
    python rip.py --nas-test check the NAS login and write access, then exit
"""
import codecs, json, os, platform, queue, re, shutil, statistics, subprocess, sys, threading, time, traceback
import urllib.parse, urllib.request
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except Exception:                       # no tkinter installed -> console mode only
    tk = ttk = messagebox = None

OS = platform.system()
APP_TITLE = "Rippy"
NOWIN = {"creationflags": 0x08000000} if OS == "Windows" else {}   # CREATE_NO_WINDOW: no console flashes from child tools
HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", HERE))  # where PyInstaller unpacks embedded files

# ---------- CONFIG (the only things you might change) ----------
OUTPUT_DIR = Path.home() / "Rips" / "Movies"   # local staging; moved to the NAS if nas.json exists
LOG_DIR = OUTPUT_DIR.parent / "logs"        # one HandBrake log per rip
FALLBACK_PRESET = "Fast 480p30"             # used if no preset file is found
POLL_SECONDS = 4
STATUS_SECONDS = 30                         # how often to print rip progress
SHOW_LOG_WINDOW = True                      # Windows: open a live log window per rip
PROMPT_TIMEOUT = 30                         # seconds of no typing before prompts auto-answer
BD_MIN_LENGTH = 2400                        # Blu-ray: only dump titles at least this many seconds long
TMDB_API_KEY = "52be8347dcbbcd765b223995d9c97cad"                           # free key from themoviedb.org (Settings > API); blank = no lookup
REVIEW_DIR_NAME = "_Needs Review"          # unsure matches are ripped here, without a year
TV_MIN_EPISODE = 900                        # TV (beta): shortest title (seconds) that can be an episode; shorter ones are extras
TV_DEFAULT_SEASON = 1                       # TV (beta): season to assume when the disc label doesn't say (None = name files by disc label instead)
TV_STALL_SECONDS = 90                       # TV (beta): HandBrake is stopped if the drive delivers (almost) nothing for this long
TV_STALL_FPS = 3.0                          #   "almost nothing" = under this many frames/second (a healthy DVD encode does 50+)
TV_RETRY_WAIT = 15                          # TV (beta): seconds to let the drive settle before retrying an episode that failed
NOTIFY = True                               # pop an OS notification when a rip needs your attention (a question, a failure, a stuck disc)
REINSERT_WINDOW = 120                      # a disc with the same label back in a drive within this many seconds of an eject = "same disc re-inserted"
SCAN_PREVIEWS = 5                           # preview images HandBrake decodes per title while scanning (default 10; fewer = faster)
SCAN_TIMEOUT = 900                          # seconds to allow for the one-time disc scan
UPLOAD_STALL_SECONDS = 60                   # NAS upload: no data accepted for this long = stalled; reconnect and retry
UPLOAD_ATTEMPTS = 3                         # tries per file before giving up until the next rip/restart
NAS_CONFIG_FILE = HERE / "nas.json"         # NAS login: next to the exe/script, else the copy embedded in the exe
# ----------------------------------------------------------------

# Preset: prefer a file next to the exe/script, else the one embedded in the exe.
PRESET_FILE = HERE / "movie_profile.json"
if not PRESET_FILE.exists():
    PRESET_FILE = BUNDLE / "movie_profile.json"

# TV preset (optional): tv_profile.json next to the exe/script or embedded; else the movie preset is used.
TV_PRESET_FILE = HERE / "tv_profile.json"
if not TV_PRESET_FILE.exists():
    TV_PRESET_FILE = BUNDLE / "tv_profile.json"
if not TV_PRESET_FILE.exists():
    TV_PRESET_FILE = PRESET_FILE

# NAS config: same rule. A nas.json next to the exe wins; otherwise use the embedded one.
if not NAS_CONFIG_FILE.exists():
    NAS_CONFIG_FILE = BUNDLE / "nas.json"


# ===== Settings (the Settings window saves here; nas.json and the profile files are only ever read) =====
def _settings_dir():
    if OS == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif OS == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Rippy"


SETTINGS_DIR = _settings_dir()
SETTINGS_FILE = SETTINGS_DIR / "settings.json"


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)                      # it can hold the NAS password
    except OSError:
        pass
    os.replace(tmp, SETTINGS_FILE)


def preset_file_for(kind="movie"):
    """The HandBrake preset file to use for 'movie' or 'tv'. A profile saved in Settings wins (written to
    Rippy's own folder); otherwise the movie_profile.json / tv_profile.json next to the app, as before."""
    prof = load_settings().get("profiles") or {}

    def saved(k):
        text = prof.get(k)
        if not (isinstance(text, str) and text.strip()):
            return None
        p = SETTINGS_DIR / "profiles" / f"{k}.json"
        try:
            if not p.exists() or p.read_text(encoding="utf-8") != text:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            return p
        except OSError:
            return None

    movie = saved("movie") or PRESET_FILE
    if kind != "tv":
        return movie
    if prof.get("tv_use_movie") is True:
        return movie
    return saved("tv") or (TV_PRESET_FILE if TV_PRESET_FILE != PRESET_FILE else movie)


def parse_profile(text):
    """Check that `text` is an exported HandBrake preset; returns its preset dict or raises ValueError."""
    try:
        p = json.loads(text)["PresetList"][0]
        if p.get("Folder"):
            p = p["ChildrenArray"][0]
        p["PresetName"]
        return p
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ValueError("That isn't a HandBrake preset export (it needs PresetList → PresetName). "
                         "In HandBrake: Presets → Export Preset.") from None


def profile_summary(p):
    bits = [p.get("PresetName"), p.get("VideoEncoder")]
    if p.get("VideoQualityType") == 2 and isinstance(p.get("VideoQualitySlider"), (int, float)):
        bits.append(f"RF {p['VideoQualitySlider']:g}")
    elif p.get("VideoAvgBitrate"):
        bits.append(f"{p['VideoAvgBitrate']} kbps")
    ff = str(p.get("FileFormat") or "")
    bits.append("MP4" if "mp4" in ff else "MKV" if "mkv" in ff else ff)
    return "  ·  ".join(str(b) for b in bits if b)


# ===== HandBrake / preset discovery =====
def find_handbrake():
    exe = "HandBrakeCLI.exe" if OS == "Windows" else "HandBrakeCLI"
    for base in (BUNDLE, HERE):
        if (base / exe).exists():
            return str(base / exe)
    found = shutil.which("HandBrakeCLI")
    if found:
        return found
    for c in (r"C:\Program Files\HandBrake\HandBrakeCLI.exe",
              "/opt/homebrew/bin/HandBrakeCLI", "/usr/local/bin/HandBrakeCLI"):
        if os.path.exists(c):
            return c
    return None


def find_makemkv():
    names = ("makemkvcon64.exe", "makemkvcon.exe") if OS == "Windows" else ("makemkvcon",)
    for base in (BUNDLE, HERE):
        for sub in ("MakeMKV", ""):          # a copy of the MakeMKV folder, or loose files
            for n in names:
                if (base / sub / n).exists():
                    return str(base / sub / n)
    for n in ("makemkvcon64", "makemkvcon"):
        found = shutil.which(n)
        if found:
            return found
    for c in (r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
              r"C:\Program Files\MakeMKV\makemkvcon64.exe",
              r"C:\Program Files (x86)\MakeMKV\makemkvcon.exe",
              r"C:\Program Files\MakeMKV\makemkvcon.exe",
              "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
              "/usr/bin/makemkvcon", "/usr/local/bin/makemkvcon"):
        if os.path.exists(c):
            return c
    return None


def makemkv_source(drive):
    """How to tell makemkvcon which drive to read."""
    if OS == "Windows":
        return f"dev:{drive[:2]}"
    if OS == "Linux":
        return f"dev:{drive}"
    return "disc:0"          # macOS: first disc (volume paths aren't device names)


def preset_args(preset_file=None):
    preset_file = preset_file or preset_file_for("movie")
    if preset_file.exists():
        try:
            data = json.loads(preset_file.read_text(encoding="utf-8"))
            p = data["PresetList"][0]
            if p.get("Folder"):
                p = p["ChildrenArray"][0]
            return ["--preset-import-file", str(preset_file), "-Z", p["PresetName"]]
        except Exception as e:
            print(f"Couldn't read {preset_file.name} ({e}); using fallback preset.")
    else:
        print(f"No {preset_file.name} found; using '{FALLBACK_PRESET}'.")
    return ["-Z", FALLBACK_PRESET]


# ===== Drive detection (per OS) =====
# list_drives() -> ids; disc_info(id) -> (source_path, label, kind) or None; eject(id)
# kind is "dvd" or "bd"
if OS == "Windows":
    import ctypes, string
    ctypes.windll.kernel32.SetErrorMode(1)  # no "no disc in drive" popups

    def list_drives():
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [f"{l}:\\" for i, l in enumerate(string.ascii_uppercase)
                if mask >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(f"{l}:\\") == 5]

    def disc_info(d):
        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(d, buf, 261, None, None, None, None, 0)
        kind = ("bd" if os.path.isdir(d + "BDMV")
                else "dvd" if os.path.isdir(d + "VIDEO_TS") else None)
        if ok and kind:
            return d, buf.value or "Unknown Disc", kind
        return None

    def eject(d):
        mci = ctypes.windll.winmm.mciSendStringW
        mci(f"open {d[:2]} type cdaudio alias ripdrive", None, 0, None)
        mci("set ripdrive door open", None, 0, None)
        mci("close ripdrive", None, 0, None)

    def close_tray(d):
        mci = ctypes.windll.winmm.mciSendStringW
        mci(f"open {d[:2]} type cdaudio alias ripdrive", None, 0, None)
        mci("set ripdrive door closed", None, 0, None)
        mci("close ripdrive", None, 0, None)

    # --- talking to the drive directly (used when the normal eject doesn't open the tray) ---
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
                                 ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    _k32.DeviceIoControl.restype = ctypes.c_int
    _k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                     ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    def _open_drive(d, access):
        h = _k32.CreateFileW("\\\\.\\" + d[:2], access, 3, None, 3, 0, None)   # share read+write, OPEN_EXISTING
        return None if h in (None, 0, _INVALID_HANDLE) else h

    def _ioctl(h, code, buf=None):
        returned = ctypes.c_ulong(0)
        return bool(_k32.DeviceIoControl(h, code, ctypes.byref(buf) if buf is not None else None,
                                         ctypes.sizeof(buf) if buf is not None else 0,
                                         None, 0, ctypes.byref(returned), None))

    def media_state(d):
        """Is there a disc in the drive? 'present' / 'none' (tray empty or open) / 'unsure'.
        Asks the drive itself (so it still works when the disc can't be READ, unlike disc_info)."""
        h = _open_drive(d, 0x80000000)                                  # GENERIC_READ
        if h is None:
            return "unsure"
        try:
            if _ioctl(h, 0x2D4800):                                     # IOCTL_STORAGE_CHECK_VERIFY
                return "present"
            return "none" if ctypes.get_last_error() == 1112 else "unsure"   # 1112 = no media in drive
        finally:
            _k32.CloseHandle(h)

    def load_media(d):
        """Ask the drive itself to close its tray / load the disc (for drives that ignore the usual command)."""
        h = _open_drive(d, 0x80000000)
        if h is None:
            return False
        try:
            return _ioctl(h, 0x2D480C)                                  # IOCTL_STORAGE_LOAD_MEDIA
        finally:
            _k32.CloseHandle(h)

    def eject_hard(d):
        """Unlock the tray and eject by talking to the drive. True if the drive accepted the eject."""
        h = _open_drive(d, 0xC0000000) or _open_drive(d, 0x80000000)    # read+write, else read
        if h is None:
            return False
        try:
            _ioctl(h, 0x90018)                                          # FSCTL_LOCK_VOLUME (fails if still in use; carry on)
            _ioctl(h, 0x90020)                                          # FSCTL_DISMOUNT_VOLUME
            _ioctl(h, 0x2D4804, ctypes.c_ubyte(0))                      # IOCTL_STORAGE_MEDIA_REMOVAL: allow removal
            return _ioctl(h, 0x2D4808)                                  # IOCTL_STORAGE_EJECT_MEDIA
        finally:
            _k32.CloseHandle(h)

elif OS == "Darwin":
    # On macOS a DVD only shows up as a mounted volume, so each disc is its own "drive".
    def list_drives():
        return [str(p.parent) for pat in ("VIDEO_TS", "BDMV")
                for p in Path("/Volumes").glob(f"*/{pat}")]

    def disc_info(d):
        if not os.path.isdir(d):
            return None
        return d, Path(d).name, ("bd" if os.path.isdir(os.path.join(d, "BDMV")) else "dvd")

    def eject(d):
        subprocess.run(["diskutil", "eject", d])

else:  # Linux
    def list_drives():
        return sorted(str(p) for p in Path("/dev").glob("sr[0-9]*"))

    def _lsblk(field, d):
        r = subprocess.run(["lsblk", "-dno", field, d], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    def _is_bd(d):
        try:
            r = subprocess.run(["udevadm", "info", "-q", "property", "-n", d],
                               capture_output=True, text=True)
            return "ID_CDROM_MEDIA_BD=1" in r.stdout
        except OSError:
            return False

    def disc_info(d):
        if not _lsblk("FSTYPE", d):
            return None  # empty tray / no media
        return d, _lsblk("LABEL", d) or "Unknown Disc", ("bd" if _is_bd(d) else "dvd")

    def eject(d):
        subprocess.run(["eject", d])


def _disc_gone(d, wait=6):
    """True once the drive reports no disc (waits a few seconds for the tray to open)."""
    end = time.time() + wait
    while time.time() < end:
        try:
            if disc_info(d) is None:
                return True
        except Exception:
            return True
        time.sleep(0.5)
    return False


def _tray_empty_win(d, wait=4):
    """Windows: wait for the drive itself to say there is no disc."""
    end = time.time() + wait
    while time.time() < end:
        if media_state(d) == "none":
            return True
        time.sleep(0.5)
    return False


_notify_last = {}


def notify(title, message):
    """Show an operating-system notification (Windows toast, macOS banner, Linux notify-send). Never blocks
    and never raises; the same message isn't repeated within 10 seconds. Text goes in via environment
    variables, so disc labels can't break the command."""
    if not NOTIFY:
        return
    message = " ".join(str(message).split())[:220]
    now = time.time()
    if now - _notify_last.get(message, 0) < 10:
        return
    _notify_last[message] = now

    def run():
        env = dict(os.environ, RIP_NOTE_TITLE=str(title), RIP_NOTE_MSG=message)
        try:
            if OS == "Windows":
                ps = ("$ErrorActionPreference='Stop';"
                      "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
                      "[void][Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime];"
                      "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
                      "$t=$x.GetElementsByTagName('text');"
                      "[void]$t.Item(0).AppendChild($x.CreateTextNode($env:RIP_NOTE_TITLE));"
                      "[void]$t.Item(1).AppendChild($x.CreateTextNode($env:RIP_NOTE_MSG));"
                      "$app='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
                      "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app)"
                      ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                               env=env, creationflags=0x08000000, timeout=30,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif OS == "Darwin":
                subprocess.run(["osascript", "-e", 'display notification (system attribute "RIP_NOTE_MSG") '
                                'with title (system attribute "RIP_NOTE_TITLE")'],
                               env=env, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif shutil.which("notify-send"):
                subprocess.run(["notify-send", str(title), message], timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


disc_labels = {}     # drive id -> label of the disc last seen there
eject_log = {}       # drive id -> (label of the disc that came out, time it came out); used to spot a disc that goes back in


def safe_eject(d):
    """Eject and make sure the disc really came out (and remember which disc it was).

    A disc that can't be read (e.g. after a read error) makes the drive look empty to the normal
    disc check even though it is still inside, so on Windows the drive is asked directly. If the
    normal eject doesn't open the tray it is retried by talking to the drive (unlocking the
    tray), then with the Explorer 'Eject' verb, before telling you to do it by hand.
    """
    label = disc_labels.get(d)
    ok = _safe_eject(d)
    if ok:
        eject_log[d] = (label, time.time())
    else:
        notify(APP_TITLE, f"Drive {d} couldn't eject the disc. Eject it by hand (and close any leftover HandBrakeCLI).")
    return ok


def _safe_eject(d):
    try:
        eject(d)
        if OS == "Windows":
            if _tray_empty_win(d):
                return True
            print(f"[{d}] the tray didn't open; asking the drive directly...")
            if eject_hard(d) and _tray_empty_win(d, 6):
                return True
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(New-Object -ComObject Shell.Application).Namespace(17)"
                            f".ParseName('{d[:2]}').InvokeVerb('Eject')"],
                           timeout=30, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, **NOWIN)
            if _tray_empty_win(d, 6) or media_state(d) != "present":
                return True
        else:
            if _disc_gone(d):
                return True
            print(f"[{d}] the disc is still in after ejecting; trying again...")
            eject(d)
            if _disc_gone(d):
                return True
        print(f"[{d}] couldn't eject. Another program may be using the drive (close any leftover "
              "HandBrakeCLI.exe in Task Manager); eject it by hand and put it back to try again.")
        return False
    except Exception as e:
        print(f"Could not eject {d}: {e}")
        return False


def reseat(d, stop=None, log=None):
    """Eject the disc and close the tray again. A drive that stalled mid-rip and refuses to open
    the disc ('Can't open F:\\ for reading') normally works again after this, as if you had taken
    the disc out and put it back. Windows (tray drives) and Linux only. True if the disc can be
    read again; False if that isn't possible (or you hit Cancel). Every step goes to `log`."""
    say = log or (lambda msg: None)
    try:
        if OS == "Windows":
            close, extra = close_tray, load_media
        elif OS == "Linux":
            close = lambda x: subprocess.run(["eject", "-t", x], timeout=30, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            extra = None
        else:
            say("re-seat: not supported on this operating system")
            return False

        def cancelled(seconds):                               # sleep; True if Cancel was pressed meanwhile
            if stop:
                return stop.wait(seconds)
            time.sleep(seconds)
            return False

        say("re-seat: ejecting the disc")
        if not safe_eject(d):
            say("re-seat: the drive wouldn't eject, so it can't be re-seated")
            return False
        if cancelled(3):
            return False
        say("re-seat: closing the tray")
        close(d)
        t0, tried_load = time.time(), False
        while time.time() - t0 < 75:
            if cancelled(2):
                return False
            if disc_info(d):
                say(f"re-seat: disc readable again after {round(time.time() - t0)}s")
                return not cancelled(3)                       # let it spin up, then report ready
            if extra and not tried_load and time.time() - t0 > 10:
                tried_load = True
                say("re-seat: still not readable after 10s; sending the drive its own 'load disc' command")
                extra(d)
        state = media_state(d) if OS == "Windows" else "n/a"
        say(f"re-seat: gave up after 75s; the drive says media is: {state} "
            "('none' = tray still open/empty, 'present' = disc in but unreadable)")
        return False
    except Exception as e:
        say(f"re-seat: failed with {e!r}")
        return False


# ===== Naming =====
def sanitize(name):
    """Make a string safe as a file name on Windows, Mac and Linux."""
    name = name.replace(":", " -").replace('"', "'")
    name = re.sub(r"[/\\|]", "-", name)
    name = re.sub(r"[<>?*\x00-\x1f]", "", name)
    return re.sub(r"\s+", " ", name).strip(" .")


JUNK = re.compile(r"\b(disc|disk|d)\s*\d+\b|\b(dvd|bluray|blu ray|bd|widescreen|fullscreen|ws|fs|"
                  r"special edition|collectors edition|dvd video|video ts)\b", re.I)


def default_title(label):
    """Disc label -> a readable title guess ('WB_ARSENIC_D1' -> 'Wb Arsenic')."""
    text = re.sub(r"[_\.]+", " ", label)
    text = re.sub(r"\s+", " ", JUNK.sub(" ", text)).strip()
    return sanitize(text.title()) or "Unknown Movie"


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _tmdb_get(path, **params):
    params["api_key"] = TMDB_API_KEY
    url = f"https://api.themoviedb.org/3/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.load(r)


def tmdb_search(query):
    """Return [{id,title,year,runtime}, ...] from TMDB (runtime in minutes or None),
    or None if there's no key / we're offline."""
    if not TMDB_API_KEY:
        return None
    try:
        data = _tmdb_get("search/movie", query=query)
    except Exception as e:
        print(f"TMDB lookup failed ({e}); falling back to manual naming.")
        return None
    out = []
    for m in data.get("results", []):
        year = (m.get("release_date") or "")[:4]
        if m.get("title") and year and len(out) < 5:
            out.append({"id": m["id"], "title": m["title"], "year": year, "runtime": None})
    for r in out:                       # the search doesn't include runtimes; ask per movie
        try:
            r["runtime"] = _tmdb_get(f"movie/{r['id']}").get("runtime") or None
        except Exception:
            pass
    return out


def runtime_ok(disc_min, tmdb_min):
    """True if the disc's main feature length is close to TMDB's runtime."""
    return bool(disc_min and tmdb_min and abs(disc_min - tmdb_min) <= max(6, 0.05 * tmdb_min))


def _parse_titles(text, tool):
    """Pull [(title_number, seconds), ...] out of HandBrake / MakeMKV scan output."""
    titles = []
    if tool == "makemkv":
        pat = re.compile(r'^TINFO:(\d+),9,\d+,"(\d+):(\d+):(\d+)"', re.M)   # attribute 9 = length
        for n, h, m, x in pat.findall(text):
            titles.append((int(n), int(h) * 3600 + int(m) * 60 + int(x)))
        return titles
    for m in re.finditer(r"\+ title (\d+):(.*?)(?=\+ title \d+:|\Z)", text, re.S):
        d = re.search(r"\+ duration: (\d+):(\d+):(\d+)", m.group(2))
        if d:
            titles.append((int(m.group(1)), int(d[1]) * 3600 + int(d[2]) * 60 + int(d[3])))
    return titles


def scan_titles(tool_path, tool, src, drive, log_path=None):
    """Scan the disc. Returns [(title_number, seconds), ...] (empty if the scan failed).

    Runs in a background thread while you answer the Movie/TV prompt. HandBrake numbers titles
    from 1 (the number to pass to '-t'); MakeMKV numbers them from 0 (the number its 'mkv'
    command takes). If the first HandBrake scan finds nothing it is retried once with plain
    arguments. Everything the tool printed (and its exit code) is saved to `log_path`.
    """
    if tool == "makemkv":
        attempts = [[tool_path, "-r", f"--minlength={TV_MIN_EPISODE}", "info", makemkv_source(drive)]]
    else:
        attempts = [[tool_path, "-i", src, "-t", "0", "--min-duration", "600",
                     "--previews", f"{SCAN_PREVIEWS}:0"],
                    [tool_path, "-i", src, "-t", "0"]]
    report, titles = [], []
    for cmd in attempts:
        report.append("$ " + subprocess.list2cmdline([str(c) for c in cmd]))
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=SCAN_TIMEOUT, **NOWIN)
            text = r.stdout.decode(errors="replace")
            report.append(f"(exit code {r.returncode}, {len(text)} characters of output)\n{text}")
            titles = _parse_titles(text, tool)
        except Exception as e:                       # timeout, tool missing, ...: retrying won't help
            report.append(f"SCAN FAILED: {e!r}")
            break
        if titles:
            break
    if log_path:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text("\n".join(report), encoding="utf-8")
        except OSError:
            pass
    return titles


def main_title(titles, tool):
    """The movie view of a scan: (main_minutes, main_title_number). Either may be None.

    HandBrake: the longest title and its number, so the rip can use '-t N' and skip a second
    full-disc scan. MakeMKV: longest title length only.
    """
    if not titles:
        return None, None
    num, secs = max(titles, key=lambda t: t[1])
    return round(secs / 60), (None if tool == "makemkv" else num)


def with_year(title, year):
    title = sanitize(title)
    return f"{title} ({year})" if year else title


def ask_manual(default):
    """Type a title (+ optional year). Returns (name, needs_review)."""
    ans = timed_input(f"Movie title [{default}] (auto-picks default after {PROMPT_TIMEOUT}s): ",
                      heading="Movie title", initial=default)
    if ans is None:
        print("No response; using the default title, flagged for review.")
        return default, True
    title = sanitize(ans) or default
    yr = timed_input("Year, e.g. 1944 (Enter to skip): ", heading="Year (optional, e.g. 1944)")
    yr = (yr or "").strip()
    return with_year(title, yr if re.fullmatch(r"(18|19|20)\d\d", yr) else ""), False


def choose_name(label, answered_in_time, disc_min=None):
    """Work out the output name. Returns (name, needs_review).

    With a TMDB key: search, then list up to 5 candidates (with TMDB runtime next to
    the disc's own length). Matches whose runtime fits the disc sort first, and Enter
    takes #1. If nobody answers in time: exactly ONE result with an exact title and a
    non-contradicting runtime is accepted into the library; otherwise the disc goes to
    the review folder, named after the best runtime fit (with year) if there is one,
    else the plain cleaned name. Without a key or when offline: ask for title and optional year.
    """
    default = default_title(label)
    results = tmdb_search(default)
    if results is None:
        if not answered_in_time:
            return default, True
        return ask_manual(default)

    for r in results:
        r["exact"] = _norm(r["title"]) == _norm(default)
        r["ok"] = runtime_ok(disc_min, r["runtime"])
    # runtime fits first (closest length wins), then exact titles; otherwise TMDB's order
    results.sort(key=lambda r: (not r["ok"], abs(disc_min - r["runtime"]) if r["ok"] else 0, not r["exact"]))

    auto = None
    if len(results) == 1 and results[0]["exact"]:
        r = results[0]
        if not (disc_min and r["runtime"]) or r["ok"]:
            auto = with_year(r["title"], r["year"])
    def no_answer():
        if auto:
            return auto, False
        if results and results[0]["ok"]:          # best runtime fit, but a human should confirm it
            return with_year(results[0]["title"], results[0]["year"]), True
        return default, True

    if not answered_in_time:
        return no_answer()
    if not results:
        print(f"No TMDB matches for '{default}'.")
        return ask_manual(default)

    print(f"TMDB matches for '{default}'" +
          (f"  (disc main feature: {disc_min} min)" if disc_min else "  (disc length unknown)") + ":")
    for i, r in enumerate(results, 1):
        rt = f"{r['runtime']} min" if r["runtime"] else "? min"
        print(f"  {i}) {r['title']} ({r['year']}) - {rt}" + ("   <- fits disc length" if r["ok"] else ""))
    print("  0) type my own")
    while True:
        opts = [(f"{r['title']} ({r['year']})   \u00b7   " + (f"{r['runtime']} min" if r["runtime"] else "? min")
                 + ("   \u00b7   fits disc length" if r["ok"] else ""), str(i)) for i, r in enumerate(results, 1)]
        opts.append(("None of these \u2014 type my own", "0"))
        ans = timed_input(f"Pick 1-{len(results)} (Enter = 1; no answer in {PROMPT_TIMEOUT}s = best fit, into Needs Review): ",
                          options=opts, default="1",
                          heading=f"Which movie is \u201c{default}\u201d?" + (f"  (disc: {disc_min} min)" if disc_min else ""))
        if ans is None:
            return no_answer()
        ans = ans.strip() or "1"
        if ans == "0":
            return ask_manual(default)
        if ans.isdigit() and 1 <= int(ans) <= len(results):
            r = results[int(ans) - 1]
            return with_year(r["title"], r["year"]), False
        print("Please type a number from the list.")


# ===== TV shows (beta): worked out from the disc; asks only for a season / disc number the label lacks =====
SEASON_RE = re.compile(r"(?<![a-z])(?:season|series|s)[\s._-]*0*(\d{1,2})(?!\d)", re.I)
DISC_RE = re.compile(r"(?<![a-z])(?:disc|disk|dvd|vol|volume|d)[\s._-]*0*(\d{1,2})(?!\d)", re.I)
_tv_lock = threading.Lock()


def parse_season_disc(label):
    """'BREAKING_BAD_S2_D1' -> (2, 1). Either part is None when the label doesn't say."""
    text = re.sub(r"[_\.]+", " ", label)
    s, d = SEASON_RE.search(text), DISC_RE.search(text)
    return (int(s[1]) if s else None), (int(d[1]) if d else None)


def tv_clean_label(label):
    """Disc label -> show-name guess, with the season/disc parts removed."""
    text = re.sub(r"[_\.]+", " ", label)
    text = DISC_RE.sub(" ", SEASON_RE.sub(" ", text))
    text = re.sub(r"\bcomplete\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", JUNK.sub(" ", text)).strip()
    return sanitize(text.title())


def _similar(a, b):
    """Loose title match: at least half of the shorter title's words appear in the other."""
    x, y = set(_norm(a).split()), set(_norm(b).split())
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.5


def tmdb_find_show(query):
    """Best TMDB TV match for the cleaned label as {id, name}, or None (no key / offline / no match)."""
    if not TMDB_API_KEY or not query:
        return None
    try:
        results = _tmdb_get("search/tv", query=query).get("results") or []
    except Exception as e:
        print(f"TMDB TV lookup failed ({e}); using the disc label as the show name.")
        return None
    results = [r for r in results if r.get("name") and _similar(r["name"], query)]

    def key(title):                                  # "The Golden Girls" and "Golden Girls" are the same title
        return re.sub(r"^(the|a|an)\s+", "", _norm(title or ""))
    exact = [r for r in results if key(r["name"]) == key(query) or key(r.get("original_name")) == key(query)]
    if exact:                                        # several shows can share a title: take the best known one
        best = max(exact, key=lambda r: (r.get("vote_count") or 0, r.get("popularity") or 0))
    else:
        best = results[0] if results else None
    return {"id": best["id"], "name": best["name"]} if best else None


def tmdb_episode_secs(show_id, season):
    """Typical episode length in seconds, from TMDB (the season's episodes, else the show's), or None."""
    runtimes = []
    if season:
        try:
            eps = _tmdb_get(f"tv/{show_id}/season/{season}").get("episodes") or []
            runtimes = [e["runtime"] for e in eps if e.get("runtime")]
        except Exception:
            pass
    if not runtimes:
        try:
            runtimes = [r for r in _tmdb_get(f"tv/{show_id}").get("episode_run_time") or [] if r]
        except Exception:
            pass
    return statistics.median(runtimes) * 60 if runtimes else None


def tmdb_season_count(show_id, season):
    """How many episodes TMDB lists for this season, or None."""
    try:
        return len(_tmdb_get(f"tv/{show_id}/season/{season}").get("episodes") or []) or None
    except Exception:
        return None


def tmdb_single_season(show_id):
    """True if TMDB says the show has exactly one season."""
    try:
        return _tmdb_get(f"tv/{show_id}").get("number_of_seasons") == 1
    except Exception:
        return False


def pick_episode_titles(titles, ref_secs=None, min_secs=TV_MIN_EPISODE):
    """Pick the episode titles out of a disc's [(title_number, seconds)] list.

    Episodes are the biggest group of similar-length titles (within 15% of each other; a group
    near TMDB's typical runtime wins if we know it). 'Play All' titles, trailers and featurettes
    are different lengths and fall away. Returns [(number, seconds)] in disc order.
    """
    cand = sorted((t for t in titles if t[1] >= min_secs), key=lambda t: t[0])
    if len(cand) <= 1:
        return cand
    best = None
    for c in cand:
        group = [t for t in cand if abs(t[1] - c[1]) <= 0.15 * c[1]]
        med = statistics.median(t[1] for t in group)
        near_ref = bool(ref_secs) and abs(med - ref_secs) <= 0.15 * ref_secs
        key = (near_ref, len(group), med)            # on a tie the longer group wins (episodes beat extras)
        if best is None or key > best[0]:
            best = (key, med)
    med = best[1]
    picked = [t for t in cand if 0.85 * med <= t[1] <= 1.25 * med]   # a bit wider (mostly upward), so a longer finale still counts
    return _trim_to_play_all(picked, cand)


def _trim_to_play_all(picked, cand):
    """Use a 'Play All' title as proof of which titles are really episodes.

    Discs usually have one title that plays every episode back to back. If a longer title is as
    long as a run of the picked titles added together (within 2%), that run is the episodes and
    anything else of similar length (a 22-minute extra, say) is not. No such title: unchanged.
    """
    if len(picked) < 3:
        return picked
    longest = max(t[1] for t in picked)
    for play_all in sorted((t for t in cand if t[1] > 1.5 * longest), key=lambda t: -t[1]):
        best = None                                   # (first, last) index of the longest matching run
        for i in range(len(picked)):
            total = 0
            for j in range(i, len(picked)):
                total += picked[j][1]
                if total > 1.02 * play_all[1]:
                    break
                if j > i and abs(total - play_all[1]) <= 0.02 * play_all[1]:
                    if best is None or j - i > best[1] - best[0]:
                        best = (i, j)
        if best:
            return picked[best[0]:best[1] + 1]
    return picked


def tv_start_episode(show, season, label, disc, count, have=(), season_total=None):
    """First episode number for this disc.

    Order of trust: this disc seen before (same numbers again, so a re-inserted disc resumes);
    the previous disc's remembered range; then what is actually in the show's folder (`have` =
    episode numbers of this season already there): a new disc carries on after the highest one;
    last resort, a guess from the disc number. Disc 1 always starts at 1.

    Safety net: if that would run past the end of the season (`season_total`, from TMDB), the
    numbers are pulled back so the disc's last episode is the season's last episode."""
    path = LOG_DIR / "tv_discs.json"
    with _tv_lock:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        entry = state.setdefault(f"{show}|{season}", {})
        me = _norm(label)
        prev = [e for k, e in entry.items() if disc and e.get("disc") == disc - 1 and k != me]
        if prev:                                             # the previous disc's range wins over this label's own record (which may be stale)
            start = prev[0]["start"] + prev[0]["count"]
        elif (me in entry and (not disc or entry[me].get("disc") in (None, disc))   # same label but another disc number (e.g. every disc labelled "..._SEASON1") is a different disc
              and not (disc and disc > 1 and entry[me]["start"] == 1)):             # a disc after the first never starts at E01
            start = entry[me]["start"]
        elif have and disc != 1:                             # a new disc: carry on after what's already there
            start = max(have) + 1
            if entry and not disc:
                start = max(start, max(e["start"] + e["count"] for e in entry.values()))
        elif disc and disc > 1:
            start = (disc - 1) * count + 1                   # earlier discs unseen: assume the same count
        elif entry and not disc:
            start = max(e["start"] + e["count"] for e in entry.values())
        else:
            start = 1
        if season_total and count <= season_total and start + count - 1 > season_total:
            fixed = max(1, season_total - count + 1)
            clash = [e for e in have if fixed <= e < fixed + count]
            if clash:                                        # shifting would land on episodes already ripped: don't
                print(f"TV (beta): numbering from E{start:02d} runs past the {season_total} episodes TMDB lists for "
                      f"season {season}, but E{fixed:02d}-E{fixed + count - 1:02d} are already in the folder, so keeping "
                      f"E{start:02d}. (TMDB may have matched the wrong show.)")
            else:
                print(f"TV (beta): numbering from E{start:02d} would run past the {season_total} episodes TMDB lists "
                      f"for season {season}; using E{fixed:02d} so this disc ends on the season finale.")
                start = fixed
        if entry.get(me) != {"start": start, "count": count, "disc": disc}:
            entry[me] = {"start": start, "count": count, "disc": disc}   # also refreshes a stale remembered count
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(state, indent=1), encoding="utf-8")
            except OSError:
                pass
        return start


def _tv_state_read():
    try:
        return json.loads((LOG_DIR / "tv_discs.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def tv_answers(label):
    """What you answered for this disc label last time ({'season': 1, 'disc': 2}), used as the default."""
    with _tv_lock:
        return dict(_tv_state_read().get("_answers", {}).get(_norm(label), {}))


def tv_remember(label, **answers):
    with _tv_lock:
        state = _tv_state_read()
        state.setdefault("_answers", {}).setdefault(_norm(label), {}).update(answers)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / "tv_discs.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
        except OSError:
            pass


def tv_guess_disc(show, season, label, have):
    """Best guess at the disc number when the label doesn't say: the same label's earlier disc number,
    else one more than the highest disc already seen for this season, else 2 if episodes are in the
    folder already, else 1."""
    with _tv_lock:
        entry = _tv_state_read().get(f"{show}|{season}", {})
    mine = entry.get(_norm(label), {})
    if mine.get("disc"):
        return mine["disc"]
    seen = [e.get("disc") or 0 for e in entry.values()]
    if any(seen):
        return max(seen) + 1
    return 2 if have else 1


def ask_tv_number(what, default, show, label):
    """Ask for a season or disc number (text box, `default` pre-filled). No answer in time = `default`."""
    while True:
        ans = timed_input(f"{show}: which {what} is this disc? [{default}] "
                          f"(Enter = {default}; no answer in {PROMPT_TIMEOUT}s = {default}): ",
                          heading=f"\u201c{label}\u201d doesn't say which {what} it is. {show}: {what}?  "
                                  f"(best guess: {default})",
                          initial=str(default))
        if ans is None:
            print(f"No answer; using {what} {default}.")
            return default
        ans = ans.strip() or str(default)
        if ans.isdigit() and 1 <= int(ans) <= 99:
            return int(ans)
        print("Please type a number from 1 to 99.")


def plan_tv_disc(label, titles, ask=None):
    """Everything TV mode decides, with no questions asked.

    Returns {show, season, disc, tag, stems, listed, episodes: [{num, secs, stem}]} (episodes may be
    empty). `stems` = rips already in the show's folder (NAS + local); `listed` False = NAS unreadable.

    `ask(what, default, show, label) -> int`, if given, is used for the season / disc number when the
    label doesn't give it (and TMDB doesn't settle the season); without it a best guess is used.
    """
    season, disc = parse_season_disc(label)
    season_sure = season is not None
    note = None
    query = tv_clean_label(label)
    found = tmdb_find_show(query)
    show = sanitize(found["name"]) if found else (query or "Unknown Show")
    if season is None and found and tmdb_single_season(found["id"]):
        season, note, season_sure = 1, "TMDB lists only one season for this show", True
    # What an earlier run already got to. The folder named after the TMDB match is the one that counts for
    # skipping; a folder named after the disc label (earlier discs, before the show was matched) also tells
    # us which season and episode numbers are taken.
    in_folder, listed = folder_stems(Path(REVIEW_DIR_NAME) / show)
    seen = set(in_folder)
    if query and query != show:
        seen |= folder_stems(Path(REVIEW_DIR_NAME) / query)[0]
    pairs = [(int(m[1]), int(m[2])) for st in seen for m in [re.search(r"S(\d+)E(\d+)", st)] if m]
    if season is None:
        seasons = {sn for sn, _ in pairs}
        if len(seasons) == 1:
            season, note = seasons.pop(), "the show's folder only holds that season so far"
        elif TV_DEFAULT_SEASON:
            season, note = TV_DEFAULT_SEASON, "the disc label doesn't say (TV_DEFAULT_SEASON)"
    if ask and not season_sure:                      # not certain: ask, with the best guess pre-filled
        season = ask("season", tv_answers(label).get("season") or season or 1, show, label)
        note = None
        tv_remember(label, season=season)
    have = {ep for sn, ep in pairs if sn == season} if season else set()
    if ask and disc is None and season:
        disc = ask("disc number", tv_answers(label).get("disc") or tv_guess_disc(show, season, label, have),
                   show, label)
        tv_remember(label, disc=disc)
    ref = tmdb_episode_secs(found["id"], season) if found else None
    picked = pick_episode_titles(titles, ref)
    if season:
        total = tmdb_season_count(found["id"], season) if found else None
        start = tv_start_episode(show, season, label, disc, len(picked), have, total)
        stems = [f"{show} - S{season:02d}E{start + i:02d}" for i in range(len(picked))]
        tag = f"S{season:02d}" + (f" D{disc}" if disc else "")
    else:                                          # season unknown: number within this disc, keep the label in the name
        text = sanitize(re.sub(r"[_\.]+", " ", label)) or "Disc"
        stems = [f"{show} - {text} - E{i + 1:02d}" for i in range(len(picked))]
        tag = text
    return {"show": show, "season": season, "disc": disc, "tag": tag, "ref": ref,
            "stems": in_folder, "listed": listed, "season_note": note,
            "episodes": [{"num": n, "secs": secs, "stem": stem} for (n, secs), stem in zip(picked, stems)]}


# ===== NAS upload (SMB, no mounting needed) =====
_nas_cfg_cache = None
_upload_lock = threading.Lock()
upload_state = {}      # live info for the window: file, index, total, done, size, started (or just error)


def nas_config():
    """Load nas.json ({host, share, folder, username, password}). None = NAS upload off."""
    global _nas_cfg_cache
    if _nas_cfg_cache is None:
        cfg = False
        saved = load_settings().get("nas")
        if isinstance(saved, dict):                  # set in the Settings window: wins over nas.json
            if saved.get("enabled", True) and saved.get("host") and saved.get("share"):
                cfg = saved                          # (switched off or incomplete there = NAS upload off)
        elif NAS_CONFIG_FILE.exists():
            try:
                data = json.loads(NAS_CONFIG_FILE.read_text(encoding="utf-8"))
                if data.get("host") and data.get("share"):
                    cfg = data
                else:
                    print(f"{NAS_CONFIG_FILE.name} needs at least 'host' and 'share'; NAS upload off.")
            except Exception as e:
                print(f"Couldn't read {NAS_CONFIG_FILE.name} ({e}); NAS upload off.")
        _nas_cfg_cache = cfg
    return _nas_cfg_cache or None


def _nas_client():
    import smbclient                      # imported lazily so NAS support is optional
    cfg = nas_config()
    smbclient.register_session(cfg["host"], username=cfg.get("username"),
                               password=cfg.get("password"), connection_timeout=10)
    return smbclient, cfg


def _nas_path(cfg, rel=None):
    """UNC path on the NAS for a path relative to OUTPUT_DIR (or the library root)."""
    folder = [p for p in cfg.get("folder", "").replace("\\", "/").split("/") if p]
    parts = [cfg["host"], cfg["share"], *folder, *(Path(rel).parts if rel else ())]
    return "\\\\" + "\\".join(parts)


def nas_has(rel):
    """True if this file is already on the NAS (False if no NAS or it can't be reached)."""
    if not nas_config():
        return False
    try:
        smb, cfg = _nas_client()
        return smb.path.exists(_nas_path(cfg, rel))
    except Exception:
        return False


def folder_stems(rel_dir):
    """Names (without extension) of the finished rips already in <library root>/rel_dir, on the
    NAS and in the local staging folder. Returns (set_of_names, listing_ok); listing_ok is False
    when a NAS is configured but its folder couldn't be read (then only local files are known)."""
    stems = set()
    local = OUTPUT_DIR / rel_dir
    if local.is_dir():
        stems |= {f.stem for f in local.rglob("*")      # includes the "Season N" subfolders
                  if f.is_file() and f.suffix.lower() in (".mp4", ".mkv") and ".partial" not in f.name}
    if not nas_config():
        return stems, True
    try:
        smb, cfg = _nas_client()
        path = _nas_path(cfg, rel_dir)
        if smb.path.exists(path):                       # no folder yet = nothing ripped yet
            for _dir, _subdirs, names in smb.walk(path):
                stems |= {Path(n).stem for n in names
                          if Path(n).suffix.lower() in (".mp4", ".mkv") and ".partial" not in n}
        return stems, True
    except Exception as e:
        print(f"Couldn't read the NAS folder '{rel_dir}' ({e}); going by local files only.")
        return stems, False


def nas_upload(local, rel, attempt=1):
    """Copy one file to the NAS (as .partN, then renamed). True if uploaded and verified.

    The copy runs in a helper thread so a hung connection can't freeze us: if the NAS accepts
    no data for UPLOAD_STALL_SECONDS the connection is dropped and TimeoutError is raised.
    """
    smb, cfg = _nas_client()
    dest = _nas_path(cfg, rel)
    if smb.path.exists(dest):
        print(f"NAS already has {rel}; keeping the local copy.")
        return False
    smb.makedirs(dest.rsplit("\\", 1)[0], exist_ok=True)
    tmp, size = f"{dest}.part{attempt}", local.stat().st_size
    result, abort = {}, threading.Event()

    def copy():
        done = 0
        try:
            with open(local, "rb") as src, smb.open_file(tmp, mode="wb") as out:
                while not abort.is_set():
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if not abort.is_set():
                        upload_state["done"], upload_state["last"] = done, time.time()
            result["ok"] = not abort.is_set()
        except Exception as e:
            result["err"] = e

    upload_state["last"] = time.time()
    worker, last_print = threading.Thread(target=copy, daemon=True), time.time()
    worker.start()
    try:
        while worker.is_alive():
            worker.join(2)
            now = time.time()
            if now - upload_state.get("last", now) > UPLOAD_STALL_SECONDS:
                abort.set()
                try:
                    smb.reset_connection_cache()      # closes the dead connection so the stuck write errors out
                except Exception:
                    pass
                raise TimeoutError(f"the NAS accepted no data for {UPLOAD_STALL_SECONDS}s "
                                   f"(stopped at {upload_state.get('done', 0) // 1000000} MB)")
            if now - last_print >= STATUS_SECONDS:
                print(f"\n-- uploading {rel}: {upload_state.get('done', 0) * 100 // max(size, 1)}%")
                last_print = now
        if "err" in result:
            raise result["err"]
        smb.rename(tmp, dest)
        return smb.stat(dest).st_size == size
    except Exception:
        try:
            smb.remove(tmp)
        except Exception:
            pass
        raise


def upload_pending():
    """Upload every finished rip in OUTPUT_DIR to the NAS, deleting each local copy after."""
    if not nas_config() or not OUTPUT_DIR.exists():
        return
    _upload_lock.acquire()                            # one upload pass at a time; later calls wait their turn
    try:
        files = sorted(f for pat in ("*.mp4", "*.mkv") for f in OUTPUT_DIR.rglob(pat)
                       if ".partial" not in str(f.relative_to(OUTPUT_DIR)))
        upload_state.clear()
        for n, f in enumerate(files, 1):
            rel = f.relative_to(OUTPUT_DIR)
            for attempt in range(1, UPLOAD_ATTEMPTS + 1):
                upload_state.update(file=str(rel), index=n, total=len(files), done=0,
                                    size=f.stat().st_size, started=time.time(), attempt=attempt)
                try:
                    if nas_upload(f, rel, attempt):
                        f.unlink()
                        print(f"\nUploaded to NAS, local copy removed: {rel}")
                    break
                except Exception as e:
                    print(f"\nNAS upload of '{rel}' failed on attempt {attempt}/{UPLOAD_ATTEMPTS} ({e}).")
                    if attempt == UPLOAD_ATTEMPTS:
                        print("Keeping it locally; will retry later.")
                        upload_state.clear()
                        upload_state["error"] = f"{rel}: {e}"
                        notify(APP_TITLE, f"NAS upload failed for '{rel.name}'; it's kept locally and will be retried.")
                        return               # NAS probably unreachable; don't hammer it
                    time.sleep(5)
        upload_state.clear()
    finally:
        _upload_lock.release()


def upload_async():
    threading.Thread(target=upload_pending, daemon=True).start()


def reload_nas(reset_connections=True):
    """Forget the cached NAS config (and the open connections) after the Settings window changed it."""
    global _nas_cfg_cache
    _nas_cfg_cache = None
    if reset_connections:
        try:
            import smbclient
            smbclient.reset_connection_cache()
        except Exception:
            pass


def nas_check(cfg):
    """Try to log in with `cfg` and write a small file. Returns (ok, message). Uses its own connection
    cache, so testing never disturbs an upload that is running."""
    try:
        import smbclient
    except ImportError:
        return False, "The 'smbprotocol' package isn't installed (pip install smbprotocol)."
    cache = {}
    try:
        smbclient.register_session(cfg["host"], username=cfg.get("username") or None,
                                   password=cfg.get("password") or None, connection_timeout=10,
                                   connection_cache=cache)
        base = _nas_path(cfg)
        smbclient.makedirs(base, exist_ok=True, connection_cache=cache)
        probe = base + "\\_rip_test.txt"
        with smbclient.open_file(probe, mode="w", connection_cache=cache) as f:
            f.write("ok")
        smbclient.remove(probe, connection_cache=cache)
        return True, f"OK: can read and write {base}"
    except Exception as e:
        return False, f"Failed: {e}"
    finally:
        try:
            smbclient.reset_connection_cache(connection_cache=cache)
        except Exception:
            pass


def nas_test():
    cfg = nas_config()
    if not cfg:
        print(f"No usable NAS config at {NAS_CONFIG_FILE}")
        return
    ok, msg = nas_check(cfg)
    print(f"NAS {msg}" if ok else f"NAS test failed: {msg}")


# ===== Rip jobs (one HandBrakeCLI subprocess each) =====
def _unthrottle(proc):
    """Windows 11 can put a background child process into Efficiency mode (EcoQoS), which on
    Intel hybrid CPUs can push it onto the slow E-cores. Explicitly opt HandBrake out."""
    try:
        import ctypes
        from ctypes import wintypes

        class THROTTLE(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]
        k32 = ctypes.windll.kernel32
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
        state = THROTTLE(1, 1, 0)   # version 1; control EXECUTION_SPEED; state 0 = throttling OFF
        k32.SetProcessInformation(int(proc._handle), 4, ctypes.byref(state), ctypes.sizeof(state))
    except Exception:
        pass


class Job:
    """One rip subprocess: HandBrakeCLI (DVD -> mp4) or makemkvcon (Blu-ray -> mkv)."""

    def __init__(self, drive, stem, final, partial, cmd, tool="handbrake", log_path=None, log_mode="w"):
        self.drive, self.name, self.final, self.partial, self.tool = drive, stem, final, partial, tool
        self.percent = 0.0
        self.fps = 0.0
        self.task = None                 # (current pass, total passes) from HandBrake
        self.started = time.time()
        self.last_output = time.time()   # when the tool last printed anything (to spot a hung drive)
        self._on_progress_line = False
        self.log_path = log_path or LOG_DIR / f"{stem}.log"
        self.log = open(self.log_path, log_mode, encoding="utf-8", errors="replace", newline="")
        self.log.write("Command: " + subprocess.list2cmdline([str(c) for c in cmd]) + "\n\n")
        self.log.flush()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **NOWIN)
        if OS == "Windows":
            _unthrottle(self.proc)
        target = self._pump_makemkv if tool == "makemkv" else self._pump
        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def _pump(self):
        """HandBrake: save output to the log and track the progress %."""
        pat = re.compile(r"(\d+(?:\.\d+)?) %")
        for chunk in iter(lambda: self.proc.stdout.read1(4096), b""):
            text = chunk.decode(errors="replace")
            self.last_output = time.time()
            self.log.write(text)
            self.log.flush()
            found = pat.findall(text)
            if found:
                self.percent = float(found[-1])
            fps = re.findall(r"\(([\d.]+) fps", text)
            if fps:
                self.fps = float(fps[-1])
            task = re.findall(r"task (\d+) of (\d+)", text)
            if task:
                self.task = (int(task[-1][0]), int(task[-1][1]))
        self.log.close()

    def _pump_makemkv(self):
        """MakeMKV robot output: turn PRGV lines into a readable progress line, log the rest."""
        buf = b""
        while True:
            chunk = self.proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.decode(errors="replace").rstrip("\r")
                if line.startswith("PRGV:"):
                    try:
                        _, total, mx = line[5:].split(",")[:3]
                        self.percent = int(total) / int(mx) * 100 if int(mx) else 0.0
                        self.log.write(f"\rMakeMKV progress: {self.percent:5.1f} %")
                        self._on_progress_line = True
                    except ValueError:
                        pass
                elif line.startswith(("PRGT:", "PRGC:")):
                    continue
                else:
                    self.log.write(("\n" if self._on_progress_line else "") + line + "\n")
                    self._on_progress_line = False
                self.log.flush()
        self.log.close()

    @property
    def overall(self):
        """Progress across all passes (2-pass encodes restart their % for pass 2)."""
        if self.task and self.task[1] > 1:
            return ((self.task[0] - 1) * 100 + self.percent) / self.task[1]
        return self.percent

    def poll(self):
        return self.proc.poll()

    def mark(self, text):
        """Append an end marker to the log so a live log window knows to finish."""
        try:
            with open(self.log_path, "a", encoding="utf-8", newline="") as f:
                f.write(f"\n=== RIP {text} ===\n")
        except OSError:
            pass

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def discard_partial(self):
        try:
            if self.partial.is_dir():
                shutil.rmtree(self.partial, ignore_errors=True)
            else:
                self.partial.unlink(missing_ok=True)
        except OSError:
            pass

    def finish_output(self):
        """Move the finished rip into place. Returns True on success."""
        if self.tool == "makemkv":
            mkvs = (sorted(self.partial.glob("*.mkv"), key=lambda p: p.stat().st_size, reverse=True)
                    if self.partial.is_dir() else [])
            if not mkvs:
                return False
            os.replace(mkvs[0], self.final)          # biggest title = the main feature
            shutil.rmtree(self.partial, ignore_errors=True)
            return True
        if self.partial.exists():
            os.replace(self.partial, self.final)
            return True
        return False


class TVJob:
    """One TV disc (beta): rips each episode title in turn, one HandBrake/MakeMKV run per episode,
    all into one folder. Looks like a Job to the rest of the program (poll/stop/overall/...).
    Each finished episode is kept (and uploaded) straight away; a failed one doesn't stop the rest."""
    is_tv = True

    def __init__(self, drive, name, show_dir, episodes, tool, tool_path, src, hb_args, existing=None):
        self.drive, self.name, self.final, self.tool = drive, name, show_dir, tool
        self.existing = existing           # stems already in the show's folder; None = couldn't list the NAS
        self.episodes, self.tool_path, self.src, self.hb_args = episodes, tool_path, src, hb_args
        self.started = time.time()
        self.cur = None                    # the running (or last) per-episode Job
        self.completed = 0                 # episodes finished, skipped or failed
        self.done, self.skipped, self.failed, self.untried = [], [], [], []
        self._stop, self._lock = threading.Event(), threading.Lock()
        self.log_path = LOG_DIR / f"{name}.log"
        with open(self.log_path, "w", encoding="utf-8", newline="") as f:
            f.write(f"TV disc (beta): {name}\n")
            for i, ep in enumerate(episodes, 1):
                f.write(f"  {i}. title {ep['num']} ({ep['secs'] // 60} min) -> {ep['final'].name}\n")
            f.write("\n")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # --- what the window / status lines read ---
    @property
    def percent(self):
        return self.cur.percent if self.cur else 0.0

    @property
    def fps(self):
        return self.cur.fps if self.cur else 0.0

    @property
    def task(self):
        return self.cur.task if self.cur else None

    @property
    def overall(self):
        n = max(len(self.episodes), 1)
        inner = self.cur.overall if (self.cur and self.completed < n) else 0.0
        return min(100.0, (self.completed + inner / 100) / n * 100)

    @property
    def status_text(self):
        n = len(self.episodes)
        return f"Episode {min(self.completed + 1, n)} of {n}"

    @property
    def partial(self):
        return self.cur.partial if self.cur else None

    def _log(self, text):
        try:
            with open(self.log_path, "a", encoding="utf-8", newline="") as f:
                f.write(text + "\n")
        except OSError:
            pass

    def _have(self, ep):
        """Is this episode already ripped (locally, or in the NAS folder listing read at the start)?"""
        if ep["final"].exists():
            return True
        if self.existing is not None:
            return ep["stem"] in self.existing
        return nas_has(ep["final"].relative_to(OUTPUT_DIR))       # no listing: ask the NAS file by file

    def _cmd(self, ep):
        if self.tool == "makemkv":
            return [self.tool_path, "-r", "--progress=-same", f"--minlength={TV_MIN_EPISODE}",
                    "mkv", makemkv_source(self.drive), str(ep["num"]), str(ep["partial"])]
        return [self.tool_path, "-i", self.src, "-o", str(ep["partial"]), "-t", str(ep["num"]),
                "--previews", f"{SCAN_PREVIEWS}:0", *self.hb_args]

    def _wait(self, job):
        """Wait for the rip to end and return its exit code. If HandBrake is alive but the drive has
        stopped feeding it (under TV_STALL_FPS for TV_STALL_SECONDS, or silent that long) it is
        stopped, instead of leaving it to give up on its own minutes later."""
        slow_since = None
        while True:
            try:
                return job.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if self.tool != "handbrake" or not 0 < job.percent < 99:
                slow_since = None
                continue
            now = time.time()
            slow_since = (slow_since or now) if job.fps < TV_STALL_FPS else None
            if (slow_since and now - slow_since > TV_STALL_SECONDS) or now - job.last_output > TV_STALL_SECONDS:
                self._log(f"=== The drive stopped delivering data (stuck at {job.percent:.1f}%, "
                          f"{job.fps:.1f} fps for {TV_STALL_SECONDS}s); stopping this try ===")
                job.stop()
                rc = job.proc.returncode
                return rc if rc else -1

    def _attempt(self, ep, i, n, retry=False):
        """One try at one episode. Returns (ok, exit_code)."""
        try:                                         # leftover from a crashed earlier run / failed try
            if ep["partial"].is_dir():
                shutil.rmtree(ep["partial"], ignore_errors=True)
            else:
                ep["partial"].unlink(missing_ok=True)
            if self.tool == "makemkv":
                ep["partial"].mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._log(f"=== Episode {i}/{n}{' (second try)' if retry else ''}: title {ep['num']} "
                  f"-> {ep['final'].name} ===")
        with self._lock:
            if self._stop.is_set():
                return False, None
            job = Job(self.drive, ep["stem"], ep["final"], ep["partial"], self._cmd(ep),
                      self.tool, log_path=self.log_path, log_mode="a")
            self.cur = job
        rc = self._wait(job)
        job.thread.join(timeout=5)
        if rc == 0 and not self._stop.is_set() and job.finish_output():
            return True, rc
        job.discard_partial()
        return False, rc

    def _run(self):
        n = len(self.episodes)
        streak = 0                                   # episodes in a row that failed
        try:
            for i, ep in enumerate(self.episodes, 1):
                if self._stop.is_set():
                    break
                if self._have(ep):
                    self._log(f"=== Episode {i}/{n}: {ep['final'].name} already exists; skipped ===")
                    self.skipped.append(ep)
                    self.completed += 1
                    continue
                ok, rc = self._attempt(ep, i, n)
                if not ok and not self._stop.is_set() and streak == 0:
                    # After a read error the drive usually refuses to open the disc until it is re-seated,
                    # so eject + close the tray, then try that episode once more.
                    self._log(f"=== Episode {i}/{n} failed (exit code {rc}); re-seating the disc "
                              "(eject and close the tray), then trying once more ===")
                    if not reseat(self.drive, self._stop, self._log) and not self._stop.is_set():
                        self._log(f"=== couldn't re-seat the disc automatically; waiting {TV_RETRY_WAIT}s instead ===")
                        if self._stop.wait(TV_RETRY_WAIT):
                            break
                    ok, rc = self._attempt(ep, i, n, retry=True)
                if self._stop.is_set():
                    break
                if ok:
                    streak = 0
                    self.done.append(ep)
                    self._log(f"=== Episode {i}/{n} finished ===")
                    upload_async()
                else:
                    streak += 1
                    self.failed.append(ep)
                    self._log(f"=== Episode {i}/{n} FAILED (exit code {rc}) ===")
                    if streak >= 2:                  # the drive has almost certainly stopped reading the disc
                        self.untried = self.episodes[i:]
                        self._log("=== Stopping: two episodes in a row failed, so the drive has probably "
                                  "stopped reading the disc (dirty/scratched disc?). Clean it and put it back "
                                  "in; finished episodes are skipped. ===")
                        self.completed = n
                        break
                self.completed += 1
        except Exception:
            self._log("=== TV rip crashed ===\n" + traceback.format_exc())
            self.failed.append({"final": self.final})
        self._log(f"=== {len(self.done)} ripped, {len(self.skipped)} skipped, {len(self.failed)} failed"
                  + (f", {len(self.untried)} not tried" if self.untried else "") + " ===")

    def summary(self):
        """One readable line about what's missing, for the console."""
        left = [ep["final"].name for ep in self.failed + self.untried if "stem" in ep] or \
               [ep["final"].name for ep in self.episodes if ep not in self.done and ep not in self.skipped]
        return (f"[{self.drive}] {len(self.done)} of {len(self.episodes)} episodes ripped"
                + (f" ({len(self.skipped)} already existed)" if self.skipped else "")
                + (". Not ripped: " + ", ".join(left) + ". Clean the disc if needed and put it back in; "
                   "episodes that are already done are skipped." if left else "."))

    # --- same interface as Job ---
    def poll(self):
        if self.thread.is_alive():
            return None
        return 0 if not self.failed and not self._stop.is_set() else 1

    def mark(self, text):
        self._log(f"\n=== RIP {text} ===")

    def stop(self):
        self._stop.set()
        with self._lock:
            cur = self.cur
        if cur:
            cur.stop()

    def discard_partial(self):
        if self.cur:
            self.cur.discard_partial()

    def finish_output(self):
        return True                                  # episodes were already moved into place one by one


_gui = None          # the RipperGUI when running with a window


def timed_input(prompt, timeout=PROMPT_TIMEOUT, options=None, default=None, heading=None, initial=""):
    """input() that gives up after `timeout` seconds of no typing (returns None).

    In the window, `options` [(label, value), ...] become buttons (returns the value),
    otherwise a text box (pre-filled with `initial`). `heading` replaces `prompt` there.
    """
    notify(APP_TITLE, f"Needs your answer: {heading or prompt.strip()}")
    if _gui is not None:
        return _gui.ask(prompt, timeout, options, default, heading, initial)
    print(prompt, end="", flush=True)
    if OS == "Windows":
        import msvcrt
        buf, deadline = "", time.time() + timeout
        while time.time() < deadline:
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            ch = msvcrt.getwch()
            deadline = time.time() + timeout          # any keypress resets the timer
            if ch in ("\r", "\n"):
                print()
                return buf
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()                        # swallow arrow/function keys
            elif ch == "\b":
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            else:
                buf += ch
                sys.stdout.write(ch)
                sys.stdout.flush()
        print()
        return None
    import select
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().rstrip("\n")
    print()
    return None


def open_log_window(job):
    """Windows: pop up a separate console that follows this rip's log live (console mode only;
    the window has its own Log button)."""
    if _gui is not None or OS != "Windows" or not SHOW_LOG_WINDOW:
        return
    base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
    try:
        subprocess.Popen(base + ["--tail", str(job.log_path), f"Rip: {job.name}"],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    except Exception as e:
        print(f"Couldn't open log window: {e}")


def tail_log(path, title):
    """Runs in the separate window: follow the log until the rip ends."""
    if OS == "Windows":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass
    print(f"{title}\nLog file: {path}\n")
    end = re.compile(r"=== RIP (FINISHED|FAILED|STOPPED)[^=]*===")
    tail, result = "", None
    with open(path, "rb") as f:
        while result is None:
            chunk = f.read(4096)
            if not chunk:
                time.sleep(0.5)
                continue
            text = chunk.decode(errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            tail = (tail + text)[-200:]
            m = end.search(tail)
            if m:
                result = m.group(1)
    if result == "FINISHED":
        print("\nThis window will close in 5 seconds.")
        time.sleep(5)
    else:
        input("\nPress Enter to close this window...")


def _fmt_len(secs):
    secs = int(secs)
    return f"{secs // 3600}h{secs % 3600 // 60:02d}" if secs >= 3600 else f"{round(secs / 60)} min"


def guess_disc_kind(titles):
    """Movie or TV, judged from the disc's titles alone. Returns (kind, reason); kind is 'm', 't' or
    None when there's nothing to judge by.

    TV: several titles of about the same length (TV_MIN_EPISODE or longer), above all when a longer
    'Play All' title is exactly their sum. Movie: one long title, or a couple of long ones of about
    the same length (widescreen + fullscreen, theatrical + extended cut); also a disc with one title far
    longer than a few similar-length extras. Anything unclear is a movie, as it always was."""
    cand = [t for t in titles if t[1] >= TV_MIN_EPISODE]
    if not cand:
        return None, "no title long enough to judge by"
    cluster = pick_episode_titles(titles)                  # biggest group of similar-length titles (Play All trimmed)
    n = len(cluster)
    longest = max(cand, key=lambda t: t[1])
    med = statistics.median(secs for _, secs in cluster) if cluster else 0
    total = sum(secs for _, secs in cluster)
    play_all = n >= 2 and any(t not in cluster and abs(t[1] - total) <= 0.02 * t[1] for t in cand)
    dominant = (longest not in cluster and longest[1] >= 3600 and longest[1] >= 1.8 * med)
    if play_all:
        return "t", f"{n} titles of about {_fmt_len(med)} and a longer 'Play All' ({_fmt_len(longest[1])}) that adds up to them"
    if dominant:
        return "m", f"one long title ({_fmt_len(longest[1])}) with only shorter extras"
    if n >= 3:
        return "t", f"{n} titles of about the same length ({_fmt_len(med)})"
    if n == 2:
        if med >= 3600:
            return "m", f"two long titles of about the same length ({_fmt_len(med)}): widescreen/fullscreen or two cuts"
        return "t", f"2 titles of about the same length ({_fmt_len(med)})"
    return "m", f"no group of similar-length titles (longest is {_fmt_len(longest[1])})"


def auto_kind(drive, scan, scan_thread):
    """Nobody answered Movie/TV in time: look at the disc's titles instead of blindly assuming a movie."""
    if scan_thread and scan_thread.is_alive():
        print(f"[{drive}] No response; reading the disc's titles to decide...")
        scan_thread.join(timeout=SCAN_TIMEOUT + 30)
    kind, why = guess_disc_kind(scan.get("titles") or [])
    if kind is None:
        print(f"[{drive}] No response, and the disc couldn't be judged ({why}); assuming movie.")
        return "m"
    print(f"[{drive}] No response; this looks like a {'TV show' if kind == 't' else 'movie'}: {why}.")
    return kind


def start_job(hb, drive, info, active):
    """Ask movie/TV (+ title) in the main process, then launch the rip subprocess.

    Prompts auto-answer after PROMPT_TIMEOUT seconds with no typing. Movie/TV is then judged from
    the disc's titles (see guess_disc_kind); a movie gets the default title. TV shows (beta) only ask for a season / disc number the label doesn't give; see start_tv_job(). If the output file already exists, nothing is ripped and the disc is ejected.
    DVDs go to HandBrake (mp4); Blu-rays go to MakeMKV (mkv).
    """
    src, label, kind_of_disc = info
    print(f"\nDisc detected in {drive}: {label}  [{'Blu-ray' if kind_of_disc == 'bd' else 'DVD'}]")
    scan = {}
    scan_tool = find_makemkv() if kind_of_disc == "bd" else hb
    scan_name = "makemkv" if kind_of_disc == "bd" else "handbrake"
    scan_thread = None

    def do_scan():
        scan["log"] = LOG_DIR / f"scan - {sanitize(label)}.log"
        scan["titles"] = scan_titles(scan_tool, scan_name, src, drive, scan["log"])
        scan["m"], scan["t"] = main_title(scan["titles"], scan_name)
    if scan_tool:                            # read the disc's titles while you answer the prompt
        scan_thread = threading.Thread(target=do_scan, daemon=True)
        scan_thread.start()
    kind, timed_out = "", False
    while kind not in ("m", "t"):
        ans = timed_input(f"Movie or TV show? [m/t] (no answer in {PROMPT_TIMEOUT}s = judged from the disc's titles): ",
                          options=[("Movie", "m"), ("TV show (beta)", "t")], default="m",
                          heading=f"Disc in {drive}: \u201c{label}\u201d. Movie or TV show?   "
                                  f"(no answer in {PROMPT_TIMEOUT}s: I'll judge from the disc's titles)")
        if ans is None:
            kind, timed_out = auto_kind(drive, scan, scan_thread), True
        else:
            kind = ans.strip().lower()[:1]
    if kind == "t":
        return start_tv_job(hb, drive, info, scan, scan_thread, active)
    if scan_thread and scan_thread.is_alive():
        print("Reading the disc's length (needed to match the movie)...")
        scan_thread.join(timeout=SCAN_TIMEOUT + 30)
    if scan.get("t"):
        print(f"[{drive}] main title is #{scan['t']} ({scan.get('m')} min).")
    name, needs_review = choose_name(label, answered_in_time=not timed_out, disc_min=scan.get("m"))
    out_dir = OUTPUT_DIR / REVIEW_DIR_NAME if needs_review else OUTPUT_DIR
    if needs_review:
        print(f"[{drive}] couldn't confirm the title/year; ripping to '{REVIEW_DIR_NAME}' as '{name}'.")

    if kind_of_disc == "bd":
        tool_path = find_makemkv()
        if not tool_path:
            print(f"[{drive}] MakeMKV (makemkvcon) not found; can't rip Blu-rays. Ejecting.")
            safe_eject(drive)
            return None
        final = out_dir / f"{name}.mkv"
        partial = out_dir / f"{name}.partial"          # a folder; MakeMKV writes inside it
        tool = "makemkv"
    else:
        tool_path, tool = hb, "handbrake"
        final = out_dir / f"{name}.mp4"
        partial = out_dir / f"{name}.partial.mp4"

    if (final.exists() or any(j.partial == partial for j in active.values())
            or nas_has(final.relative_to(OUTPUT_DIR))):
        print(f"[{drive}] '{final.name}' already exists or is already being ripped; "
              "not ripping. Ejecting.")
        safe_eject(drive)
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:                                     # leftover from a crashed earlier run
        if partial.is_dir():
            shutil.rmtree(partial, ignore_errors=True)
        else:
            partial.unlink(missing_ok=True)
    except OSError:
        pass

    if tool == "makemkv":
        partial.mkdir(parents=True, exist_ok=True)
        cmd = [tool_path, "-r", "--progress=-same", f"--minlength={BD_MIN_LENGTH}",
               "mkv", makemkv_source(drive), "all", str(partial)]
    else:
        # Known title from the scan -> HandBrake scans only that one; else fall back to --main-feature
        title_args = ["-t", str(scan["t"])] if scan.get("t") else ["--main-feature"]
        cmd = [tool_path, "-i", src, "-o", str(partial), *title_args,
               "--previews", f"{SCAN_PREVIEWS}:0", *preset_args()]
    print(f"[{drive}] ripping in the background -> {final}")
    job = Job(drive, name, final, partial, cmd, tool)
    open_log_window(job)
    return job


def start_tv_job(hb, drive, info, scan, scan_thread, active):
    """TV show (beta): no questions. Uses the disc scan already running in the background to
    find the episode titles, names them from TMDB + the disc label, and rips them one after
    another into _Needs Review/<show name>/ (so a person checks the numbering later)."""
    src, label, kind_of_disc = info
    if scan_thread and scan_thread.is_alive():
        print(f"[{drive}] TV (beta): reading the disc's titles...")
        scan_thread.join(timeout=SCAN_TIMEOUT + 30)
    titles = scan.get("titles") or []
    if kind_of_disc == "bd":
        tool_path, tool, ext = find_makemkv(), "makemkv", ".mkv"
        if not tool_path:
            print(f"[{drive}] MakeMKV (makemkvcon) not found; can't rip Blu-rays. Ejecting.")
            safe_eject(drive)
            return None
    else:
        tool_path, tool, ext = hb, "handbrake", ".mp4"
    print(f"[{drive}] TV (beta): the scan found {len(titles)} title(s)"
          + (": " + ", ".join(f"#{n} {secs // 60}m" for n, secs in titles) if titles else "."))
    plan = plan_tv_disc(label, titles, ask=ask_tv_number) if titles else None
    if not plan or not plan["episodes"]:
        if not titles:
            print(f"[{drive}] TV (beta): couldn't read any titles from the disc. "
                  f"What the scan said is saved in: {scan.get('log') or 'n/a'}")
        else:
            print(f"[{drive}] TV (beta): none of those is at least {TV_MIN_EPISODE // 60} minutes long "
                  "(TV_MIN_EPISODE at the top of rip.py), so there are no episodes to rip.")
        print(f"[{drive}] Ejecting. (If it stays in, eject it by hand and put it back to try again.)")
        safe_eject(drive)
        return None

    show_dir = OUTPUT_DIR / REVIEW_DIR_NAME / plan["show"]
    ep_dir = show_dir / f"Season {plan['season']}" if plan["season"] else show_dir     # <show>/Season X/<files>
    for ep in plan["episodes"]:
        ep["final"] = ep_dir / f"{ep['stem']}{ext}"
        ep["partial"] = ep_dir / (f"{ep['stem']}.partial" if tool == "makemkv" else f"{ep['stem']}.partial.mp4")
    name = sanitize(f"{plan['show']} - {plan['tag']}")
    print(f"[{drive}] TV (beta): '{plan['show']}'"
          + (f" season {plan['season']}" if plan["season"] else ", season unknown")
          + (f", disc {plan['disc']}" if plan["disc"] else "")
          + f" -> {len(plan['episodes'])} episode(s) into '{ep_dir.relative_to(OUTPUT_DIR).as_posix()}':")
    if plan["season_note"]:
        print(f"    (assuming season {plan['season']}: {plan['season_note']})")
    for ep in plan["episodes"]:
        ep["there"] = ep["stem"] in plan["stems"]
        print(f"    title {ep['num']} ({ep['secs'] // 60} min) -> {ep['final'].name}"
              + ("   [already there; will skip]" if ep["there"] else ""))
    if not plan["listed"]:
        print(f"[{drive}] (couldn't read the NAS folder, so only local files were checked)")
    if all(ep["there"] for ep in plan["episodes"]):
        print(f"[{drive}] TV (beta): every episode on this disc is already in '{REVIEW_DIR_NAME}/{plan['show']}'; "
              "nothing to rip. Ejecting.")
        safe_eject(drive)
        return None
    ep_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    job = TVJob(drive, name, show_dir, plan["episodes"], tool, tool_path, src,
                preset_args(preset_file_for("tv")) if tool == "handbrake" else None,
                existing=plan["stems"] if plan["listed"] else None)
    open_log_window(job)
    return job


def reap(active):
    """Find rip subprocesses that have ended (for any reason) and eject their drives."""
    for drive, job in list(active.items()):
        rc = job.poll()
        if rc is None:
            continue
        job.thread.join(timeout=5)
        finished = False
        if rc == 0 and job.finish_output():
            finished = True
            job.mark("FINISHED")
            print(f"\n[{drive}] finished: {job.final.name}")
            upload_async()
        else:
            job.discard_partial()
            job.mark(f"FAILED (exit code {rc})")
            notify(APP_TITLE, f"The rip of '{job.name}' in {drive} failed (exit code {rc}).")
            print(f"\n[{drive}] rip of '{job.name}' ended without finishing "
                  f"(exit code {rc}); see {LOG_DIR / (job.name + '.log')}")
            if getattr(job, "is_tv", False):
                print(job.summary())
        ejected = safe_eject(drive)
        if finished and ejected:
            notify(APP_TITLE, f"Finished '{job.name}' in {drive}. The disc has been ejected.")
        del active[drive]


def print_status(active):
    if active:
        parts = [f"{d} {j.name} {j.percent:.0f}%" + (f" {j.fps:.0f} fps" if j.fps else "")
                 for d, j in active.items()]
        print("\n-- ripping: " + " | ".join(parts))


def shutdown(active):
    print("\nStopping...")
    for drive, job in list(active.items()):
        job.stop()
        job.thread.join(timeout=5)
        job.discard_partial()
        job.mark("STOPPED")
        safe_eject(drive)
    active.clear()


# ===== Window (tkinter) =====
class UserQuit(Exception):
    """Raised in the watcher thread when the window is closing."""


drive_state = {}     # drive id -> short text shown in the window ("Empty", "Ripping: ...")

BG, PANEL, PANEL2, FG, MUTED = "#0e1014", "#161920", "#20242e", "#eceff4", "#8b93a3"
ACCENT, ACCENT_HI, GOOD, BAD, PROMPT_BG = "#7c6cff", "#9486ff", "#34d399", "#f87171", "#1a1736"
BORDER, WELL = "#2a2f3b", "#0b0d10"                  # card outlines; recessed areas (log, progress trough, text boxes)
UI_FONT, MONO_FONT = "Segoe UI", "Consolas"          # replaced by nicer installed fonts in RipperGUI._style()


def _dark_titlebar(win):
    """Windows 10/11: make the title bar dark to match the window (silently does nothing elsewhere)."""
    if OS != "Windows":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):                        # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 2004+ / older builds)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on))
    except Exception:
        pass


_ICON_KEEP = []                                      # Tk drops images that nothing references


def _find_icon(name):
    for base in (BUNDLE, HERE):
        if (base / name).exists():
            return base / name
    return None


def _set_icon(win):
    """Give a window the Rippy logo (title bar, taskbar, Alt-Tab). Silently does nothing if the files are missing."""
    try:
        ico = _find_icon("rippy.ico")
        if OS == "Windows" and ico:
            win.iconbitmap(default=str(ico)) if isinstance(win, tk.Tk) else win.iconbitmap(str(ico))
        png = _find_icon("rippy.png")
        if png:
            img = tk.PhotoImage(file=str(png))
            _ICON_KEEP.append(img)
            win.iconphoto(isinstance(win, tk.Tk), img)   # True on the main window = also the default for child windows
    except Exception:
        pass


class RoundFrame(tk.Canvas if tk else object):
    """A card with rounded corners. Tk can't round a normal frame, so this is a Canvas that draws the
    rounded outline and hosts a ttk.Frame (`.body`) that you put your widgets in.

    By default the card is as tall as its contents. `stretch=True` makes the contents fill the card instead
    (for a log box that expands with the window); `fit_width=True` makes it as wide as its contents (chips).
    """
    def __init__(self, parent, fill=PANEL, outline=BORDER, radius=14, pad=16, stretch=False, fit_width=False,
                 body_style="Inner.TFrame", min_height=40):
        super().__init__(parent, bg=BG, highlightthickness=0, bd=0, height=min_height, width=10)
        self.fill, self.outline, self.radius, self.pad = fill, outline, radius, pad
        self.stretch, self.fit_width = stretch, fit_width
        self.body = ttk.Frame(self, style=body_style)
        self._win = self.create_window(pad, pad, anchor="nw", window=self.body)
        self.bind("<Configure>", self._redraw)
        self.body.bind("<Configure>", self._fit)

    def _fit(self, _e=None):
        w = self.body.winfo_reqwidth() + 2 * self.pad if self.fit_width else None
        h = None if self.stretch else self.body.winfo_reqheight() + 2 * self.pad
        if (h is not None and int(self.cget("height")) != h) or (w is not None and int(self.cget("width")) != w):
            self.configure(**({"height": h} if h is not None else {}), **({"width": w} if w is not None else {}))

    def _redraw(self, _e=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        self.delete("card")
        x1, y1, x2, y2, r = 0.5, 0.5, w - 1.5, h - 1.5, min(self.radius, (h - 2) / 2, (w - 2) / 2)
        pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r,
               x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
               x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]          # doubled points keep the straight edges straight
        self.create_polygon(pts, smooth=True, fill=self.fill, outline=self.outline, width=1, tags="card")
        self.tag_lower("card")
        opts = {"width": max(w - 2 * self.pad, 1)}
        if self.stretch:
            opts["height"] = max(h - 2 * self.pad, 1)
        self.itemconfigure(self._win, **opts)


class _QueueWriter:
    """Stands in for sys.stdout so every print() lands in the window's Activity log."""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s or "")

    def flush(self):
        pass

    def isatty(self):
        return False


def _tag_for(line):
    if re.search(r"fail|error|couldn't|could not|not found|ended without", line, re.I):
        return "err"
    if re.search(r"finished|uploaded to nas|nas ok", line, re.I):
        return "ok"
    return None


class LogText:
    """Read-only, auto-scrolling log box. Understands \\r (HandBrake's in-place progress lines)."""
    def __init__(self, parent, max_lines=3000):
        self.frame = ttk.Frame(parent)
        self.max_lines, self.carry = max_lines, ""
        self.autoscroll = tk.BooleanVar(master=parent, value=True)
        ttk.Checkbutton(self.frame, text="Auto-scroll to bottom", variable=self.autoscroll,
                        command=lambda: self.autoscroll.get() and self.text.see("end")
                        ).pack(side="top", anchor="e", pady=(0, 4))
        self.box = RoundFrame(self.frame, fill=WELL, radius=12, pad=6, stretch=True, body_style="Well.TFrame",
                              min_height=120)
        self.box.pack(side="top", fill="both", expand=True)
        self.text = tk.Text(self.box.body, wrap="word", state="disabled", bg=WELL, fg="#c4cad6",
                            relief="flat", borderwidth=0, highlightthickness=0, padx=10, pady=8,
                            font=(MONO_FONT, 9), height=8)
        sb = ttk.Scrollbar(self.box.body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("err", foreground=BAD)
        self.text.tag_configure("ok", foreground=GOOD)

    def append(self, s, tag=None):
        s = self.carry + s.replace("\r\n", "\n")
        self.carry = "\r" if s.endswith("\r") else ""    # a \r at the very end may be half of \r\n
        if self.carry:
            s = s[:-1]
        t = self.text
        at_end = self.autoscroll.get()
        t.configure(state="normal")
        for i, part in enumerate(s.split("\r")):
            if i:
                t.delete("end-1c linestart", "end-1c")   # \r: overwrite the current line
            if part:
                t.insert("end", part, (tag,) if tag else ())
        n = int(t.index("end-1c").split(".")[0])
        if n > self.max_lines:
            t.delete("1.0", f"{n - self.max_lines + 1}.0")
        t.configure(state="disabled")
        if at_end:
            t.see("end")


class LogViewer:
    """A separate window that follows one rip's log file live."""
    def __init__(self, parent, job):
        self.win = tk.Toplevel(parent)
        self.win.title(f"Log: {job.name}")
        self.win.geometry("820x480")
        self.win.configure(bg=BG)
        _dark_titlebar(self.win)
        _set_icon(self.win)
        self.log = LogText(self.win, max_lines=5000)
        self.log.frame.pack(fill="both", expand=True)
        self.path, self.pos, self.alive = job.log_path, 0, True
        self.dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def poll(self):
        if not self.alive:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
            if data:
                self.log.append(self.dec.decode(data))
        except OSError:
            pass
        self.win.after(500, self.poll)

    def close(self):
        self.alive = False
        self.win.destroy()


class JobCard:
    """One active rip: name, progress bar, speed, elapsed time, Log and Cancel buttons."""
    def __init__(self, parent, app, drive, job):
        self.app, self.drive, self.job = app, drive, job
        self.frame = RoundFrame(parent)
        f = self.frame.body
        top = ttk.Frame(f, style="Inner.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text=job.name, style="CardTitle.TLabel").pack(side="left")
        ttk.Button(top, text="Cancel", style="Danger.TButton", command=self.cancel).pack(side="right")
        ttk.Button(top, text="Log", command=lambda: LogViewer(app.root, job)).pack(side="right", padx=6)
        kind = "Blu-ray \u2192 MKV  (MakeMKV)" if job.tool == "makemkv" else "DVD \u2192 MP4  (HandBrake)"
        if getattr(job, "is_tv", False):
            kind += "   \u00b7   TV show (beta)"
        ttk.Label(f, text=f"Drive {drive}   \u00b7   {kind}", style="CardSub.TLabel").pack(anchor="w", pady=(2, 10))
        self.bar = ttk.Progressbar(f, style="Rip.Horizontal.TProgressbar", maximum=100)
        self.bar.pack(fill="x")
        self.info = ttk.Label(f, style="CardSub.TLabel")
        self.info.pack(anchor="w", pady=(8, 0))
        self.update_view()

    def cancel(self):
        if messagebox.askyesno(APP_TITLE, f"Cancel the rip of '{self.job.name}'?\n\n"
                               "The unfinished file is deleted and the disc is ejected."):
            threading.Thread(target=self.job.stop, daemon=True).start()

    def update_view(self):
        j = self.job
        pct = j.overall
        self.bar["value"] = pct
        bits = []
        if getattr(j, "is_tv", False):
            bits.append(j.status_text)
        if j.task and j.task[1] > 1:
            bits.append(f"Pass {j.task[0]} of {j.task[1]}")
        bits.append(f"{pct:.1f}%")
        if j.fps:
            bits.append(f"{j.fps:.0f} fps")
        e = int(time.time() - j.started)
        bits.append(f"{e // 3600}:{e % 3600 // 60:02d}:{e % 60:02d} elapsed")
        self.info.configure(text="   \u00b7   ".join(bits))


# ===== HandBrake preset editor: the form behind the profile tabs in Settings =====
_AUDIO_DEFAULT = {"AudioBitrate": 160, "AudioCompressionLevel": 0.0, "AudioEncoder": "av_aac",
                  "AudioMixdown": "stereo", "AudioNormalizeMixLevel": False, "AudioSamplerate": "auto",
                  "AudioTrackQualityEnable": False, "AudioTrackQuality": -1.0, "AudioTrackGainSlider": 0.0,
                  "AudioTrackDRCSlider": 0.0}
_COPY_CODECS = ("aac", "ac3", "eac3", "dts", "dtshd", "mp3", "truehd", "flac", "opus")
_VIDEO_ENCODERS = {
    "x264": "H.264 (x264)", "x264_10bit": "H.264 10-bit (x264)", "x265": "H.265 (x265)",
    "x265_10bit": "H.265 10-bit (x265)", "x265_12bit": "H.265 12-bit (x265)", "svt_av1": "AV1 (SVT-AV1)",
    "svt_av1_10bit": "AV1 10-bit (SVT-AV1)", "nvenc_h264": "H.264 (NVIDIA NVEnc)",
    "nvenc_h265": "H.265 (NVIDIA NVEnc)", "nvenc_av1": "AV1 (NVIDIA NVEnc)", "qsv_h264": "H.264 (Intel QSV)",
    "qsv_h265": "H.265 (Intel QSV)", "qsv_av1": "AV1 (Intel QSV)", "mpeg4": "MPEG-4", "mpeg2": "MPEG-2",
    "vp8": "VP8", "vp9": "VP9", "theora": "Theora"}
_AUDIO_ENCODERS = {"av_aac": "AAC", "ac3": "AC-3", "eac3": "E-AC-3", "mp3": "MP3", "opus": "Opus",
                   "vorbis": "Vorbis", "flac16": "FLAC 16-bit", "flac24": "FLAC 24-bit"}
_X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
                 "placebo"]
_ONOFF = {"off": "Off"}
_STRENGTH = {"ultralight": "Ultralight", "light": "Light", "medium": "Medium", "strong": "Strong",
             "stronger": "Stronger", "verystrong": "Very strong"}


def _F(label, kind, path, default, options=None, **kw):
    return dict(label=label, kind=kind, path=(path,) if isinstance(path, str) else tuple(path), default=default,
                options=options, **kw)


# (card title, settings). Paths starting with "AudioList" mean the first audio track entry.
PRESET_SECTIONS = [
    ("Output", [
        _F("Preset name", "text", "PresetName", "Rippy"),
        _F("Container", "choice", "FileFormat", "av_mp4",
           {"av_mp4": "MP4", "av_mkv": "MKV (Matroska)", "av_webm": "WebM"}),
        _F("Web optimized (fast start)", "bool", "Mp4HttpOptimize", False),
        _F("Chapter markers", "bool", "ChapterMarkers", True),
        _F("Align A/V start", "bool", "AlignAVStart", False)]),
    ("Video", [
        _F("Encoder", "choice", "VideoEncoder", "x264", _VIDEO_ENCODERS),
        {"kind": "quality"},
        _F("Frame rate", "choice", "VideoFramerateMode", "vfr",
           {"vfr": "Variable (VFR)", "cfr": "Constant (CFR)", "pfr": "Peak (PFR)"}),
        _F("Encoder preset", "combo", "VideoPreset", "medium", _X264_PRESETS),
        _F("Tune", "combo", "VideoTune", "", ["", "film", "animation", "grain", "stillimage", "psnr", "ssim",
                                              "fastdecode", "zerolatency"]),
        _F("Profile", "combo", "VideoProfile", "auto", ["auto", "baseline", "main", "high", "main10"]),
        _F("Level", "combo", "VideoLevel", "auto", ["auto", "1.0", "1.1", "1.2", "1.3", "2.0", "2.1", "2.2", "3.0",
                                                    "3.1", "3.2", "4.0", "4.1", "4.2", "5.0", "5.1", "5.2"]),
        _F("Extra encoder options", "text", "VideoOptionExtra", ""),
        _F("Two-pass encoding (bitrate mode)", "bool", "VideoTwoPass", False),
        _F("Turbo first pass", "bool", "VideoTurboTwoPass", False),
        _F("Grayscale", "bool", "VideoGrayScale", False),
        _F("Hardware decoding (Intel QSV)", "bool", "VideoQSVDecode", False)]),
    ("Audio", [
        _F("Tracks", "choice", "AudioTrackSelectionBehavior", "first",
           {"first": "First track", "all": "All tracks", "none": "No audio"}),
        _F("Languages (e.g. eng, fra)", "list_text", "AudioLanguageList", []),
        _F("Encoder", "choice", ("AudioList", "AudioEncoder"), "av_aac", {**_AUDIO_ENCODERS, "copy": "Auto passthru"}),
        _F("Mixdown", "choice", ("AudioList", "AudioMixdown"), "stereo",
           {"mono": "Mono", "stereo": "Stereo", "dpl1": "Dolby Surround", "dpl2": "Dolby Pro Logic II",
            "5point1": "5.1 channels", "6point1": "6.1 channels", "7point1": "7.1 channels",
            "5_2_lfe": "5.2 (with LFE)"}),
        _F("Bitrate (kbps)", "intcombo", ("AudioList", "AudioBitrate"), 160,
           ["32", "48", "64", "80", "96", "112", "128", "160", "192", "224", "256", "320", "384", "448", "512",
            "640", "768"]),
        _F("Sample rate", "choice", ("AudioList", "AudioSamplerate"), "auto",
           {"auto": "Auto", "8": "8 kHz", "11.025": "11.025 kHz", "12": "12 kHz", "16": "16 kHz",
            "22.05": "22.05 kHz", "24": "24 kHz", "32": "32 kHz", "44.1": "44.1 kHz", "48": "48 kHz"}),
        _F("Passthrough when possible", "mask", "AudioCopyMask", ["copy:aac", "copy:ac3", "copy:dts", "copy:mp3"]),
        _F("Encoder if passthrough isn't possible", "choice", "AudioEncoderFallback", "ac3", _AUDIO_ENCODERS)]),
    ("Picture", [
        _F("Auto-crop black bars", "bool", "PictureAutoCrop", True),
        _F("Keep aspect ratio", "bool", "PictureKeepRatio", True),
        _F("Pixel aspect", "choice", "PicturePAR", "auto",
           {"auto": "Automatic", "none": "None", "loose": "Loose", "custom": "Custom"}),
        _F("Dimension multiple", "choice", "PictureModulus", 2, {2: "2", 4: "4", 8: "8", 16: "16"}),
        _F("Width (0 = from source)", "int", "PictureForceWidth", 0, min=0, max=8192),
        _F("Height (0 = from source)", "int", "PictureForceHeight", 0, min=0, max=8192),
        _F("Deinterlace", "choice", "PictureDeinterlaceFilter", "off",
           {"off": "Off", "decomb": "Decomb", "yadif": "Yadif"}),
        _F("Deinterlace preset", "combo", "PictureDeinterlacePreset", "default",
           ["default", "fast", "bob", "skip-spatial"]),
        _F("Comb detection", "choice", "PictureCombDetectPreset", "default",
           {"off": "Off", "default": "Default", "permissive": "Permissive", "fast": "Fast"}),
        _F("Detelecine", "choice", "PictureDetelecine", "off", {"off": "Off", "default": "Default"}),
        _F("Denoise", "choice", "PictureDenoiseFilter", "off", {"off": "Off", "nlmeans": "NLMeans", "hqdn3d": "HQDN3D"}),
        _F("Denoise strength", "choice", "PictureDenoisePreset", "light", _STRENGTH),
        _F("Sharpen", "choice", "PictureSharpenFilter", "off", {"off": "Off", "unsharp": "Unsharp", "lapsharp": "LapSharp"}),
        _F("Sharpen strength", "choice", "PictureSharpenPreset", "medium", _STRENGTH),
        _F("Deblock", "choice", "PictureDeblockPreset", "off", {**_ONOFF, **_STRENGTH})]),
    ("Subtitles", [
        _F("Tracks", "choice", "SubtitleTrackSelectionBehavior", "none",
           {"none": "None", "first": "First track", "all": "All tracks"}),
        _F("Languages (e.g. eng, fra)", "list_text", "SubtitleLanguageList", []),
        _F("Burn in", "choice", "SubtitleBurnBehavior", "foreign",
           {"none": "Don't burn in", "foreign": "Foreign-audio scenes only", "first": "First track",
            "foreign_first": "Foreign-audio scenes, else first"}),
        _F("Scan for foreign-audio scenes (forced subtitles)", "bool", "SubtitleAddForeignAudioSearch", False),
        _F("Include the foreign-audio subtitle track", "bool", "SubtitleAddForeignAudioSubtitle", False),
        _F("Add closed captions", "bool", "SubtitleAddCC", False),
        _F("Burn in DVD subtitles", "bool", "SubtitleBurnDVDSub", False),
        _F("Burn in Blu-ray subtitles", "bool", "SubtitleBurnBDSub", False)]),
]


def _preset_of(data):
    p = data["PresetList"][0]
    return p["ChildrenArray"][0] if p.get("Folder") else p


class ScrollFrame(ttk.Frame):
    """A vertically scrolling area; put widgets in `.inner`. Scrolls with the mouse wheel."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0, yscrollincrement=20)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))

    def wheel(self, e):
        """Scroll if the pointer is over this area (wired to the window's wheel events)."""
        try:
            if not self.winfo_viewable():
                return
            w = self.winfo_containing(*self.winfo_pointerxy())
        except (tk.TclError, KeyError):
            return
        while w is not None and w is not self:
            w = w.master
        if w is None:
            return
        step = -1 if getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0 else 1
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(step * 3, "units")

    def attach(self, widget):
        """Widgets that use the wheel themselves (dropdowns, sliders) scroll the page instead."""
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(ev, lambda e: (self.wheel(e), "break")[1])


class ProfileForm:
    """Form view of a HandBrake preset: a control for each common setting. Everything the form doesn't show is
    kept exactly as it was, and the text is only regenerated once you actually change something."""

    def __init__(self, parent, win, on_change):
        self.win, self.on_change = win, on_change
        self.scroll = ScrollFrame(parent)
        self.all, self.controls, self.mask_extras = [], [], []
        self.data, self.source, self.dirty, self._loading = None, "", False, True
        self.q_type = tk.StringVar(master=win, value="2")
        self.q_rf = tk.DoubleVar(master=win, value=22.0)
        self.q_kbps = tk.StringVar(master=win, value="0")
        for title, fields in PRESET_SECTIONS:
            card = RoundFrame(self.scroll.inner)
            card.pack(fill="x", pady=(0, 10), padx=(0, 8))
            b = card.body
            b.columnconfigure(1, weight=1)
            ttk.Label(b, text=title, style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
            row = 1
            for spec in fields:
                row = self._add(b, row, dict(spec))
        self._loading = False

    # ----- building -----
    def _track(self, var):
        var.trace_add("write", self._changed)
        return var

    def _ctl(self, widget, wheel=False):
        self.controls.append(widget)
        if wheel:
            self.scroll.attach(widget)
        return widget

    def _label(self, b, row, text):
        ttk.Label(b, text=text, style="CardSub.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 16), pady=4)

    def _add(self, b, row, f):
        kind = f["kind"]
        if kind == "quality":
            return self._add_quality(b, row)
        self.all.append(f)
        if kind == "mask":
            self._label(b, row, f["label"])
            box = ttk.Frame(b, style="Inner.TFrame")
            box.grid(row=row, column=1, sticky="w")
            f["vars"] = {}
            for i, c in enumerate(_COPY_CODECS):
                v = f["vars"][c] = self._track(tk.BooleanVar(master=self.win))
                self._ctl(ttk.Checkbutton(box, text=c.upper(), variable=v, style="Card.TCheckbutton")
                          ).grid(row=i // 5, column=i % 5, sticky="w", padx=(0, 12))
            return row + 1
        f["var"] = var = self._track(tk.BooleanVar(master=self.win) if kind == "bool" else tk.StringVar(master=self.win))
        if kind == "bool":
            self._ctl(ttk.Checkbutton(b, text=f["label"], variable=var, style="Card.TCheckbutton")
                      ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
            return row + 1
        self._label(b, row, f["label"])
        if kind == "choice":
            f["labels"] = dict(f["options"])
            f["widget"] = w = self._ctl(ttk.Combobox(b, textvariable=var, state="readonly", width=34,
                                                     values=list(f["labels"].values())), wheel=True)
            w.grid(row=row, column=1, sticky="w", pady=4)
        elif kind in ("combo", "intcombo"):
            f["widget"] = w = self._ctl(ttk.Combobox(b, textvariable=var, width=34, values=f["options"]), wheel=True)
            w.grid(row=row, column=1, sticky="w", pady=4)
        elif kind == "int":
            self._ctl(ttk.Spinbox(b, from_=f["min"], to=f["max"], textvariable=var, width=10), wheel=True
                      ).grid(row=row, column=1, sticky="w", pady=4)
        else:                                                      # text, list_text
            self._ctl(ttk.Entry(b, textvariable=var)).grid(row=row, column=1, sticky="ew", pady=4)
        if f["path"] == ("VideoEncoder",):
            self.enc_field = f
            var.trace_add("write", lambda *a: self._encoder_changed())
        if f["path"] == ("VideoPreset",):
            self.preset_field = f
        return row + 1

    def _add_quality(self, b, row):
        self._label(b, row, "Quality")
        box = ttk.Frame(b, style="Inner.TFrame")
        box.grid(row=row, column=1, sticky="ew", pady=4)
        box.columnconfigure(0, weight=1)
        for v in (self.q_type, self.q_rf, self.q_kbps):
            self._track(v)
        self.q_type.trace_add("write", lambda *a: self._q_sync())
        top = ttk.Frame(box, style="Inner.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        self._ctl(ttk.Radiobutton(top, text="Constant quality", value="2", variable=self.q_type,
                                  style="Card.TRadiobutton")).pack(side="left")
        self.q_label = ttk.Label(top, text="", style="CardSub.TLabel")
        self.q_label.pack(side="right")
        self.q_scale = self._ctl(ttk.Scale(box, from_=0, to=51, variable=self.q_rf, command=self._rf_moved), wheel=True)
        self.q_scale.grid(row=1, column=0, sticky="ew", pady=(2, 8), padx=(4, 4))
        low = ttk.Frame(box, style="Inner.TFrame")
        low.grid(row=2, column=0, sticky="w")
        self._ctl(ttk.Radiobutton(low, text="Average bitrate", value="1", variable=self.q_type,
                                  style="Card.TRadiobutton")).pack(side="left", padx=(0, 12))
        self.q_spin = self._ctl(ttk.Spinbox(low, from_=0, to=100000, increment=100, textvariable=self.q_kbps, width=10),
                                wheel=True)
        self.q_spin.pack(side="left")
        ttk.Label(low, text="kbps", style="CardSub.TLabel").pack(side="left", padx=8)
        self.all.append({"kind": "quality", "path": ("VideoQualityType",), "default": None})
        return row + 1

    # ----- reacting to changes -----
    def _changed(self, *_):
        if not self._loading:
            self.dirty = True
            self.on_change()

    def _rf_moved(self, v=None):
        val = round(float(self.q_rf.get()) * 4) / 4
        if abs(val - float(self.q_rf.get())) > 1e-9:
            self.q_rf.set(val)
        self.q_label.configure(text=f"RF {val:g}   (lower = better quality, bigger file)")

    def _q_sync(self):
        const = self.q_type.get() == "2"
        self.q_scale.state(["!disabled"] if const else ["disabled"])
        self.q_spin.state(["disabled"] if const else ["!disabled"])

    def _encoder_changed(self):
        enc = self._value(self.enc_field)
        self.q_scale.configure(to=63 if "av1" in str(enc) else 51)
        if str(enc).startswith(("x264", "x265")):
            self.preset_field["widget"].configure(values=_X264_PRESETS)
        elif str(enc).startswith("svt_av1"):
            self.preset_field["widget"].configure(values=[str(i) for i in range(1, 14)])

    # ----- values <-> widgets -----
    def _read(self, p, f):
        path = f["path"]
        try:
            return p["AudioList"][0][path[1]] if path[0] == "AudioList" else p[path[0]]
        except (KeyError, IndexError, TypeError):
            return f["default"]

    def _show(self, f, v):
        kind = f["kind"]
        if kind == "bool":
            f["var"].set(bool(v))
        elif kind == "choice":
            labels = f["labels"]
            if v not in labels:                                     # a value the list doesn't know: keep it as is
                labels[v] = str(v)
                f["widget"].configure(values=list(labels.values()))
            f["var"].set(labels[v])
        elif kind == "list_text":
            f["var"].set(", ".join(str(x) for x in (v or [])))
        elif kind == "mask":
            v = v or []
            for c, var in f["vars"].items():
                var.set(f"copy:{c}" in v)
            self.mask_extras = [x for x in v if x not in {f"copy:{c}" for c in _COPY_CODECS}]
        elif kind == "int":
            f["var"].set(str(int(v or 0)))
        else:                                                       # text, combo, intcombo
            f["var"].set("" if v is None else str(v))

    def _value(self, f):
        kind, var = f["kind"], f.get("var")
        if kind == "bool":
            return bool(var.get())
        if kind == "choice":
            text = var.get()
            return next((k for k, lab in f["labels"].items() if lab == text), f["default"])
        if kind == "list_text":
            return [x.strip() for x in var.get().split(",") if x.strip()]
        if kind == "mask":
            return [f"copy:{c}" for c, v in f["vars"].items() if v.get()] + self.mask_extras
        if kind in ("int", "intcombo"):
            try:
                n = int(float(var.get()))
            except ValueError:
                return f["default"]
            return max(f["min"], min(f["max"], n)) if kind == "int" else n
        return var.get()

    # ----- loading / saving -----
    def _template(self):
        p = {"PresetName": "Rippy", "Type": 1, "Default": False, "Folder": False, "FolderOpen": False,
             "ChildrenArray": [], "UsesPictureSettings": 2, "UsesPictureFilters": True,
             "VideoQualityType": 2, "VideoQualitySlider": 22.0, "VideoAvgBitrate": 0}
        data = {"PresetList": [p], "VersionMajor": 42, "VersionMinor": 0, "VersionMicro": 0}
        self._apply(p, force=True, defaults=True)
        return data

    def _set_data(self, data, source):
        self._loading = True
        self.data, self.source = data, source
        p = _preset_of(data)
        for f in self.all:
            if f["kind"] != "quality":
                self._show(f, self._read(p, f))
        t = p.get("VideoQualityType", 2)
        self.q_type.set(str(t) if t in (1, 2) else "2")
        self.q_rf.set(float(p.get("VideoQualitySlider", 22.0) or 0))
        self.q_kbps.set(str(int(p.get("VideoAvgBitrate", 0) or 0)))
        self._encoder_changed()
        self._rf_moved()
        self._q_sync()
        self.dirty, self._loading = False, False

    def load(self, text):
        self._set_data(json.loads(text), text)

    def load_template(self):
        self._set_data(self._template(), "")

    def _apply(self, p, force=False, defaults=False):
        for f in self.all:
            if f["kind"] == "quality":
                continue
            val = f["default"] if defaults else self._value(f)
            path = f["path"]
            box = p
            if path[0] == "AudioList":
                if not p.get("AudioList"):
                    p["AudioList"] = [dict(_AUDIO_DEFAULT)]
                box = p["AudioList"][0]
            key = path[-1]
            if force or key in box or val != f["default"]:
                box[key] = val
        if not defaults:
            p["VideoQualityType"] = int(self.q_type.get())
            p["VideoQualitySlider"] = float(round(float(self.q_rf.get()) * 4) / 4)
            try:
                p["VideoAvgBitrate"] = int(float(self.q_kbps.get()))
            except ValueError:
                pass

    def text(self):
        """The preset as JSON text: exactly what was loaded until you change something."""
        if not self.dirty or self.data is None:
            return self.source
        import copy
        data = copy.deepcopy(self.data)
        self._apply(_preset_of(data))
        return json.dumps(data, indent=2)

    def set_enabled(self, on):
        for w in self.controls:
            w.state(["!disabled"] if on else ["disabled"])
        if on:
            self._q_sync()


class SettingsWindow:
    """NAS login and HandBrake profiles. Saved to Rippy's own settings.json; the nas.json and profile
    files next to the app are only read (as the starting values), never written."""

    def __init__(self, app):
        from tkinter import filedialog
        self.app, self.filedialog = app, filedialog
        k = app.k
        self.win = win = tk.Toplevel(app.root)
        win.title(f"{APP_TITLE} — Settings")
        win.configure(bg=BG)
        win.geometry(f"{int(780 * k)}x{min(int(760 * k), max(app.root.winfo_screenheight() - 100, int(520 * k)))}")
        win.minsize(int(620 * k), int(520 * k))
        win.transient(app.root)
        _dark_titlebar(win)
        _set_icon(win)
        self.settings = load_settings()
        outer = ttk.Frame(win, padding=(22, 18))
        outer.pack(fill="both", expand=True)
        bar = ttk.Frame(outer)
        bar.pack(side="bottom", fill="x", pady=(14, 0))
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.close).pack(side="right", padx=(0, 10))
        ttk.Label(bar, text="Saved in Rippy's own settings", style="Muted.TLabel").pack(side="left")
        self.nb = nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        self._build_nas(nb)
        self.movie = self._profile_tab(nb, "movie", "Movie profile")
        self.tv = self._profile_tab(nb, "tv", "TV profile")
        def wheel(e):
            for st in (self.movie, self.tv):
                st["form"].scroll.wheel(e)
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(ev, wheel, add="+")
        win.bind("<Escape>", lambda e: self.close())
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.focus_set()

    def close(self):
        self.win.destroy()

    # ----- NAS -----
    def _build_nas(self, nb):
        tab = ttk.Frame(nb, padding=(0, 18, 0, 0))
        nb.add(tab, text="NAS upload")
        saved = self.settings.get("nas")
        if isinstance(saved, dict):
            base = dict(saved)
        else:                                          # start from nas.json (read only), if there is one
            base = {}
            try:
                data = json.loads(NAS_CONFIG_FILE.read_text(encoding="utf-8"))
                base = dict(data) if isinstance(data, dict) else {}
            except Exception:
                pass
            base["enabled"] = bool(base.get("host") and base.get("share"))
        mk = lambda key: tk.StringVar(master=self.win, value=str(base.get(key) or ""))
        self.nas_on = tk.BooleanVar(master=self.win, value=bool(base.get("enabled", True)))
        self.nas_vars = {key: mk(key) for key in ("host", "share", "folder", "username", "password")}
        self.nas_initial = self._nas_dict()
        card = RoundFrame(tab)
        card.pack(fill="x")
        b = card.body
        ttk.Checkbutton(b, text="Upload finished rips to a NAS (SMB share)", variable=self.nas_on,
                        style="Card.TCheckbutton").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        rows = (("Host or IP", "host", ""), ("Share", "share", ""), ("Folder inside the share", "folder", ""),
                ("Username", "username", ""), ("Password", "password", "•"))
        for i, (label, key, mask) in enumerate(rows, start=1):
            ttk.Label(b, text=label, style="CardSub.TLabel").grid(row=i, column=0, sticky="w", padx=(0, 16), pady=5)
            e = ttk.Entry(b, textvariable=self.nas_vars[key], show=mask, width=38)
            e.grid(row=i, column=1, sticky="ew", pady=5)
            if key == "password":
                self.pw_entry = e
        b.columnconfigure(1, weight=1)
        self.show_pw = tk.BooleanVar(master=self.win, value=False)
        ttk.Checkbutton(b, text="Show password", variable=self.show_pw, style="Card.TCheckbutton",
                        command=lambda: self.pw_entry.configure(show="" if self.show_pw.get() else "•")
                        ).grid(row=len(rows) + 1, column=1, sticky="w", pady=(2, 0))
        test = ttk.Frame(b, style="Inner.TFrame")
        test.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(test, text="Test connection", command=self._test_nas).pack(side="left")
        self.nas_result = ttk.Label(test, text="", style="CardSub.TLabel", wraplength=int(430 * self.app.k))
        self.nas_result.pack(side="left", padx=14)
        note = ("Example: host 192.168.1.101, share “Received Video”. Leave the folder empty to use the share's top level. "
                "Your nas.json is never changed.")
        ttk.Label(tab, text=note, style="Muted.TLabel", wraplength=int(620 * self.app.k), justify="left"
                  ).pack(anchor="w", pady=(12, 0))

    def _nas_dict(self):
        d = {k: v.get().strip() for k, v in self.nas_vars.items()}
        d["password"] = self.nas_vars["password"].get()          # a password may legitimately start/end with a space
        d["enabled"] = bool(self.nas_on.get())
        return d

    def _test_nas(self):
        d = self._nas_dict()
        if not (d["host"] and d["share"]):
            self.nas_result.configure(text="Enter a host and a share first.", foreground=BAD)
            return
        self.nas_result.configure(text="Testing…", foreground=MUTED)
        box = []
        threading.Thread(target=lambda: box.append(nas_check(d)), daemon=True).start()

        def poll():
            try:
                if not box:
                    self.win.after(200, poll)
                    return
                ok, msg = box[0]
                self.nas_result.configure(text=msg, foreground=GOOD if ok else BAD)
            except tk.TclError:                                    # window closed meanwhile
                pass
        self.win.after(200, poll)

    # ----- profiles -----
    @staticmethod
    def _baseline_file(kind):
        """The profile file next to the app that applies when nothing is saved in Settings (or None)."""
        f = PRESET_FILE if kind == "movie" else (TV_PRESET_FILE if TV_PRESET_FILE != PRESET_FILE else None)
        return f if f and f.exists() else None

    def _baseline_text(self, kind):
        f = self._baseline_file(kind)
        try:
            return f.read_text(encoding="utf-8-sig").strip() if f else ""
        except OSError:
            return ""

    def _profile_tab(self, nb, kind, title):
        tab = ttk.Frame(nb, padding=(0, 18, 0, 0))
        nb.add(tab, text=title)
        wrap = int(620 * self.app.k)
        prof = self.settings.get("profiles") or {}
        saved = prof.get(kind) if isinstance(prof.get(kind), str) and prof.get(kind).strip() else None
        baseline_file = self._baseline_file(kind)
        text = (saved or self._baseline_text(kind)).strip()
        if saved:
            source = "Saved in Rippy's settings."
        elif baseline_file:
            source = f"Using {baseline_file.name} next to the app (it stays untouched; saving here keeps a separate copy)."
        elif kind == "tv":
            source = "No TV profile set."
        else:
            source = f"No profile set; HandBrake's built-in “{FALLBACK_PRESET}” is used until you change something here."
        st = {"kind": kind, "initial": text, "buttons": [], "mode": "form"}
        if kind == "tv":
            use = prof.get("tv_use_movie")
            if use is None:
                use = not saved and not baseline_file
            st["use_movie"], st["use_initial"] = tk.BooleanVar(master=self.win, value=bool(use)), bool(use)
            ttk.Checkbutton(tab, text="Use the movie profile for TV shows", variable=st["use_movie"],
                            command=lambda: self._sync_tv(st)).pack(anchor="w", pady=(0, 10))
        ttk.Label(tab, text=source, style="Muted.TLabel", wraplength=wrap, justify="left").pack(anchor="w")
        st["summary"] = ttk.Label(tab, text="", style="Summary.TLabel", wraplength=wrap, justify="left")
        st["summary"].pack(anchor="w", pady=(6, 10))
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 10))
        for label, cmd in (("Import file…", lambda: self._import(st)), ("Reset to default", lambda: self._reset(st))):
            b = ttk.Button(row, text=label, command=cmd)
            b.pack(side="left", padx=(0, 10))
            st["buttons"].append(b)
        st["toggle"] = ttk.Button(row, text="Edit raw JSON", command=lambda: self._toggle_view(st))
        st["toggle"].pack(side="right")
        st["buttons"].append(st["toggle"])
        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True)
        st["form"] = ProfileForm(holder, self.win, lambda: self._summarize(st))
        st["form"].scroll.pack(fill="both", expand=True)
        card = st["raw_card"] = RoundFrame(holder, fill=WELL, radius=12, pad=6, stretch=True, body_style="Well.TFrame",
                                           min_height=220)
        raw = st["raw"] = tk.Text(card.body, wrap="none", bg=WELL, fg="#c4cad6", insertbackground=FG, relief="flat",
                                  bd=0, highlightthickness=0, padx=8, pady=6, font=(MONO_FONT, 9), undo=True)
        sb = ttk.Scrollbar(card.body, orient="vertical", command=raw.yview)
        raw.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        raw.pack(side="left", fill="both", expand=True)
        raw.bind("<KeyRelease>", lambda e: self._summarize(st))
        raw.insert("1.0", text)
        try:
            if text:
                parse_profile(text)
                st["form"].load(text)
            else:
                st["form"].load_template()
        except ValueError:                              # a profile the form can't read: show it as text to fix
            self._to_raw(st, refill=False)
        self._summarize(st)
        if kind == "tv":
            self._sync_tv(st)
        return st

    def _raw_text(self, st):
        return st["raw"].get("1.0", "end").strip()

    def _get_text(self, st):
        return st["form"].text().strip() if st["mode"] == "form" else self._raw_text(st)

    def _fill_raw(self, st, text):
        st["raw"].configure(state="normal")
        st["raw"].delete("1.0", "end")
        st["raw"].insert("1.0", text)

    def _to_raw(self, st, refill=True):
        if st["mode"] == "raw":
            return
        if refill:                                       # (not when the raw box already holds text the form couldn't read)
            self._fill_raw(st, st["form"].text())
        st["form"].scroll.pack_forget()
        st["raw_card"].pack(fill="both", expand=True)
        st["toggle"].configure(text="Back to the form")
        st["mode"] = "raw"

    def _to_form(self, st):
        if st["mode"] == "form":
            return
        text = self._raw_text(st)
        if text:
            try:
                parse_profile(text)
                st["form"].load(text)
            except ValueError as e:
                messagebox.showerror(APP_TITLE, f"Fix the JSON first. {e}", parent=self.win)
                return
        else:
            st["form"].load_template()
        st["raw_card"].pack_forget()
        st["form"].scroll.pack(fill="both", expand=True)
        st["toggle"].configure(text="Edit raw JSON")
        st["mode"] = "form"
        self._summarize(st)

    def _toggle_view(self, st):
        (self._to_form if st["mode"] == "raw" else self._to_raw)(st)

    def _summarize(self, st):
        text = self._get_text(st)
        if not text:
            st["summary"].configure(text="Empty — the default is used.", foreground=MUTED)
            return
        try:
            st["summary"].configure(text=profile_summary(parse_profile(text)), foreground=GOOD)
        except ValueError:
            st["summary"].configure(text="Not a valid HandBrake preset (yet).", foreground=BAD)

    def _set_text(self, st, text):
        self._fill_raw(st, text)
        if st["mode"] == "form":
            try:
                if text:
                    parse_profile(text)
                    st["form"].load(text)
                else:
                    st["form"].load_template()
            except ValueError:
                self._to_raw(st, refill=False)
        self._summarize(st)
        if st["kind"] == "tv":
            self._sync_tv(st)

    def _import(self, st):
        path = self.filedialog.askopenfilename(
            parent=self.win, title="Choose an exported HandBrake preset",
            filetypes=[("HandBrake preset", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
            parse_profile(text)
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_TITLE, str(e), parent=self.win)
            return
        self._set_text(st, text.strip())

    def _reset(self, st):
        self._set_text(st, self._baseline_text(st["kind"]))

    def _sync_tv(self, st):
        off = st["use_movie"].get()
        st["form"].set_enabled(not off)
        st["raw"].configure(state="disabled" if off else "normal")
        for b in st["buttons"]:
            b.configure(state="disabled" if off else "normal")

    # ----- save -----
    def save(self):
        s = load_settings()                                         # re-read, so nothing saved elsewhere is lost
        nd = self._nas_dict()
        nas_changed = nd != self.nas_initial
        if nas_changed:
            if nd["enabled"] and not (nd["host"] and nd["share"]):
                self.nb.select(0)
                messagebox.showerror(APP_TITLE, "Enter at least a host and a share, or untick "
                                     "“Upload finished rips to a NAS”.", parent=self.win)
                return
            s["nas"] = nd
        prof = dict(s.get("profiles") or {})
        for st in (self.movie, self.tv):
            kind = st["kind"]
            if kind == "tv":
                use = bool(st["use_movie"].get())
                if use != st["use_initial"]:
                    prof["tv_use_movie"] = use
                if use:
                    continue
            text = self._get_text(st)
            if not text or text == self._baseline_text(kind):
                prof.pop(kind, None)                                # back to the default / the file next to the app
            elif text != st["initial"]:
                try:
                    parse_profile(text)
                except ValueError as e:
                    self.nb.select(1 if kind == "movie" else 2)
                    messagebox.showerror(APP_TITLE, f"{'Movie' if kind == 'movie' else 'TV'} profile: {e}",
                                         parent=self.win)
                    return
                prof[kind] = text + "\n"
        if prof:
            s["profiles"] = prof
        else:
            s.pop("profiles", None)
        try:
            save_settings(s)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Couldn't save the settings: {e}", parent=self.win)
            return
        print("Settings saved. Profile changes apply from the next rip.")
        if nas_changed:
            reload_nas()
            cfg = nas_config()
            if cfg:
                print(f"NAS upload on: \\\\{cfg['host']}\\{cfg['share']}\\{cfg.get('folder', '')}")
                upload_async()
            else:
                print("NAS upload is off.")
        self.close()


class RipperGUI:
    """The main window. Minimizes to the taskbar like any normal Windows app.

    Runs on the main thread; the disc watcher runs in a worker thread and talks to the
    window through queues (log text, and prompts that wait for your answer).
    """
    def __init__(self):
        if OS == "Windows":
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on high-DPI screens
            except Exception:
                pass
        if OS == "Windows":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Rippy.DVDRipper")
            except Exception:
                pass
        self.root = root = tk.Tk()
        root.title(APP_TITLE)
        k = max(1.0, root.winfo_fpixels("1i") / 96)          # display scaling (e.g. 1.5 at 150%)
        self.k = k
        root.geometry(f"{int(940 * k)}x{min(int(800 * k), max(root.winfo_screenheight() - 90, int(540 * k)))}")
        root.minsize(int(760 * k), int(540 * k))
        root.configure(bg=BG)
        self.logq, self.requests = queue.Queue(), queue.Queue()
        self.writer = _QueueWriter(self.logq)
        self.active, self.cards = {}, {}
        self.cur, self.deadline, self.entry, self.countdown = None, 0, None, None
        self.quitting, self.closing_since, self._chips = False, 0, None
        self.stop_event, self.done, self.worker = threading.Event(), threading.Event(), None
        self._style()
        self._build()
        root.bind("<Return>", self._on_return)
        root.bind_all("<Key>", self._touch, add="+")          # typing/clicking resets the prompt timer
        root.bind_all("<Button-1>", self._touch, add="+")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----- look -----
    def _style(self):
        global UI_FONT, MONO_FONT
        import tkinter.font as tkfont
        have = {f.lower(): f for f in tkfont.families(self.root)}
        UI_FONT = next((have[n.lower()] for n in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Inter",
                                                   "Helvetica Neue", "DejaVu Sans") if n.lower() in have), UI_FONT)
        MONO_FONT = next((have[n.lower()] for n in ("Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas",
                                                     "SF Mono", "Menlo", "DejaVu Sans Mono") if n.lower() in have), MONO_FONT)
        bold = lambda size: (UI_FONT, size, "bold")
        _dark_titlebar(self.root)
        _set_icon(self.root)
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, font=(UI_FONT, 10), bordercolor=BORDER, focuscolor=BG)
        # cards: flat surface with a 1px outline
        s.configure("Card.TFrame", background=PANEL, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    borderwidth=1, relief="solid")
        s.configure("Prompt.TFrame", background=PROMPT_BG, bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    borderwidth=1, relief="solid")
        s.configure("Inner.TFrame", background=PANEL)
        s.configure("Well.TFrame", background=WELL)
        s.configure("PromptInner.TFrame", background=PROMPT_BG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("H1.TLabel", font=bold(22))
        s.configure("H2.TLabel", font=bold(8), foreground=MUTED)
        s.configure("CardTitle.TLabel", background=PANEL, font=bold(12))
        s.configure("CardSub.TLabel", background=PANEL, foreground=MUTED)
        s.configure("PromptTitle.TLabel", background=PROMPT_BG, font=bold(12))
        s.configure("PromptSub.TLabel", background=PROMPT_BG, foreground="#a9a3d8")
        s.configure("Chip.TLabel", background=PANEL, padding=(0, 6))
        s.configure("ChipState.TLabel", background=PANEL, foreground=MUTED, padding=(0, 6))
        s.configure("TCheckbutton", background=BG, foreground=MUTED, focuscolor=BG, indicatorbackground=WELL,
                    indicatorforeground="white", upperbordercolor=BORDER, lowerbordercolor=BORDER, padding=4)
        s.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)],
              indicatorbackground=[("selected", ACCENT), ("active", PANEL2)],
              upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
        # buttons: flat, soft hover
        s.configure("TButton", background=PANEL2, foreground=FG, borderwidth=1, bordercolor=PANEL2,
                    lightcolor=PANEL2, darkcolor=PANEL2, padding=(14, 7), relief="flat")
        s.map("TButton", background=[("disabled", PANEL), ("active", "#2b303c")],
              bordercolor=[("active", "#3a4151"), ("disabled", PANEL)],
              lightcolor=[("active", "#2b303c")], darkcolor=[("active", "#2b303c")],
              foreground=[("disabled", MUTED)])
        s.configure("Eject.TButton", padding=(10, 3), font=(UI_FONT, 9), width=0)
        s.configure("Card.TCheckbutton", background=PANEL, foreground=FG, focuscolor=PANEL, indicatorbackground=WELL,
                    indicatorforeground="white", upperbordercolor=BORDER, lowerbordercolor=BORDER, padding=4)
        s.map("Card.TCheckbutton", background=[("active", PANEL)], foreground=[("disabled", MUTED)],
              indicatorbackground=[("selected", ACCENT), ("active", PANEL2)],
              upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
        s.configure("Summary.TLabel", font=bold(10))
        s.configure("Card.TRadiobutton", background=PANEL, foreground=FG, focuscolor=PANEL, indicatorbackground=WELL,
                    indicatorforeground="white", upperbordercolor=BORDER, lowerbordercolor=BORDER, padding=4)
        s.map("Card.TRadiobutton", background=[("active", PANEL)], foreground=[("disabled", MUTED)],
              indicatorbackground=[("selected", ACCENT), ("active", PANEL2)],
              upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
        for w in ("TCombobox", "TSpinbox"):
            s.configure(w, fieldbackground=WELL, background=PANEL2, foreground=FG, arrowcolor=MUTED, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, insertcolor=FG, padding=5, arrowsize=14)
            s.map(w, fieldbackground=[("readonly", WELL), ("disabled", PANEL)], foreground=[("disabled", MUTED)],
                  background=[("active", "#2b303c")], bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)], selectbackground=[("readonly", WELL)], selectforeground=[("readonly", FG)])
        s.configure("Horizontal.TScale", background=ACCENT, troughcolor=WELL, bordercolor=WELL, lightcolor=ACCENT,
                    darkcolor=ACCENT, sliderlength=20, gripcount=0)
        s.map("Horizontal.TScale", background=[("disabled", PANEL2), ("active", ACCENT_HI)])
        for opt, val in (("background", PANEL2), ("foreground", FG), ("selectBackground", ACCENT),
                         ("selectForeground", "white"), ("font", (UI_FONT, 10))):
            self.root.option_add(f"*TCombobox*Listbox.{opt}", val)
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0), bordercolor=BG,
                    lightcolor=BG, darkcolor=BG)
        s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(18, 9), borderwidth=0,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG, font=bold(10))
        s.map("TNotebook.Tab", background=[("selected", PANEL2)], foreground=[("selected", FG), ("active", FG)],
              lightcolor=[("selected", PANEL2)], darkcolor=[("selected", PANEL2)], bordercolor=[("selected", PANEL2)])
        s.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, font=bold(10))
        s.map("Accent.TButton", background=[("active", ACCENT_HI)], bordercolor=[("active", ACCENT_HI)],
              lightcolor=[("active", ACCENT_HI)], darkcolor=[("active", ACCENT_HI)])
        s.configure("Danger.TButton", foreground=BAD)
        s.map("Danger.TButton", background=[("active", "#2c2029"), ("disabled", PANEL)],
              bordercolor=[("active", "#5a2f3a"), ("disabled", PANEL)],
              lightcolor=[("active", "#2c2029")], darkcolor=[("active", "#2c2029")],
              foreground=[("disabled", MUTED)])
        for name, color in (("Rip", ACCENT), ("Up", GOOD)):
            s.configure(f"{name}.Horizontal.TProgressbar", troughcolor=WELL, background=color,
                        bordercolor=WELL, lightcolor=color, darkcolor=color, thickness=6)
        s.configure("TEntry", fieldbackground=WELL, foreground=FG, insertcolor=FG, padding=7,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)])
        s.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=WELL, bordercolor=WELL,
                    lightcolor=PANEL2, darkcolor=PANEL2, arrowcolor=WELL, gripcount=0, width=10)
        s.map("Vertical.TScrollbar", background=[("active", "#4a5062")])

    def _build(self):
        outer = ttk.Frame(self.root, padding=(26, 22))
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer)
        head.pack(fill="x")
        left = ttk.Frame(head)
        left.pack(side="left")
        title = ttk.Frame(left)
        title.pack(anchor="w")
        tk.Label(title, text="\u25cf", bg=BG, fg=ACCENT, font=(UI_FONT, 14)).pack(side="left", padx=(0, 8))
        ttk.Label(title, text=APP_TITLE, style="H1.TLabel").pack(side="left")
        self.subtitle = ttk.Label(left, text="Starting\u2026", style="Muted.TLabel")
        self.subtitle.pack(anchor="w", pady=(2, 0))
        self.stop_btn = ttk.Button(head, text="Stop all rips", style="Danger.TButton", command=self.stop_all,
                                   state="disabled")
        self.stop_btn.pack(side="right")
        ttk.Button(head, text="⚙  Settings", command=self.open_settings).pack(side="right", padx=(0, 10))
        self.drives_frame = ttk.Frame(outer)
        self.drives_frame.pack(fill="x", pady=(16, 0))
        self.prompt = RoundFrame(outer, fill=PROMPT_BG, outline=ACCENT, body_style="PromptInner.TFrame")   # shown only when asking something
        ttk.Label(outer, text="RIPS", style="H2.TLabel").pack(anchor="w", pady=(18, 6))
        self.jobs_frame = ttk.Frame(outer)
        self.jobs_frame.pack(fill="x")
        self.empty = ttk.Label(self.jobs_frame, text="No active rips. Insert a disc to begin.", style="Muted.TLabel")
        self.empty.pack(anchor="w", pady=6)
        self.up = RoundFrame(outer)                                           # NAS upload panel (shown on demand)
        upb = self.up.body
        uptop = ttk.Frame(upb, style="Inner.TFrame")
        uptop.pack(fill="x")
        self.up_title = ttk.Label(uptop, style="CardTitle.TLabel")
        self.up_title.pack(side="left")
        self.up_retry = ttk.Button(uptop, text="Retry now", command=upload_async)
        self.up_file = ttk.Label(upb, style="CardSub.TLabel")
        self.up_file.pack(anchor="w", pady=(2, 10))
        self.up_bar = ttk.Progressbar(upb, style="Up.Horizontal.TProgressbar", maximum=100)
        self.up_info = ttk.Label(upb, style="CardSub.TLabel", wraplength=820, justify="left")
        self.up_info.pack(anchor="w", pady=(8, 0))
        ttk.Label(outer, text="ACTIVITY", style="H2.TLabel").pack(anchor="w", pady=(18, 6))
        self.log = LogText(outer)
        self.log.frame.pack(fill="both", expand=True)

    # ----- prompts (called from the watcher thread) -----
    def ask(self, prompt, timeout, options, default, heading, initial):
        if self.quitting:
            raise UserQuit()
        req = {"heading": heading or prompt.strip().rstrip(":").strip(), "timeout": timeout,
               "options": options, "default": default, "initial": initial,
               "ev": threading.Event(), "result": None}
        self.requests.put(req)
        req["ev"].wait()
        if self.quitting:
            raise UserQuit()
        return req["result"]

    def _show_prompt(self, req):
        self.cur = req
        pb = self.prompt.body
        for w in pb.winfo_children():
            w.destroy()
        ttk.Label(pb, text=req["heading"], style="PromptTitle.TLabel",
                  wraplength=820, justify="left").pack(anchor="w")
        body = ttk.Frame(pb, style="PromptInner.TFrame")
        body.pack(fill="x", pady=(12, 8))
        opts = req["options"]
        if opts:
            for i, (label, value) in enumerate(opts):
                is_default = (value == req["default"]) if req["default"] is not None else i == 0
                b = ttk.Button(body, text=label, style="Accent.TButton" if is_default else "TButton",
                               command=lambda v=value: self._answer(v))
                if len(opts) <= 2:
                    b.pack(side="left", padx=(0, 10), ipadx=12, ipady=3)
                else:
                    b.pack(fill="x", pady=2)
        else:
            self.entry = ttk.Entry(body, width=50)
            self.entry.insert(0, req["initial"])
            self.entry.pack(side="left", fill="x", expand=True)
            ttk.Button(body, text="OK", style="Accent.TButton",
                       command=lambda: self._answer(self.entry.get())).pack(side="left", padx=(10, 0))
            self.entry.focus_set()
            self.entry.select_range(0, "end")
        self.countdown = ttk.Label(pb, style="PromptSub.TLabel")
        self.countdown.pack(anchor="w")
        self.prompt.pack(fill="x", pady=(14, 0), after=self.drives_frame)
        self.deadline = time.time() + req["timeout"]
        print(f"? {req['heading']}")
        self._flash()

    def _answer(self, value):
        req = self.cur
        if not req:
            return
        self.cur = None
        self.prompt.pack_forget()
        req["result"] = value
        shown = ("(no answer; choosing automatically)" if value is None
                 else next((l for l, v in (req["options"] or []) if v == value), value))
        print(f"   \u2192 {shown}")
        req["ev"].set()

    def _on_return(self, _e=None):
        req = self.cur
        if not req:
            return
        if req["options"]:
            self._answer(req["default"] if req["default"] is not None else req["options"][0][1])
        else:
            self._answer(self.entry.get())

    def _touch(self, _e=None):
        if self.cur:
            self.deadline = time.time() + self.cur["timeout"]

    def _flash(self):
        """Flash the taskbar button so a minimized window can get your attention."""
        if OS != "Windows":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                            ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x0F, 0, 0)   # FLASHW_ALL | FLASHW_TIMERNOFG
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    # ----- buttons -----
    def open_settings(self):
        w = getattr(self, "_settings", None)
        if w is not None and w.win.winfo_exists():
            w.win.lift()
            w.win.focus_force()
            return
        self._settings = SettingsWindow(self)

    def stop_all(self):
        jobs = list(self.active.values())
        if jobs and messagebox.askyesno(APP_TITLE, f"Stop {len(jobs)} rip(s)?\n\n"
                                        "Unfinished files are deleted and the discs are ejected."):
            for j in jobs:
                threading.Thread(target=j.stop, daemon=True).start()

    def manual_eject(self, drive):
        """Eject button next to a drive's name. A drive that is ripping asks first (cancelling the rip)."""
        job = self.active.get(drive)
        if job:
            if not messagebox.askyesno(APP_TITLE, f"Drive {drive} is ripping '{job.name}'.\n\n"
                                       "Cancel the rip and eject? The unfinished file is deleted."):
                return
            threading.Thread(target=job.stop, daemon=True).start()   # the watcher ejects once the rip has stopped
            return
        print(f"[{drive}] manual eject.")
        threading.Thread(target=safe_eject, args=(drive,), daemon=True).start()

    def on_close(self):
        if self.quitting:
            return
        n = len(self.active)
        if n and not messagebox.askyesno(APP_TITLE, f"{n} rip(s) are still running.\n\n"
                                         "Quit and cancel them? Unfinished files are deleted."):
            return
        self.quitting, self.closing_since = True, time.time()
        self.stop_event.set()
        self.root.title(f"{APP_TITLE} \u2014 stopping\u2026")
        if self.cur:
            self._answer(None)                      # releases the watcher (it then raises UserQuit)
        if self.worker is not None and not self.worker.is_alive():      # watcher already died
            threading.Thread(target=lambda: (shutdown(self.active), self.done.set()), daemon=True).start()

    # ----- periodic updates (main thread) -----
    def _pump(self):
        chunks = []
        try:
            while True:
                chunks.append(self.logq.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            for line in re.findall(r"[^\n]*\n|[^\n]+", "".join(chunks)):
                self.log.append(line, _tag_for(line))
        if self.cur is None:
            try:
                self._show_prompt(self.requests.get_nowait())
            except queue.Empty:
                pass
        if self.cur:
            left = self.deadline - time.time()
            if left <= 0:
                self._answer(None)
            else:
                self.countdown.configure(text=f"Choosing automatically in {int(left) + 1}s if you don't answer.")
        if self.quitting and (self.done.is_set() or time.time() - self.closing_since > 25):
            self.root.destroy()
            return
        self.root.after(100, self._pump)

    def _refresh(self):
        try:
            jobs, drives = list(self.active.items()), dict(drive_state)
        except RuntimeError:                        # watcher changed them mid-copy; try next tick
            self.root.after(500, self._refresh)
            return
        seen = set()
        for drive, job in jobs:
            seen.add(drive)
            card = self.cards.get(drive)
            if card is None or card.job is not job:
                if card:
                    card.frame.destroy()
                card = self.cards[drive] = JobCard(self.jobs_frame, self, drive, job)
                card.frame.pack(fill="x", pady=(0, 8))
            card.update_view()
        for drive in [d for d in self.cards if d not in seen]:
            self.cards.pop(drive).frame.destroy()
        if self.cards:
            self.empty.pack_forget()
        elif not self.empty.winfo_ismapped():
            self.empty.pack(anchor="w", pady=6)
        if drives != self._chips:                   # redraw drive chips only when something changed
            self._chips = drives
            for w in self.drives_frame.winfo_children():
                w.destroy()
            for d, state in drives.items():
                dot = ACCENT if state.startswith("Ripping") else GOOD if state.startswith("Disc") else MUTED
                card = RoundFrame(self.drives_frame, radius=12, pad=7, fit_width=True)
                card.pack(side="left", padx=(0, 10))
                chip = card.body
                tk.Label(chip, text="\u25cf", bg=PANEL, fg=dot, font=(UI_FONT, 9)).pack(side="left", padx=(6, 8))
                ttk.Label(chip, text=d, style="Chip.TLabel", font=(UI_FONT, 10, "bold")).pack(side="left")
                ttk.Label(chip, text=f"   {state}   ", style="ChipState.TLabel").pack(side="left")
                ttk.Button(chip, text="\u23cf Eject", style="Eject.TButton",
                           command=lambda d=d: self.manual_eject(d)).pack(side="left", pady=1)
        self._refresh_upload()
        self.subtitle.configure(text=(f"Watching {len(drives)} drive{'s' if len(drives) != 1 else ''}"
                                      if drives else "Waiting for an optical drive\u2026"))
        self.stop_btn.configure(state="normal" if self.cards else "disabled")
        if not self.quitting:
            if self.cards:
                avg = sum(c.job.overall for c in self.cards.values()) / len(self.cards)
                self.root.title(f"{APP_TITLE} \u2014 {len(self.cards)} ripping \u00b7 {avg:.0f}%")   # shows on the taskbar
            else:
                self.root.title(APP_TITLE)
        self.root.after(500, self._refresh)

    def _refresh_upload(self):
        try:
            st = dict(upload_state)
        except RuntimeError:
            return
        if st.get("file"):
            size, done = max(st.get("size", 0), 1), st.get("done", 0)
            el = max(time.time() - st.get("started", time.time()), 0.001)
            self.up_title.configure(text="Uploading to NAS" + (f"  ({st['index']} of {st['total']})" if st.get("total", 1) > 1 else ""))
            self.up_file.configure(text=st["file"])
            self.up_bar.configure(style="Up.Horizontal.TProgressbar", value=done * 100 / size)
            self.up_info.configure(text=f"{done * 100 / size:.0f}%   \u00b7   {done / 1e6:,.0f} of {size / 1e6:,.0f} MB"
                                        f"   \u00b7   {done / el / 1e6:.1f} MB/s")
            idle = time.time() - st.get("last", time.time())
            if idle > 10:                              # slow is fine; silent for a while is worth saying
                self.up_info.configure(text=self.up_info.cget("text") + f"   \u00b7   no data for {int(idle)}s")
            if st.get("attempt", 1) > 1:
                self.up_title.configure(text=self.up_title.cget("text") + f"   \u2014 retry {st['attempt']}")
            self.up_retry.pack_forget()
            self.up_bar.pack(fill="x", before=self.up_info)
        elif st.get("error"):
            self.up_title.configure(text="NAS upload failed")
            self.up_file.configure(text="Files stay on this PC and are retried when the next rip finishes or on restart.")
            self.up_info.configure(text=st["error"])
            self.up_bar.pack_forget()
            self.up_retry.pack(side="right")
        else:
            self.up.pack_forget()
            return
        if not self.up.winfo_ismapped():
            self.up.pack(fill="x", pady=(8, 0), after=self.jobs_frame)

    def run(self):
        self.root.after(100, self._pump)
        self.root.after(500, self._refresh)
        self.root.mainloop()


# ===== Main loop =====
def startup(hb, gui=False):
    print(f"Using {hb}\nDrives: {list_drives() or 'none found yet'}\n"
          "Watching for discs..." + ("" if gui else " (Ctrl+C to quit)"))
    cfg = nas_config()
    if cfg:
        try:
            import smbclient  # noqa: F401
            print(f"NAS upload on: \\\\{cfg['host']}\\{cfg['share']}\\{cfg.get('folder', '')}")
            upload_async()                         # send anything left over from earlier runs
        except ImportError:
            print("nas.json found but 'smbprotocol' isn't installed (pip install smbprotocol); "
                  "NAS upload is off.")


def confirm_reinsert(drive, label):
    """If the disc that just appeared is the very one that was ejected from this drive moments ago
    (drives sometimes pull it back in), ask whether to rip it again. True = go ahead."""
    prev = eject_log.get(drive)
    if not prev or not prev[0] or prev[0] != label or time.time() - prev[1] > REINSERT_WINDOW:
        return True
    ans = timed_input(f"The exact same disc was re-inserted into drive {drive}: proceed or eject? [p/e] "
                      f"(no answer in {PROMPT_TIMEOUT}s = eject): ",
                      options=[("Proceed", "p"), ("Eject", "e")], default="e",
                      heading=f"The exact same disc (\u201c{label}\u201d) was re-inserted into drive {drive}.  "
                              f"Proceed or eject?")
    if ans is None:
        print(f"[{drive}] no answer; ejecting the re-inserted disc.")
        return False
    return ans.strip().lower()[:1] == "p"


def watch(hb, active, stop=None):
    """Poll the drives forever (until `stop` is set), starting a rip for each new disc."""
    handled = set()   # drives whose current disc we've already dealt with
    handled_at = {}   # drive id -> when we last dealt with its disc (an eject after that + a disc present = it came back)
    last_status = time.time()
    while not (stop and stop.is_set()):
        reap(active)
        drives = list_drives()
        handled &= set(drives) | set(active)       # forget drives that vanished (macOS)
        for d in drives:
            drive_state.setdefault(d, "Checking\u2026")
        for d in drives:
            if stop and stop.is_set():
                break
            if d in active:
                drive_state[d] = f"Ripping: {active[d].name}"
                continue                           # already ripping this drive
            info = disc_info(d)
            if info is None:
                handled.discard(d)                 # empty/ejected; ready for the next disc
                drive_state[d] = "Empty"
            elif d not in handled or (d in eject_log and eject_log[d][1] > handled_at.get(d, 0)):
                drive_state[d] = f"Disc: {info[1]}"
                time.sleep(3)                      # let the disc spin up
                info = disc_info(d) or info
                disc_labels[d] = info[1]
                handled.add(d)
                handled_at[d] = time.time()
                if not confirm_reinsert(d, info[1]):
                    safe_eject(d)
                    continue
                job = start_job(hb, d, info, active)
                if job:
                    active[d] = job
                    drive_state[d] = f"Ripping: {job.name}"
                reap(active)
            else:
                drive_state[d] = f"Disc: {info[1]}"
        for d in [d for d in drive_state if d not in drives]:
            del drive_state[d]
        if _gui is None and active and time.time() - last_status >= STATUS_SECONDS:
            print_status(active)                   # the window shows live progress instead
            last_status = time.time()
        if stop:
            stop.wait(POLL_SECONDS)
        else:
            time.sleep(POLL_SECONDS)


def run_gui(hb):
    global _gui
    app = RipperGUI()
    if not hb:
        messagebox.showerror(APP_TITLE, "HandBrakeCLI not found.\n\nPut it next to this program or install it.")
        app.root.destroy()
        return
    _gui = app
    sys.stdout = sys.stderr = app.writer           # print() now goes to the Activity box

    def worker():
        try:
            startup(hb, gui=True)
            watch(hb, app.active, app.stop_event)
        except UserQuit:
            pass
        except Exception:
            print("The disc watcher crashed:\n" + traceback.format_exc())
            return                                 # leave the window up so the error can be read
        if app.stop_event.is_set():
            shutdown(app.active)
        app.done.set()

    app.worker = threading.Thread(target=worker, daemon=True)
    app.worker.start()
    app.run()
    os._exit(0)                                    # don't wait on daemon threads


def _attach_console():
    """A windowed (--noconsole) exe has no console; reconnect to the one it was started from."""
    if OS == "Windows" and sys.stdout is None:
        try:
            ctypes.windll.kernel32.AttachConsole(-1)
            sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8")
        except Exception:
            pass


def main():
    if "--tail" in sys.argv:
        i = sys.argv.index("--tail")
        tail_log(sys.argv[i + 1], sys.argv[i + 2] if len(sys.argv) > i + 2 else "Rip log")
        return
    if "--nas-test" in sys.argv:
        _attach_console()
        nas_test()
        return
    if "--list" in sys.argv:
        _attach_console()
        drives = list_drives()
        print("Detected drives/discs:", drives or "none")
        for d in drives:
            print(" ", d, "->", disc_info(d) or "no disc")
        return
    hb = find_handbrake()
    if tk is not None and "--nogui" not in sys.argv:
        run_gui(hb)
        return
    if tk is None and "--nogui" not in sys.argv:
        print("tkinter isn't available, so running in console mode.")
    if not hb:
        sys.exit("HandBrakeCLI not found. Put it next to this script or install it.")
    startup(hb)
    active = {}       # drive id -> running Job
    try:
        watch(hb, active)
    except KeyboardInterrupt:
        shutdown(active)


if __name__ == "__main__":
    main()