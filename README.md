# WoW Launcher

Tiny Windows utility that launches World of Warcraft and auto-types your password (and TOTP code, if your account uses
2FA) into the login screen via `pyautogui`. Originally written for the 3.3.5a / WotLK client. Download the `.exe` and
use it in place of the regular WoW shortcut to skip manual password entry.

## Download

[![Latest release](https://img.shields.io/github/v/release/irotaicnoc/wow_launcher_remember_password)](https://github.com/irotaicnoc/wow_launcher_remember_password/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/irotaicnoc/wow_launcher_remember_password/total)](https://github.com/irotaicnoc/wow_launcher_remember_password/releases)

Grab the latest Windows exe from the [Releases page](https://github.com/irotaicnoc/wow_launcher_remember_password/releases/latest), or [download directly](https://github.com/irotaicnoc/wow_launcher_remember_password/releases/latest/download/wow-launcher.exe).

> Windows SmartScreen will show "Windows protected your PC" on first run because the exe isn't code-signed: click **More info → Run anyway**.

## Usage

On first run, a setup dialog asks for:

- Path to `Wow.exe`
- Account password
- TOTP secret (optional: leave blank if your account doesn't use 2FA)
- Debug mode (optional, off by default: see [Debug mode](#debug-mode))
- **Advanced** (folded away by default): the login flow timings, see [Timings](#timings)

Values are stored in **Windows Credential Manager** under the service name `wow-launcher`, encrypted with your Windows user account's DPAPI key. They aren't readable by other users on the machine and don't roam to other machines.

On every subsequent launch, the script starts WoW, types the password, and (if a TOTP secret is set and the 2FA prompt appears on screen) types the TOTP code. The username field is skipped because the WoW client remembers it.

> Don't switch windows while it's running: `pyautogui` types into whatever window has focus.

## Updating credentials

Three ways to reopen the setup dialog:

- **Hold Shift while launching** the exe. It may take seconds for the program to start, keep Shift held until the setup dialog appears.
- **Run with `--setup`**: `wow-launcher.exe --setup`.
- **Edit/delete the entries directly** in Windows Credential Manager (`Control Panel → Credential Manager → Windows Credentials`, search for `wow-launcher`).

## Timings

Click **▶ Advanced** in the setup dialog to unfold the timings that govern the login flow. Every one of them has a
tooltip on its name, and **Reset to defaults** puts them all back. They are stored alongside the credentials, so they
survive updates of the exe.

| Setting | Default | What it does |
| --- | --- | --- |
| Wait for window (s) | 30 | Give up if no WoW window has appeared this long after launch. |
| Window settle (s) | 0.5 | The window must stop moving and resizing for this long before the client counts as ready. |
| Login UI load (s) | 2.5 | Extra wait for the login screen to finish drawing. **Raise this first if characters go missing.** |
| Focus settle (s) | 0.4 | Pause between focusing the window and the first keystroke. |
| Key hold (s) | 0.03 | How long each key stays held down. Raise it if the client misses individual keys. |
| Typing interval (s) | 0.09 | Pause between one key and the next. |
| 2FA search timeout (s) | 6 | How long to watch for the 2FA prompt before concluding no code is needed. |
| 2FA match confidence | 0.9 | How closely the screen must match the reference image, 0 to 1. Lowering it risks typing a code with no prompt on screen. |
| TOTP minimum life (s) | 4 | If the current code expires sooner than this, wait for the next one. |

Values outside a sane range are refused when saving, and anything unreadable falls back to its default at load time
(with a line in the log saying so), so a bad edit can't stop the launcher from working.

## Debug mode

Off by default. Tick **Debug mode** in the setup dialog (or pass `--debug` for a single run) to make every launch
record what it saw:

- `%TEMP%\wow-launcher.log` gets a detailed trace: window handle and rect, when the window settled, best 2FA match
  score and position, TOTP validity window.
- `%TEMP%\wow-launcher-debug\` gets screenshots: the screen right before the password is typed, and the best 2FA
  candidate frame with a red box drawn around whatever was matched.

That second screenshot is the one to look at if a 2FA code ever gets typed when no prompt was on screen. It shows the
frame the match was actually made against, which is not necessarily what was on screen at that moment.

Without debug mode only warnings and errors are logged, and no screenshots are written.

## Build from source

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

Clone the GitHub repo, then run the following from the project root:

```
uv sync
uv run pyinstaller --onefile --noconsole --name wow-launcher --icon="assets/wotlk_icon.ico" --add-data "assets/2fa_prompt_small.jpg;assets" --add-data "assets/wotlk_icon.ico;assets" --exclude-module setuptools --exclude-module pkg_resources --exclude-module _distutils_hack --exclude-module numpy.f2py --exclude-module numpy.testing --exclude-module numpy.tests --exclude-module numpy.distutils --exclude-module numpy._pyinstaller --exclude-module unittest --exclude-module doctest --exclude-module pydoc --exclude-module pydoc_data --upx-dir "C:\Users\Marco\AppData\Local\Microsoft\WinGet\Packages\UPX.UPX_Microsoft.Winget.Source_8wekyb3d8bbwe\upx-5.1.1-win64" main.py
```

Output: `dist/wow-launcher.exe`, fully self-contained.

The `--exclude-module` flags drop test/build infrastructure (setuptools, numpy.testing, etc.) that gets pulled in transitively but is never executed at runtime. The `--upx-dir` enables [UPX](https://upx.github.io/) compression. Install via `winget install UPX.UPX` if you don't have it; on a different machine, swap the path for wherever UPX lives.

**Pick a different app icon (optional):** the default `--icon` is `assets/wotlk_icon.ico`. `assets/` also ships `cataclysm_icon.ico`, `wow_icon_1.ico`, and `wow_icon_2.ico`; point `--icon` at any of them, or drop your own `.ico` into `assets/` and use that. If you want the same icon on the setup-dialog window too, also update the corresponding `--add-data "assets/<choice>.ico;assets"` flag and the `WINDOW_ICON` constant in `main.py`.

If the password types before the login screen is ready, or the 2FA prompt isn't detected, you don't need to rebuild: tune the values under [Timings](#timings) in the setup dialog. Turn on debug mode first, since the log prints the best match score of every 2FA search, which is the number the match confidence has to sit under. If 2FA stops matching after a resolution / GPU-scaling change, re-crop `assets/2fa_prompt_small.jpg` rather than lowering confidence.
