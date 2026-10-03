#!/usr/bin/env python3
"""Auto DVD ripper with multi-drive support.

One main process watches every optical drive. When a disc shows up in a drive
that isn't already being ripped, it asks Movie/TV, then launches a separate
HandBrakeCLI subprocess for that drive and goes straight back to watching.
When a rip subprocess exits for any reason (finished, failed, or killed), the
main process ejects that drive.

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
import codecs, json, os, platform, queue, re, shutil, subprocess, sys, threading, time, traceback
import urllib.parse, urllib.request
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except Exception:                       # no tkinter installed -> console mode only
    tk = ttk = messagebox = None

OS = platform.system()
APP_TITLE = "DVD Ripper"
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

# NAS config: same rule. A nas.json next to the exe wins; otherwise use the embedded one.
if not NAS_CONFIG_FILE.exists():
    NAS_CONFIG_FILE = BUNDLE / "nas.json"


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


def preset_args():
    if PRESET_FILE.exists():
        try:
            data = json.loads(PRESET_FILE.read_text(encoding="utf-8"))
            p = data["PresetList"][0]
            if p.get("Folder"):
                p = p["ChildrenArray"][0]
            return ["--preset-import-file", str(PRESET_FILE), "-Z", p["PresetName"]]
        except Exception as e:
            print(f"Couldn't read {PRESET_FILE.name} ({e}); using fallback preset.")
    else:
        print(f"No movie_profile.json found; using '{FALLBACK_PRESET}'.")
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


def safe_eject(d):
    try:
        eject(d)
    except Exception as e:
        print(f"Could not eject {d}: {e}")


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


def scan_runtime(tool_path, tool, src, drive):
    """Scan the disc ONCE. Returns (main_minutes, main_title_number); either may be None.

    HandBrake: the longest title and its number, so the rip can use '-t N' and skip a
    second full-disc scan. MakeMKV: longest title length only. Runs in a background thread.
    """
    try:
        if tool == "makemkv":
            cmd = [tool_path, "-r", f"--minlength={BD_MIN_LENGTH}", "info", makemkv_source(drive)]
        else:
            cmd = [tool_path, "-i", src, "-t", "0", "--min-duration", "600",
                   "--previews", f"{SCAN_PREVIEWS}:0"]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=SCAN_TIMEOUT, **NOWIN)
        text = r.stdout.decode(errors="replace")
        if tool == "makemkv":
            pat = re.compile(r'^TINFO:\d+,9,\d+,"(\d+):(\d+):(\d+)"', re.M)   # attribute 9 = length
            secs = [int(h) * 3600 + int(m) * 60 + int(x) for h, m, x in pat.findall(text)]
            return (round(max(secs) / 60) if secs else None), None
        best = None                                    # (seconds, title number)
        for m in re.finditer(r"\+ title (\d+):(.*?)(?=\+ title \d+:|\Z)", text, re.S):
            d = re.search(r"\+ duration: (\d+):(\d+):(\d+)", m.group(2))
            if d:
                secs = int(d[1]) * 3600 + int(d[2]) * 60 + int(d[3])
                if best is None or secs > best[0]:
                    best = (secs, int(m.group(1)))
        return (round(best[0] / 60), best[1]) if best else (None, None)
    except Exception:
        return None, None


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


# ===== NAS upload (SMB, no mounting needed) =====
_nas_cfg_cache = None
_upload_lock = threading.Lock()
upload_state = {}      # live info for the window: file, index, total, done, size, started (or just error)


def nas_config():
    """Load nas.json ({host, share, folder, username, password}). None = NAS upload off."""
    global _nas_cfg_cache
    if _nas_cfg_cache is None:
        cfg = False
        if NAS_CONFIG_FILE.exists():
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
                        return                # NAS probably unreachable; don't hammer it
                    time.sleep(5)
        upload_state.clear()
    finally:
        _upload_lock.release()


def upload_async():
    threading.Thread(target=upload_pending, daemon=True).start()


def nas_test():
    cfg = nas_config()
    if not cfg:
        print(f"No usable NAS config at {NAS_CONFIG_FILE}")
        return
    try:
        smb, cfg = _nas_client()
        base = _nas_path(cfg)
        smb.makedirs(base, exist_ok=True)
        probe = base + "\\_rip_test.txt"
        with smb.open_file(probe, mode="w") as f:
            f.write("ok")
        smb.remove(probe)
        print(f"NAS OK: can read/write {base}")
    except Exception as e:
        print(f"NAS test failed: {e}")


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

    def __init__(self, drive, stem, final, partial, cmd, tool="handbrake"):
        self.drive, self.name, self.final, self.partial, self.tool = drive, stem, final, partial, tool
        self.percent = 0.0
        self.fps = 0.0
        self.task = None                 # (current pass, total passes) from HandBrake
        self.started = time.time()
        self._on_progress_line = False
        self.log_path = LOG_DIR / f"{stem}.log"
        self.log = open(self.log_path, "w", encoding="utf-8", errors="replace", newline="")
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


_gui = None          # the RipperGUI when running with a window


def timed_input(prompt, timeout=PROMPT_TIMEOUT, options=None, default=None, heading=None, initial=""):
    """input() that gives up after `timeout` seconds of no typing (returns None).

    In the window, `options` [(label, value), ...] become buttons (returns the value),
    otherwise a text box (pre-filled with `initial`). `heading` replaces `prompt` there.
    """
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


def start_job(hb, drive, info, active):
    """Ask movie/TV (+ title) in the main process, then launch the rip subprocess.

    Prompts auto-answer after PROMPT_TIMEOUT seconds with no typing (movie, default
    title). If the output file already exists, nothing is ripped and the disc is ejected.
    DVDs go to HandBrake (mp4); Blu-rays go to MakeMKV (mkv).
    """
    src, label, kind_of_disc = info
    print(f"\nDisc detected in {drive}: {label}  [{'Blu-ray' if kind_of_disc == 'bd' else 'DVD'}]")
    scan = {}
    scan_tool = find_makemkv() if kind_of_disc == "bd" else hb
    scan_thread = None
    if scan_tool:                            # read the disc's length while you answer the prompt
        scan_thread = threading.Thread(
            target=lambda: scan.update(zip(("m", "t"), scan_runtime(scan_tool, "makemkv" if kind_of_disc == "bd" else "handbrake", src, drive))),
            daemon=True)
        scan_thread.start()
    kind, timed_out = "", False
    while kind not in ("m", "t"):
        ans = timed_input(f"Movie or TV show? [m/t] (auto-picks movie after {PROMPT_TIMEOUT}s): ",
                          options=[("Movie", "m"), ("TV show", "t")], default="m",
                          heading=f"Disc in {drive}: \u201c{label}\u201d. Movie or TV show?")
        if ans is None:
            print("No response; assuming movie.")
            kind, timed_out = "m", True
        else:
            kind = ans.strip().lower()[:1]
    if kind == "t":
        print("TV shows aren't implemented yet; ejecting.")
        safe_eject(drive)
        return None
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


def reap(active):
    """Find rip subprocesses that have ended (for any reason) and eject their drives."""
    for drive, job in list(active.items()):
        rc = job.poll()
        if rc is None:
            continue
        job.thread.join(timeout=5)
        if rc == 0 and job.finish_output():
            job.mark("FINISHED")
            print(f"\n[{drive}] finished: {job.final.name}")
            upload_async()
        else:
            job.discard_partial()
            job.mark(f"FAILED (exit code {rc})")
            print(f"\n[{drive}] rip of '{job.name}' ended without finishing "
                  f"(exit code {rc}); see {LOG_DIR / (job.name + '.log')}")
        safe_eject(drive)
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

BG, PANEL, PANEL2, FG, MUTED = "#1b1d23", "#262932", "#313541", "#e8eaed", "#9aa0ab"
ACCENT, GOOD, BAD, PROMPT_BG = "#4c8dff", "#3ecf8e", "#ff6b6b", "#1f2f4d"


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
        self.text = tk.Text(self.frame, wrap="word", state="disabled", bg="#14161a", fg=FG,
                            relief="flat", borderwidth=0, highlightthickness=0, padx=10, pady=8,
                            font=("Consolas", 9), height=8)
        sb = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
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
        at_end = t.yview()[1] >= 0.999
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
        self.frame = f = ttk.Frame(parent, style="Card.TFrame", padding=14)
        top = ttk.Frame(f, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text=job.name, style="CardTitle.TLabel").pack(side="left")
        ttk.Button(top, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(top, text="Log", command=lambda: LogViewer(app.root, job)).pack(side="right", padx=6)
        kind = "Blu-ray \u2192 MKV  (MakeMKV)" if job.tool == "makemkv" else "DVD \u2192 MP4  (HandBrake)"
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
        if j.task and j.task[1] > 1:
            bits.append(f"Pass {j.task[0]} of {j.task[1]}")
        bits.append(f"{pct:.1f}%")
        if j.fps:
            bits.append(f"{j.fps:.0f} fps")
        e = int(time.time() - j.started)
        bits.append(f"{e // 3600}:{e % 3600 // 60:02d}:{e % 60:02d} elapsed")
        self.info.configure(text="   \u00b7   ".join(bits))


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
        self.root = root = tk.Tk()
        root.title(APP_TITLE)
        k = max(1.0, root.winfo_fpixels("1i") / 96)          # display scaling (e.g. 1.5 at 150%)
        root.geometry(f"{int(940 * k)}x{int(720 * k)}")
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
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, font=("Segoe UI", 10), bordercolor=PANEL2, focuscolor=PANEL)
        s.configure("Card.TFrame", background=PANEL)
        s.configure("Prompt.TFrame", background=PROMPT_BG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("H1.TLabel", font=("Segoe UI Semibold", 20))
        s.configure("H2.TLabel", font=("Segoe UI Semibold", 9), foreground=MUTED)
        s.configure("CardTitle.TLabel", background=PANEL, font=("Segoe UI Semibold", 12))
        s.configure("CardSub.TLabel", background=PANEL, foreground=MUTED)
        s.configure("PromptTitle.TLabel", background=PROMPT_BG, font=("Segoe UI Semibold", 12))
        s.configure("PromptSub.TLabel", background=PROMPT_BG, foreground="#a9b8d6")
        s.configure("Chip.TLabel", background=PANEL, padding=(12, 5))
        s.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0, padding=(14, 6))
        s.map("TButton", background=[("active", "#3d4352"), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
        s.configure("Accent.TButton", background=ACCENT, foreground="white")
        s.map("Accent.TButton", background=[("active", "#6aa0ff")])
        s.configure("Rip.Horizontal.TProgressbar", troughcolor="#14161a", background=ACCENT,
                    bordercolor="#14161a", lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        s.configure("Up.Horizontal.TProgressbar", troughcolor="#14161a", background=GOOD,
                    bordercolor="#14161a", lightcolor=GOOD, darkcolor=GOOD, thickness=10)
        s.configure("TEntry", fieldbackground="#14161a", foreground=FG, insertcolor=FG, padding=6)
        s.configure("Vertical.TScrollbar", background=PANEL2, troughcolor="#14161a", bordercolor="#14161a",
                    lightcolor=PANEL2, darkcolor=PANEL2, arrowcolor=MUTED, gripcount=0)
        s.map("Vertical.TScrollbar", background=[("active", "#4a5062")])

    def _build(self):
        outer = ttk.Frame(self.root, padding=(22, 18))
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer)
        head.pack(fill="x")
        left = ttk.Frame(head)
        left.pack(side="left")
        ttk.Label(left, text=APP_TITLE, style="H1.TLabel").pack(anchor="w")
        self.subtitle = ttk.Label(left, text="Starting\u2026", style="Muted.TLabel")
        self.subtitle.pack(anchor="w")
        self.stop_btn = ttk.Button(head, text="Stop all rips", command=self.stop_all, state="disabled")
        self.stop_btn.pack(side="right")
        self.drives_frame = ttk.Frame(outer)
        self.drives_frame.pack(fill="x", pady=(16, 0))
        self.prompt = ttk.Frame(outer, style="Prompt.TFrame", padding=16)      # shown only when asking something
        ttk.Label(outer, text="RIPS", style="H2.TLabel").pack(anchor="w", pady=(18, 6))
        self.jobs_frame = ttk.Frame(outer)
        self.jobs_frame.pack(fill="x")
        self.empty = ttk.Label(self.jobs_frame, text="No active rips. Insert a disc to begin.", style="Muted.TLabel")
        self.empty.pack(anchor="w", pady=6)
        self.up = ttk.Frame(outer, style="Card.TFrame", padding=14)           # NAS upload panel (shown on demand)
        uptop = ttk.Frame(self.up, style="Card.TFrame")
        uptop.pack(fill="x")
        self.up_title = ttk.Label(uptop, style="CardTitle.TLabel")
        self.up_title.pack(side="left")
        self.up_retry = ttk.Button(uptop, text="Retry now", command=upload_async)
        self.up_file = ttk.Label(self.up, style="CardSub.TLabel")
        self.up_file.pack(anchor="w", pady=(2, 10))
        self.up_bar = ttk.Progressbar(self.up, style="Up.Horizontal.TProgressbar", maximum=100)
        self.up_info = ttk.Label(self.up, style="CardSub.TLabel", wraplength=820, justify="left")
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
        for w in self.prompt.winfo_children():
            w.destroy()
        ttk.Label(self.prompt, text=req["heading"], style="PromptTitle.TLabel",
                  wraplength=820, justify="left").pack(anchor="w")
        body = ttk.Frame(self.prompt, style="Prompt.TFrame")
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
        self.countdown = ttk.Label(self.prompt, style="PromptSub.TLabel")
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
    def stop_all(self):
        jobs = list(self.active.values())
        if jobs and messagebox.askyesno(APP_TITLE, f"Stop {len(jobs)} rip(s)?\n\n"
                                        "Unfinished files are deleted and the discs are ejected."):
            for j in jobs:
                threading.Thread(target=j.stop, daemon=True).start()

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
                ttk.Label(self.drives_frame, text=f"{d}   \u00b7   {state}", style="Chip.TLabel").pack(side="left", padx=(0, 8))
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


def watch(hb, active, stop=None):
    """Poll the drives forever (until `stop` is set), starting a rip for each new disc."""
    handled = set()   # drives whose current disc we've already dealt with
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
            elif d not in handled:
                drive_state[d] = f"Disc: {info[1]}"
                time.sleep(3)                      # let the disc spin up
                info = disc_info(d) or info
                handled.add(d)
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