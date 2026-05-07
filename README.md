# WoW Launcher

Tiny Windows utility that launches World of Warcraft and auto-types your password (and TOTP code, if your account uses
2FA) into the login screen via `pyautogui`. Originally written for the 3.3.5a / WotLK client. Download the `.exe` and
use it in place of the regular WoW shortcut to skip manual password entry.

## Usage

On first run, a setup dialog asks for:

- Path to `Wow.exe`
- Account password
- TOTP secret (optional — leave blank if your account doesn't use 2FA)

Values are stored in **Windows Credential Manager** under the service name `wow-launcher`, encrypted with your Windows user account's DPAPI key. They aren't readable by other users on the machine and don't roam to other machines.

On every subsequent launch, the script starts WoW, types the password, and — if a TOTP secret is set and the 2FA prompt appears on screen — types the TOTP code. The username field is skipped because the WoW client remembers it.

> Don't switch windows while it's running — `pyautogui` types into whatever window has focus.

## Updating credentials

Three ways to reopen the setup dialog:

- **Hold Shift while launching** the exe.
- **Run with `--setup`**: `wow-launcher.exe --setup`.
- **Edit/delete the entries directly** in Windows Credential Manager (`Control Panel → Credential Manager → Windows Credentials`, search for `wow-launcher`).

## Build from source

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

Clone the GitHub repo, then run the following from the project root:

```
uv sync
uv run pyinstaller --onefile --noconsole --name wow-launcher --icon="assets/wotlk_icon.ico" --add-data "assets/2fa_prompt_small.jpg;assets" main.py
```

Output: `dist/wow-launcher.exe`, fully self-contained.

If the password types before the login screen appears, or the 2FA prompt isn't detected, tune the constants at the top of `main.py` (`time.sleep(5)` after launch, `TWO_FA_TIMEOUT_SECONDS`, `TWO_FA_CONFIDENCE`). If 2FA stops matching after a resolution / GPU-scaling change, re-crop `assets/2fa_prompt_small.jpg` rather than lowering confidence.
