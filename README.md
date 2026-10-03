# DVD Ripper

Watches all optical drives and shows everything in a window (it minimizes to the
taskbar like any other app). When a disc is inserted it asks Movie or TV show,
then starts a separate HandBrakeCLI subprocess to rip that drive to MP4 and
goes straight back to watching for other discs. When a rip subprocess ends for
any reason (finished, failed, or killed), the drive is ejected. TV shows are
not implemented yet.

DVDs are ripped to MP4 with HandBrakeCLI. Blu-rays are dumped to MKV (no
re-encode) with MakeMKV, because HandBrake can't decrypt most Blu-rays.

Rips go to:  ~/Rips/Movies   (change OUTPUT_DIR at the top of rip.py)
             DVD -> Title.mp4,  Blu-ray -> Title.mkv
Logs go to:  ~/Rips/logs     (one HandBrake log per rip)

Notes:
- The Movie/TV question and title choice appear as a highlighted box in the window,
  one disc at a time, so insert discs one after another rather than simultaneously.
  Enter picks the highlighted (default) button. If the window is minimized, its
  taskbar button flashes when it needs an answer.
- If nobody types for 30 seconds (PROMPT_TIMEOUT), the prompts answer themselves:
  Movie, and the title taken from the disc label (see Movie naming below).
- If a file with the same name already exists in the output folder (or another
  drive is already ripping that name), nothing is ripped and the disc is ejected.
- Each active rip has a progress card (pass, percent, fps, elapsed time) with a
  Log button (live HandBrake/MakeMKV output) and a Cancel button. The window title,
  and so the taskbar tooltip, shows overall progress.
- "Stop all rips", or closing the window, stops everything, deletes unfinished
  files, and ejects those drives (it asks first if rips are running).
- Prefer a plain console? Run `python rip.py --nogui` (then Ctrl+C stops all rips and
  each rip opens its own log window on Windows; SHOW_LOG_WINDOW = False turns that off).
- Unfinished rips are written as *.partial.mp4 and renamed when complete.

## Movie naming (TMDB)
Set TMDB_API_KEY at the top of rip.py (free key: themoviedb.org > Settings > API).
The disc label is cleaned and searched on TMDB; you get numbered candidates with
years. Enter takes #1, 0 lets you type your own. Files are named `Title (Year).mp4`
(`.mkv` for Blu-ray), with characters Windows disallows swapped for safe ones.
- If nobody answers in time, it only auto-accepts when exactly one result matches
  the cleaned title exactly. Otherwise (a remake, or no match) it rips into
  `~/Rips/Movies/_Needs Review` under the plain cleaned name, with no year.
- No key, or offline: it asks for the title, then an optional year.
- This product uses the TMDB API but is not endorsed or certified by TMDB.

## One-time setup
1. Install Python 3 (python.org). Nothing else to pip install to run the script.
2. Download HandBrakeCLI (the command-line version, a separate download from
   the HandBrake GUI) and put it in this folder, or install it on your PATH.
3. In the HandBrake GUI: Presets > Export, and save your movie preset as
   `movie_profile.json` in this folder. (If missing, a built-in preset is used.)
4. Encrypted discs need libdvdcss:
   - Windows: put libdvdcss-2.dll in the same folder as HandBrakeCLI.exe
   - Mac:     brew install libdvdcss
   - Linux:   install libdvd-pkg
   On Windows, build.bat embeds libdvdcss-2.dll in the exe if it is in this
   folder. On Mac and Linux it has to be installed on the system.

## Blu-ray support (optional)
1. Install MakeMKV (makemkv.com). It includes makemkvcon, which this script
   finds automatically in the usual install locations.
2. MakeMKV needs a registration key to keep working after its trial period.
   Enter it once in the MakeMKV app (Help > Register).
3. You need a Blu-ray capable drive. For Blu-ray the script dumps every title
   at least BD_MIN_LENGTH seconds long (default 40 minutes), keeps the largest
   one as the movie, and deletes the rest.
4. To embed MakeMKV in the exe, copy its whole install folder (on Windows,
   C:\Program Files (x86)\MakeMKV) into this folder as `MakeMKV`, then run
   build.bat. The registration key is stored in your user profile, not in the
   program folder, so register MakeMKV once on each machine you run this on.

## Test
    python rip.py --list     shows detected drives and whether a disc is in them
    python rip.py            runs the ripper window; insert a disc
    python rip.py --nogui    runs it in the console instead

## Build a single executable (after testing)
Put HandBrakeCLI and movie_profile.json in this folder, then run:
    Windows:   build.bat
    Mac/Linux: ./build.sh
The exe appears in the dist folder. On Windows it is built as a windowed app (no
console), so `rip.exe --list` / `--nas-test` print into the Command Prompt you ran
them from. Python needs tkinter for the window (included with the python.org installer). Build separately on each OS you want to
support. HandBrake is GPL; keep that in mind if you share the exe.