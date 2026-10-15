# Rippy: auto DVD / Blu-ray ripper

Watches all optical drives and shows everything in a window (it minimizes to the
taskbar like any other app). When a disc is inserted it asks Movie or TV show,
then starts a separate rip subprocess for that drive and goes straight back to
watching for other discs. When a rip ends for any reason (finished, failed, or
killed), the drive is ejected. Finished rips can be uploaded to a NAS over SMB
and removed from the local disk.

- **DVDs** are ripped to MP4 with HandBrakeCLI.
- **Blu-rays** are dumped to MKV (no re-encode) with MakeMKV, because HandBrake
  can't decrypt most Blu-rays.
- **Movies** are named from TMDB: `Title (Year).mp4` / `.mkv`.
- **TV shows (beta)** are worked out from the disc: episode titles are found,
  numbered, and ripped one after another as `Show - S01E03`.

## Where things go

Rips are made in a local staging folder first. If a NAS is set up, each finished
file is uploaded, verified, then deleted locally; otherwise it just stays there.

    ~/Rips/Movies/                       confirmed movies:   Title (Year).mp4 / .mkv
    ~/Rips/Movies/_Needs Review/         movies Rippy wasn't sure about (see below)
    ~/Rips/Movies/_Needs Review/<Show>/Season N/<Show> - S01E03.mp4     TV episodes
    ~/Rips/logs/                         one log per rip (plus a "scan" log per disc)

The same folder layout is used on the NAS, inside the share/folder you configure.
Change `OUTPUT_DIR` at the top of `rip.py` to move the staging folder. TV episodes
always go into `_Needs Review` so a person can check the numbering later.

Rippy's own settings (what the Settings window saves) live outside the program folder:

    Windows:  %APPDATA%\Rippy\settings.json
    Mac:      ~/Library/Application Support/Rippy/settings.json
    Linux:    ~/.config/Rippy/settings.json

That file can hold the NAS password, so on Mac/Linux it is made readable by you only.

## The window

- **Drives** row: each drive and what is in it (Empty, Disc: label, Ripping: name).
- **Question box** (highlighted): Movie or TV, which movie it is, season/disc number.
  One question at a time, so insert discs one after another rather than all at once.
  Enter picks the highlighted (default) button. If the window is minimized, its
  taskbar button flashes when it needs an answer.
- **Rips**: one card per active rip with pass, percent, fps and elapsed time, a
  **Log** button (live HandBrake/MakeMKV output) and a **Cancel** button. TV discs
  show "Episode 2 of 6" and overall progress. The window title, and so the taskbar
  tooltip, shows overall progress too.
- **NAS upload** panel (appears while uploading): file, progress, speed. If an
  upload fails, the file stays local and a **Retry now** button appears.
- **Activity**: a running log of everything that happens.
- **Stop all rips** (or closing the window) stops everything, deletes unfinished
  files and ejects those drives. It asks first if rips are running.
- **Settings** opens the settings window (see below).
- Rippy pops an operating-system notification when it needs you: a question, a
  failed rip, a stuck disc, a failed upload (`NOTIFY` turns this off).

## Movie or TV?

Pick **Movie** or **TV show (beta)** on the question box. If nobody answers within
30 seconds (`PROMPT_TIMEOUT`; this auto-answer can be switched off in **Settings > General**), Rippy reads the disc's titles and decides:
several titles of about the same length (especially with a longer "Play All" that
adds up to them) means TV; one long title, or two long ones of similar length
(widescreen + fullscreen, theatrical + extended), means movie. Anything unclear
is treated as a movie.

## Movie naming (TMDB)

Paste your TMDB key in **Settings > TMDB** (free key: themoviedb.org > Settings > API). It is stored in Rippy's own settings file, never in the exe or script. The `TMDB_API_KEY` environment variable also works as a fallback.
The disc label is cleaned and searched on TMDB, and you get numbered candidates
with the year and runtime. Candidates whose runtime fits the disc's main feature
sort first. Enter takes #1, 0 lets you type your own.

- If nobody answers in time, it only auto-accepts when exactly one result matches
  the cleaned title exactly and its runtime fits the disc. Otherwise the movie is
  ripped into `_Needs Review`: named after the best runtime fit (with year) if
  there is one, else the plain cleaned name with no year.
- No key, or offline: it asks for the title, then an optional year.
- Characters Windows disallows in file names are swapped for safe ones.
- If a file with the same name already exists (locally, on the NAS, or another
  drive is already ripping that name), nothing is ripped and the disc is ejected.

## TV shows (beta)

TV mode asks almost nothing. It reads the disc, looks the show up on TMDB, and:

- Picks the episode-length titles (at least `TV_MIN_EPISODE` seconds, default 15 min;
  a "Play All" title is left out) and ignores extras.
- Works out the season and disc number from the label (`BREAKING_BAD_S2_D1`). Only
  if the label doesn't say does it ask, with a best guess pre-filled; no answer in
  time means the guess is used. TMDB, the show's existing folder, or
  `TV_DEFAULT_SEASON` are used to make that guess.
- Numbers the episodes continuing from what is already in the show's folder
  (on the NAS and locally), using TMDB episode runtimes to help.
- Skips episodes that are already there, and ejects without ripping if the whole
  disc is already done.
- Rips each episode in turn, one run per episode. Each finished episode is kept
  and uploaded straight away, and a failed episode doesn't stop the rest.
- Watches for a drive that stops delivering data (under `TV_STALL_FPS` fps for
  `TV_STALL_SECONDS`): it stops that try, re-seats the disc, and tries the episode once more.

Because the numbering is a best guess, TV rips go to `_Needs Review/<Show>/Season N/`.
The log for each disc lists every title and the file it became.

## NAS upload (SMB)

No drive mounting needed. Rippy talks SMB directly using `smbprotocol`.

Set it up in **Settings > NAS upload** (host or IP, share, folder inside the share,
username, password, and a **Test connection** button that checks login and write
access), or put a `nas.json` next to the script/exe (see `nas.json.example`):

    {"host": "192.168.1.101", "share": "Received Video", "folder": "", "username": "...", "password": "..."}

Leave the folder empty to use the share's top level. Settings saved in the window
win over `nas.json`, and `nas.json` itself is never modified.

How uploads behave:
- Each finished file is copied as `.partN`, renamed when complete, verified, and
  only then deleted locally.
- If the NAS already has a file of that name, the local copy is kept.
- If the NAS accepts no data for `UPLOAD_STALL_SECONDS` the connection is dropped
  and retried (`UPLOAD_ATTEMPTS` tries per file). If it still fails, the file stays
  local and is retried on the next rip or the next start.
- Anything left over from an earlier run is uploaded when Rippy starts.

You can also test from a console: `python rip.py --nas-test`.

## Settings window

- **General**: three switches.
  - *Start Rippy when I sign in to Windows* (installed version only, off by default): adds a per-user
    entry to Windows startup, so no admin rights are needed. Rippy starts minimized to the taskbar and watches your drives.
    Turning it off removes the entry; **Uninstall Rippy** in the app removes it too.
  - *Start ripping by itself if I don't answer within 30 seconds* (on by default): see below. Turn it off and Rippy
    waits for your answer on every disc, however long that takes, so nothing rips until you choose.
  - *Install updates automatically* (installed version only, on by default): see **Updates** above.
  The switches apply right away (the prompt timer applies from the next question).
- **NAS upload**: described above.
- **Movie profile** / **TV profile**: the HandBrake settings used for DVD rips.
  Edit them with a form (Output, Video, Audio, Picture, Subtitles), import an
  exported HandBrake preset, edit the raw JSON, or reset to default. The TV tab
  has **Use the movie profile for TV shows**.

Profiles saved here win over `movie_profile.json` / `tv_profile.json` next to the
app, which are only read and never changed. If neither exists, HandBrake's built-in
"Fast 480p30" preset is used. Blu-ray dumps are not re-encoded, so profiles only
affect DVDs.

## Other behavior worth knowing

- If nobody types for 30 seconds (`PROMPT_TIMEOUT`), every prompt answers itself
  (see the sections above for what each one does). **Settings > General** can turn this off, and then
  prompts wait for you. A prompt that is waiting stays on screen even if you take the disc out; answering it
  just ends in a failed rip and an eject.
- Drives sometimes pull an ejected disc back in. If the same disc label returns to
  a drive within `REINSERT_WINDOW` seconds of an eject, Rippy asks whether to
  proceed; no answer means eject.
- Ejecting is checked: if the tray doesn't open, Rippy retries by talking to the
  drive directly, then tells you to eject by hand.
- Unfinished rips are written as `*.partial.mp4` (or a `*.partial` folder for
  Blu-ray) and renamed when complete. Leftovers from a crash are cleaned up.
- Prefer a plain console? Run `python rip.py --nogui` (Ctrl+C stops all rips, and
  each rip opens its own log window on Windows; `SHOW_LOG_WINDOW = False` turns
  that off).

## One-time setup

1. Install Python 3 (python.org; it includes tkinter, needed for the window).
2. For NAS upload: `pip install smbprotocol`. Nothing else needs pip to run the script.
3. Download HandBrakeCLI (the command-line version, a separate download from the
   HandBrake GUI) and put it in this folder, or install it on your PATH.
4. Optional: set up your encoding profile in **Settings**, or in the HandBrake GUI
   use Presets > Export and save it as `movie_profile.json` (and `tv_profile.json`
   for a separate TV preset) in this folder.
5. Encrypted discs need libdvdcss:
   - Windows: put libdvdcss-2.dll in the same folder as HandBrakeCLI.exe
   - Mac:     brew install libdvdcss
   - Linux:   install libdvd-pkg
   On Windows, build.bat embeds libdvdcss-2.dll in the exe if it is in this
   folder. On Mac and Linux it has to be installed on the system.

## Blu-ray support (optional)

1. Install MakeMKV (makemkv.com). It includes makemkvcon, which Rippy finds
   automatically in the usual install locations.
2. MakeMKV needs a registration key to keep working after its trial period.
   Enter it once in the MakeMKV app (Help > Register).
3. You need a Blu-ray capable drive. For a movie, Rippy dumps every title at
   least `BD_MIN_LENGTH` seconds long (default 40 minutes), keeps the largest one
   as the movie, and deletes the rest. For TV, each episode-length title is dumped.
4. To embed MakeMKV in the exe, copy its whole install folder (on Windows,
   C:\Program Files (x86)\MakeMKV) into this folder as `MakeMKV`, then run
   build.bat. The registration key is stored in your user profile, not in the
   program folder, so register MakeMKV once on each machine you run this on.

## Settings at the top of rip.py

The things you might change, all near the top of `rip.py`:

| Setting | What it does |
| --- | --- |
| `OUTPUT_DIR` | Local staging folder (default `~/Rips/Movies`); logs go beside it |
| Settings > TMDB | TMDB key for movie and TV lookups (blank = no lookup). Env var `TMDB_API_KEY` is the fallback |
| `PROMPT_TIMEOUT` | Seconds without typing before prompts answer themselves (Settings > General can switch this off) |
| `BD_MIN_LENGTH` | Blu-ray: shortest title (seconds) to dump |
| `TV_MIN_EPISODE` | TV: shortest title (seconds) that counts as an episode |
| `TV_DEFAULT_SEASON` | TV: season to assume when the label doesn't say |
| `TV_STALL_SECONDS` / `TV_STALL_FPS` | TV: when a stuck drive is stopped and retried |
| `UPLOAD_STALL_SECONDS` / `UPLOAD_ATTEMPTS` | NAS upload stall limit and tries per file |
| `REINSERT_WINDOW` | Seconds in which the same disc coming back counts as a re-insert |
| `SCAN_PREVIEWS` / `SCAN_TIMEOUT` | How hard and how long the disc scan works |
| `UPDATE_CHECK` / `UPDATE_INTERVAL` | Look for a newer GitHub release (and how often, in seconds) |
| `NOTIFY` | OS notifications when something needs your attention |
| `SHOW_LOG_WINDOW` | Console mode on Windows: open a live log window per rip |

## Command line

    python rip.py              runs the ripper window; insert a disc
    python rip.py --nogui      runs it in the console instead
    python rip.py --list       shows detected drives and whether a disc is in them
    python rip.py --version    prints the version, then exits
    python rip.py --nas-test   checks the NAS login and write access, then exits
    python rip.py --check-update   checks GitHub for a newer release, then exits
    rip.exe --minimized        starts with the window minimized (what start-with-Windows uses)

## One exe with your settings built in
Set everything up on one PC, then open **Settings > Portable copy > Save a copy...** (or run
`rip.exe --export-portable NEW.exe`). You get a single exe with your NAS login, TMDB key and profiles inside it. Copy
that exe to as many PCs as you like; each one runs with no setup. Settings saved on a PC override the built-in ones there.
The copy holds the NAS password and TMDB key in readable form, and an updated Rippy needs a new copy saved.

## Installing and updating
Each release has two Windows downloads:

- **`Rippy.msi`** (installer): installs Rippy for your user account into `%LOCALAPPDATA%\Programs\Rippy` (no admin
  prompt) and adds Start menu shortcuts for Rippy and **Uninstall Rippy**, and starts Rippy when the install finishes (it tells you it's installed). To remove it: the Start menu shortcut, **Settings > About > Uninstall Rippy** in the app, or Windows Settings > Apps. Your settings and rips are kept. Silent installs can pass `NOLAUNCH=1` to skip the launch.
- **`Rippy-portable.exe`**: the same program as a single file. Run it from anywhere, nothing is installed.

Both check GitHub for a newer release when they start (then every 6 hours; if the PC is offline, every 5 minutes
until it works) and show a banner at the top of the window.

The **installed** version updates itself (**Settings > General > Install updates automatically**, on by default):
it downloads the new `Rippy.msi` in the background, checks it against the SHA-256 GitHub lists for it, and installs it
as soon as Rippy is idle, meaning no rip running, no disc being asked about or scanned, no question on screen and no
NAS upload in progress. Rippy then restarts minimized. While an update waits for Rippy to be idle the banner has an
**Install now** button (it asks first if rips are running); the **x** cancels the waiting update. With the setting
off, the banner has an **Install update** button instead, which downloads and installs when you click it.

The **portable** version's banner has a **Download** button that opens the release page; it never updates itself.
`python rip.py --check-update` does the check from a console. Set `UPDATE_CHECK = False` in `rip.py` to turn all of it off.
Versions before 0.0.4 only have the manual **Install update** button, so update those once by hand.

## Releases
The version is `VERSION` near the top of `rip.py` (it must look like `0.0.4`; the MSI needs plain numbers). To publish a
release, change it and push to `main`: GitHub Actions builds `Rippy.msi`, `Rippy-portable.exe` and the Linux build, test-installs
and uninstalls the MSI, and creates release `v0.0.4` with them attached. Pushes that don't change the version just build and don't release.
The MSI's `UpgradeCode` (in `installer/Rippy.wxs`) must never change, or new versions will install beside the old one instead of replacing it.
Neither Windows file is code-signed, so Windows SmartScreen may warn the first time you run one.

## Build a single executable (after testing)

Put HandBrakeCLI (and optionally movie_profile.json, tv_profile.json, nas.json,
libdvdcss-2.dll, MakeMKV\, rippy.ico, rippy.png) in this folder, then run:

    Windows:   build.bat
    Mac/Linux: ./build.sh

The exe appears in the dist folder. `rippy.ico` becomes the exe's icon, and
`rippy.ico` + `rippy.png` are embedded so the window, taskbar and Alt-Tab show the
Rippy logo (without them the default icon is used). On Windows it is built as a
windowed app (no console), so `rip.exe --list` / `--nas-test` print into the
Command Prompt you ran them from. Build separately on each OS you want to support.
HandBrake is GPL; keep that in mind if you share the exe.

## Credits

This product uses the TMDB API but is not endorsed or certified by TMDB.
