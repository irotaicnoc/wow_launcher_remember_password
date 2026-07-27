import argparse
import ctypes
import json
import logging
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Literal
import cv2
import keyring
import keyring.errors
import numpy as np
import pyautogui
import pyotp
from PIL import Image, ImageDraw

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D

WINDOW_STABLE_TIMEOUT_SECONDS = 8
TWO_FA_POLL_SECONDS = 0.15
TWO_FA_CONFIRM_SECONDS = 0.25  # re-check delay: proves the prompt persists and the capture is not frozen
TWO_FA_REFERENCE = "assets/2fa_prompt_small.jpg"

WINDOW_ICON = "assets/wotlk_icon.ico"

KEYRING_SERVICE = "wow-launcher"
SECRET_KEYS = ("path", "password", "totp_secret")
SETTING_KEYS = ("debug", "timings")
KEYRING_KEYS = SECRET_KEYS + SETTING_KEYS


@dataclass(frozen=True)
class Timings:
    """The tunable part of the login flow, editable from the Advanced section of the setup dialog."""
    window_wait_seconds: float = 30.0
    window_stable_seconds: float = 0.5
    login_ui_load_seconds: float = 2.5
    focus_settle_seconds: float = 0.4
    key_hold_seconds: float = 0.03
    typing_interval_seconds: float = 0.09
    two_fa_timeout_seconds: float = 6.0
    two_fa_confidence: float = 0.9
    totp_min_remaining_seconds: float = 4.0


# field, dialog label, tooltip, lowest accepted value, highest accepted value
TIMING_FIELDS = (
    ("window_wait_seconds", "Wait for window (s)",
     "Give up if no WoW window has appeared this long after launch.", 1.0, 300.0),
    ("window_stable_seconds", "Window settle (s)",
     "The window must stop moving and resizing for this long before\nthe client counts as ready.", 0.0, 30.0),
    ("login_ui_load_seconds", "Login UI load (s)",
     "Extra wait for the login screen to finish drawing.\nRaise this first if characters go missing.", 0.0, 120.0),
    ("focus_settle_seconds", "Focus settle (s)",
     "Pause between focusing the window and the first keystroke.", 0.0, 30.0),
    ("key_hold_seconds", "Key hold (s)",
     "How long each key stays held down. Raise it if the client\nmisses individual keys.", 0.0, 1.0),
    ("typing_interval_seconds", "Typing interval (s)",
     "Pause between one key and the next.", 0.0, 2.0),
    ("two_fa_timeout_seconds", "2FA search timeout (s)",
     "How long to watch for the 2FA prompt before concluding\nthe account did not ask for a code.", 0.0, 120.0),
    ("two_fa_confidence", "2FA match confidence",
     "How closely the screen must match the reference image, 0 to 1.\n"
     "Lowering it risks typing a code when no prompt is on screen.", 0.5, 1.0),
    ("totp_min_remaining_seconds", "TOTP minimum life (s)",
     "If the current code expires sooner than this, wait for the next one\n"
     "instead of typing one that dies mid-entry.", 0.0, 29.0),
)

TIMINGS = Timings()

LOG_PATH = Path(tempfile.gettempdir()) / "wow-launcher.log"
DEBUG_DIR = Path(tempfile.gettempdir()) / "wow-launcher-debug"

# All keystroke timing is handled explicitly below; pyautogui's own pause would add 0.1s per call.
pyautogui.PAUSE = 0

_debug_enabled = False
_run_id = ""


def setup_logging(debug: bool) -> None:
    global _debug_enabled, _run_id
    _debug_enabled = debug
    _run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger().setLevel(logging.DEBUG if debug else logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)  # its DEBUG chatter buries ours
    if debug:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        logging.debug("--- run %s (debug mode, artifacts in %s) ---", _run_id, DEBUG_DIR)


def save_debug_image(image: Image.Image, name: str, box: tuple[int, int, int, int] | None = None) -> None:
    """Write a screenshot to the debug folder; no-op unless debug mode is on."""
    if not _debug_enabled:
        return
    try:
        if box is not None:
            image = image.convert("RGB")
            ImageDraw.Draw(image).rectangle(box, outline=(255, 0, 0), width=3)
        image.save(DEBUG_DIR / f"{_run_id}_{name}.png")
    except OSError:
        logging.exception("Could not write debug image %s", name)


def timings_from_json(raw: str) -> Timings:
    """Stored timings, falling back to the default for anything missing, unreadable, or out of range."""
    stored: dict[str, object] = {}
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            logging.warning("Stored timings are not valid JSON; using the defaults")
    defaults = Timings()
    values: dict[str, float] = {}
    for key, label, _tooltip, low, high in TIMING_FIELDS:
        default = getattr(defaults, key)
        raw_value = stored.get(key)
        try:
            value = default if raw_value is None else float(raw_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logging.warning("Stored '%s' (%r) is not a number; using the default %s", label, raw_value, default)
            value = default
        if not low <= value <= high:
            logging.warning("Stored '%s' (%s) is outside %g..%g; using the default %s", label, value, low, high, default)
            value = default
        values[key] = value
    return Timings(**values)


def timings_to_json(timings: Timings) -> str:
    return json.dumps({key: getattr(timings, key) for key, *_ in TIMING_FIELDS})


def apply_settings(creds: dict[str, str]) -> None:
    """Push the stored non-secret settings into the globals the login flow reads."""
    global TIMINGS
    TIMINGS = timings_from_json(creds.get("timings", ""))
    if creds.get("debug") == "1" and not _debug_enabled:
        setup_logging(debug=True)  # apply the setting to this run too, not just the next one
    logging.debug("Timings in effect: %s", TIMINGS)


def base_dir() -> Path:
    # When frozen, bundled assets are extracted to sys._MEIPASS at startup.
    meipass: str | None = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return Path(meipass)
    return Path(__file__).parent


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MAPVK_VK_TO_VSC = 0


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort)]


class _InputUnion(ctypes.Union):
    # The union must keep its full size (MOUSEINPUT is the largest member) or SendInput rejects the struct.
    _fields_ = [("ki", _KeyboardInput), ("mi", _MouseInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("value", _InputUnion)]


def _key_event(vk: int, scan: int, flags: int) -> _Input:
    return _Input(
        type=INPUT_KEYBOARD,
        value=_InputUnion(ki=_KeyboardInput(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)),
    )


def _send(events: list[_Input]) -> None:
    array = (_Input * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(_Input))
    if sent != len(events):
        raise OSError(f"SendInput delivered {sent} of {len(events)} events (last error {ctypes.GetLastError()})")


def _press_vk(vk: int, modifiers: int = 0) -> None:
    """Press one virtual key with real scancodes, held long enough that a per-frame input poll cannot miss it."""
    user32 = ctypes.windll.user32
    modifier_vks = [mod_vk for bit, mod_vk in ((1, VK_SHIFT), (2, VK_CONTROL), (4, VK_MENU)) if modifiers & bit]
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    down = [_key_event(mod_vk, user32.MapVirtualKeyW(mod_vk, MAPVK_VK_TO_VSC), 0) for mod_vk in modifier_vks]
    down.append(_key_event(vk, scan, 0))
    _send(down)
    time.sleep(TIMINGS.key_hold_seconds)
    up = [_key_event(vk, scan, KEYEVENTF_KEYUP)]
    up += [
        _key_event(mod_vk, user32.MapVirtualKeyW(mod_vk, MAPVK_VK_TO_VSC), KEYEVENTF_KEYUP)
        for mod_vk in reversed(modifier_vks)
    ]
    _send(up)


def _press_unicode(char: str) -> None:
    """Send the character itself instead of a key, for characters the active layout cannot produce."""
    _send([_key_event(0, ord(char), KEYEVENTF_UNICODE)])
    time.sleep(TIMINGS.key_hold_seconds)
    _send([_key_event(0, ord(char), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])


def release_stuck_modifiers() -> None:
    """A modifier latched down by the OS silently turns the password into a different string."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    for vk, name in ((VK_SHIFT, "shift"), (VK_CONTROL, "ctrl"), (VK_MENU, "alt")):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            logging.warning("%s was held down before typing; releasing it", name)
            _send([_key_event(vk, user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC), KEYEVENTF_KEYUP)])


def type_text(text: str) -> None:
    """Type text through the layout that is active right now, one key at a time."""
    if sys.platform != "win32":
        pyautogui.typewrite(text, interval=TIMINGS.typing_interval_seconds)
        return
    for char in text:
        # VkKeyScanW resolves the character against the current keyboard layout, so this stays correct
        # even if the layout differs from the one that was active when the process started.
        scanned = ctypes.windll.user32.VkKeyScanW(ctypes.c_wchar(char)) & 0xFFFF
        if scanned == 0xFFFF:
            _press_unicode(char)
        else:
            _press_vk(scanned & 0xFF, (scanned >> 8) & 0x07)
        time.sleep(TIMINGS.typing_interval_seconds)


def press_enter() -> None:
    if sys.platform != "win32":
        pyautogui.press("enter")
        return
    _press_vk(VK_RETURN)


def load_two_fa_reference() -> np.ndarray:
    reference = base_dir() / TWO_FA_REFERENCE
    needle = cv2.imread(str(reference), cv2.IMREAD_GRAYSCALE)
    if needle is None:
        raise FileNotFoundError(f"Could not read the 2FA reference image: {reference}")
    return needle


def match_reference(frame: Image.Image, needle: np.ndarray) -> tuple[float, tuple[int, int]]:
    """Best match score for the reference inside frame, with the top-left corner where it was found."""
    haystack = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2GRAY)
    if haystack.shape[0] < needle.shape[0] or haystack.shape[1] < needle.shape[1]:
        return -1.0, (0, 0)
    result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
    _, best, _, location = cv2.minMaxLoc(result)
    return float(best), (int(location[0]), int(location[1]))


def capture(rect: tuple[int, int, int, int] | None) -> Image.Image:
    """Screenshot of the primary monitor, cropped to the game window when its rect is known."""
    frame = pyautogui.screenshot()
    if rect is None:
        return frame
    box = (max(rect[0], 0), max(rect[1], 0), min(rect[2], frame.width), min(rect[3], frame.height))
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        logging.warning("Window rect %s does not overlap the captured screen %s; searching all of it", rect, frame.size)
        return frame
    return frame.crop(box)


def two_fa_state(rect: tuple[int, int, int, int] | None) -> Literal["found", "absent", "stale"]:
    """Watch the game window for the 2FA prompt until it appears or the timeout expires."""
    needle = load_two_fa_reference()
    state: Literal["found", "absent", "stale"] = "absent"
    best_score, best_location, best_frame = -1.0, (0, 0), None
    deadline = time.monotonic() + TIMINGS.two_fa_timeout_seconds
    started = time.monotonic()

    warned_about_size = False
    while True:
        frame = capture(rect)
        if not warned_about_size and (frame.height < needle.shape[0] or frame.width < needle.shape[1]):
            logging.warning(
                "The 2FA reference is %sx%s but the searched area is only %sx%s, so it can never match",
                needle.shape[1], needle.shape[0], frame.width, frame.height,
            )
            warned_about_size = True
        score, location = match_reference(frame, needle)
        if best_frame is None or score > best_score:
            best_score, best_location, best_frame = score, location, frame

        if score >= TIMINGS.two_fa_confidence:
            # A real prompt stays put on an animated screen. A capture pipeline that has gone stale keeps
            # returning one frozen frame, which is what makes an old prompt look like a current one.
            time.sleep(TWO_FA_CONFIRM_SECONDS)
            again = capture(rect)
            again_score, _ = match_reference(again, needle)
            best_score, best_location, best_frame = score, location, frame  # save the frame we acted on
            if again.tobytes() == frame.tobytes():
                logging.error(
                    "2FA prompt matched at %s (%.3f) but the screen capture is frozen: "
                    "two frames %ss apart are byte-identical",
                    location, score, TWO_FA_CONFIRM_SECONDS,
                )
                state = "stale"
                break
            if again_score >= TIMINGS.two_fa_confidence:
                logging.debug("2FA prompt confirmed at %s (%.3f, then %.3f)", location, score, again_score)
                state = "found"
                break
            logging.warning(
                "2FA prompt matched at %s (%.3f) but was gone %ss later (%.3f); not typing a code",
                location, score, TWO_FA_CONFIRM_SECONDS, again_score,
            )

        if time.monotonic() >= deadline:
            break
        time.sleep(TWO_FA_POLL_SECONDS)

    logging.debug(
        "2FA search ended as '%s' after %.1fs, best score %.3f at %s (threshold %.2f)",
        state, time.monotonic() - started, best_score, best_location, TIMINGS.two_fa_confidence,
    )
    if best_frame is not None:
        box = (
            best_location[0], best_location[1],
            best_location[0] + needle.shape[1], best_location[1] + needle.shape[0],
        )
        save_debug_image(best_frame, f"2fa_{state}_score{best_score:.3f}", box=box)
    return state


def load_credentials() -> dict[str, str]:
    return {key: keyring.get_password(KEYRING_SERVICE, key) or "" for key in KEYRING_KEYS}


def save_credentials(creds: dict[str, str]) -> None:
    for key in KEYRING_KEYS:
        value = creds.get(key, "")
        if value:
            keyring.set_password(KEYRING_SERVICE, key, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass


def attach_tooltip(widget: tk.Widget, text: str, side: Literal["above", "below", "left", "right"] = "right") -> None:
    tip: dict[str, tk.Toplevel | None] = {"win": None}

    def show(_e: tk.Event) -> None:
        if tip["win"] is not None:
            return
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        tk.Label(
            win, text=text, justify="left", background="#ffffe0",
            relief="solid", borderwidth=1, padx=6, pady=3,
        ).pack()
        win.update_idletasks()
        tw, th = win.winfo_reqwidth(), win.winfo_reqheight()
        wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
        ww, wh = widget.winfo_width(), widget.winfo_height()
        gap = 8
        if side == "right":
            x, y = wx + ww + gap, wy
        elif side == "left":
            x, y = wx - tw - gap, wy
        elif side == "below":
            x, y = wx, wy + wh + gap
        else:  # above
            x, y = wx, wy - th - gap
        win.wm_geometry(f"+{x}+{y}")
        tip["win"] = win

    def hide(_e: tk.Event) -> None:
        win = tip["win"]
        if win is not None:
            win.destroy()
            tip["win"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)


def make_password_toggle(parent: tk.Misc, entry: tk.Entry) -> tk.Button:
    btn = tk.Button(parent, text="Show", width=5, takefocus=False)

    def toggle() -> None:
        if entry.cget("show"):
            entry.config(show="")
            btn.config(text="Hide")
        else:
            entry.config(show="•")
            btn.config(text="Show")

    btn.config(command=toggle)
    return btn


def prompt_for_credentials(prefill: dict[str, str]) -> dict[str, str] | None:
    root = tk.Tk()
    root.title("WoW Launcher Setup")
    root.columnconfigure(1, weight=1)
    try:
        root.iconbitmap(str(base_dir() / WINDOW_ICON))
    except tk.TclError:
        pass

    path_var = tk.StringVar(value=prefill.get("path", ""))
    pw_var = tk.StringVar(value=prefill.get("password", ""))
    totp_var = tk.StringVar(value=prefill.get("totp_secret", ""))
    debug_var = tk.BooleanVar(value=prefill.get("debug", "") == "1")
    timings = timings_from_json(prefill.get("timings", ""))

    tk.Label(
        root,
        text="Credentials are stored in Windows Credential Manager,\nencrypted under your Windows user account.",
        justify="left",
        fg="#555",
    ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(12, 8))

    path_label = tk.Label(root, text="WoW executable:")
    path_label.grid(row=1, column=0, sticky="w", padx=10, pady=4)
    attach_tooltip(widget=path_label, text="Full path to Wow.exe\ne.g. C:\\Games\\WoW\\Wow.exe", side="above")
    tk.Entry(root, textvariable=path_var, width=48).grid(row=1, column=1, sticky="we", padx=4, pady=4)

    def browse() -> None:
        chosen = filedialog.askopenfilename(parent=root, title="Select Wow.exe", filetypes=[("Executable", "*.exe")])
        if chosen:
            path_var.set(chosen)

    tk.Button(root, text="Browse…", command=browse).grid(row=1, column=2, padx=(4, 10), pady=4)

    tk.Label(root, text="Password:").grid(row=2, column=0, sticky="w", padx=10, pady=4)
    pw_entry = tk.Entry(root, textvariable=pw_var, show="•", width=48)
    pw_entry.grid(row=2, column=1, sticky="we", padx=4, pady=4)
    make_password_toggle(root, pw_entry).grid(row=2, column=2, sticky="w", padx=(4, 10), pady=4)

    totp_label = tk.Label(root, text="TOTP secret (optional):")
    totp_label.grid(row=3, column=0, sticky="w", padx=10, pady=4)
    attach_tooltip(
        widget=totp_label,
        text="Base32 seed from your authenticator app\nLeave empty if the account has no 2FA",
        side="below",
    )
    totp_entry = tk.Entry(root, textvariable=totp_var, show="•", width=48)
    totp_entry.grid(row=3, column=1, sticky="we", padx=4, pady=4)
    make_password_toggle(root, totp_entry).grid(row=3, column=2, sticky="w", padx=(4, 10), pady=4)

    advanced_frame = tk.Frame(root)
    advanced_frame.grid(row=5, column=0, columnspan=3, sticky="we", padx=10, pady=(2, 0))
    advanced_frame.grid_remove()  # folded away until the user asks for it
    advanced_frame.columnconfigure(0, weight=1)

    debug_check = tk.Checkbutton(advanced_frame, text="Debug mode", variable=debug_var, anchor="w")
    debug_check.grid(row=0, column=0, sticky="w", pady=(0, 4))
    attach_tooltip(
        widget=debug_check,
        text=(
            "Off by default. When on, every launch writes a detailed log and saves\n"
            "screenshots of the login flow, so a wrong 2FA detection can be diagnosed.\n"
            f"Log: {LOG_PATH}\nScreenshots: {DEBUG_DIR}"
        ),
        side="right",
    )

    timings_frame = tk.LabelFrame(advanced_frame, text="Login flow timings", padx=6, pady=4)
    timings_frame.grid(row=1, column=0, sticky="we")
    timings_frame.columnconfigure(1, weight=1)

    tk.Label(
        timings_frame,
        text="Leave these alone unless the launcher mistimes something. Hover a name to see what it does.",
        justify="left",
        fg="#555",
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

    timing_vars: dict[str, tk.StringVar] = {}
    for index, (key, label, tooltip, _low, _high) in enumerate(TIMING_FIELDS, start=1):
        field_label = tk.Label(timings_frame, text=label)
        field_label.grid(row=index, column=0, sticky="w", pady=1)
        attach_tooltip(widget=field_label, text=tooltip, side="right")
        timing_vars[key] = tk.StringVar(value=f"{getattr(timings, key):g}")
        tk.Entry(timings_frame, textvariable=timing_vars[key], width=10, justify="right").grid(
            row=index, column=1, sticky="e", pady=1,
        )

    def reset_timings() -> None:
        defaults = Timings()
        for field, *_ in TIMING_FIELDS:
            timing_vars[field].set(f"{getattr(defaults, field):g}")

    tk.Button(timings_frame, text="Reset to defaults", command=reset_timings).grid(
        row=len(TIMING_FIELDS) + 1, column=0, columnspan=2, sticky="e", pady=(6, 2),
    )

    advanced_open = tk.BooleanVar(value=False)

    def toggle_advanced() -> None:
        if advanced_open.get():
            advanced_frame.grid_remove()
            advanced_toggle.config(text="► Advanced settings")
        else:
            advanced_frame.grid()
            advanced_toggle.config(text="▼ Advanced settings")
        advanced_open.set(not advanced_open.get())
        root.update_idletasks()
        root.geometry("")  # let the window shrink back when the section folds away
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())

    advanced_toggle = tk.Button(
        root, text="► Advanced settings", command=toggle_advanced, relief="flat", anchor="w", width=20,
    )
    advanced_toggle.grid(row=4, column=0, columnspan=2, sticky="w", padx=6, pady=(8, 0))

    result: dict[str, str] = {}

    def on_save() -> None:
        path_value = path_var.get().strip()
        pw_value = pw_var.get()
        totp_value = totp_var.get().strip()
        if not path_value or not pw_value:
            messagebox.showerror(
                "Missing fields",
                "Path and password are required. TOTP secret is optional.",
                parent=root,
            )
            return

        timing_values: dict[str, float] = {}
        problems: list[str] = []
        for key, label, _tooltip, low, high in TIMING_FIELDS:
            text = timing_vars[key].get().strip().replace(",", ".")  # an Italian layout types a decimal comma
            try:
                value = float(text)
            except ValueError:
                problems.append(f"{label}: '{text}' is not a number")
                continue
            if not low <= value <= high:
                problems.append(f"{label}: must be between {low:g} and {high:g}")
                continue
            timing_values[key] = value
        if problems:
            if not advanced_open.get():
                toggle_advanced()  # show the fields being complained about
            messagebox.showerror("Invalid timings", "\n".join(problems), parent=root)
            return

        result["path"] = path_value
        result["password"] = pw_value
        result["totp_secret"] = totp_value
        result["debug"] = "1" if debug_var.get() else "0"
        result["timings"] = timings_to_json(Timings(**timing_values))
        root.destroy()

    button_frame = tk.Frame(root)
    button_frame.grid(row=6, column=0, columnspan=3, sticky="e", padx=10, pady=(8, 12))
    tk.Button(button_frame, text="Cancel", command=root.destroy, width=10).pack(side="right", padx=(4, 0))
    tk.Button(button_frame, text="Save", command=on_save, width=10).pack(side="right")

    root.bind("<Return>", lambda _e: on_save())
    root.bind("<Escape>", lambda _e: root.destroy())

    root.update_idletasks()
    root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())

    root.mainloop()
    return result or None


def shift_held() -> bool:
    if sys.platform != "win32":
        return False
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)


def find_window_for_pid(pid: int) -> int | None:
    """Handle of the first visible top-level window owned by pid, in z-order."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    found: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return found[0] if found else None


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if sys.platform != "win32":
        return None

    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = Rect()
    if not ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def wait_for_window_for_pid(pid: int, timeout: float) -> bool:
    if sys.platform != "win32":
        return True
    deadline = time.monotonic() + timeout
    while True:
        hwnd = find_window_for_pid(pid)
        if hwnd is not None:
            logging.debug("WoW window %s appeared after %.1fs", hwnd, timeout - (deadline - time.monotonic()))
            return True
        if time.monotonic() >= deadline:
            logging.error("Timed out after %ss waiting for WoW window (pid=%s)", timeout, pid)
            return False
        time.sleep(0.2)


def wait_for_stable_window(pid: int) -> None:
    """Wait until the window stops moving and resizing: the client creates it before the login UI exists."""
    if sys.platform != "win32":
        return
    deadline = time.monotonic() + WINDOW_STABLE_TIMEOUT_SECONDS
    last_rect: tuple[int, int, int, int] | None = None
    unchanged_since = time.monotonic()
    while time.monotonic() < deadline:
        hwnd = find_window_for_pid(pid)
        rect = window_rect(hwnd) if hwnd is not None else None
        if rect != last_rect:
            logging.debug("WoW window rect changed to %s", rect)
            last_rect, unchanged_since = rect, time.monotonic()
        elif time.monotonic() - unchanged_since >= TIMINGS.window_stable_seconds:
            return
        time.sleep(0.1)
    logging.warning("WoW window rect never settled within %ss", WINDOW_STABLE_TIMEOUT_SECONDS)


def foreground_pid() -> int:
    if sys.platform != "win32":
        return 0
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    process_id = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(process_id))
    return process_id.value


def foreground_keyboard_layout() -> str:
    """Layout the foreground window types under, e.g. 0x00000410 for Italian, 0x04110411 for a Japanese IME."""
    if sys.platform != "win32":
        return "n/a"
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    return f"0x{user32.GetKeyboardLayout(thread) & 0xFFFFFFFF:08X}"


def focus_window_for_pid(pid: int) -> bool:
    if sys.platform != "win32":
        return True
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    hwnd = find_window_for_pid(pid)
    if hwnd is None:
        logging.error("No visible window found for WoW pid %s", pid)
        return False
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)

    # SetForegroundWindow is blocked when another process owns the foreground.
    # Attaching to the foreground thread's input queue lifts that restriction.
    foreground_hwnd = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None)
    current_thread = kernel32.GetCurrentThreadId()
    attached = False
    if foreground_thread and foreground_thread != current_thread:
        attached = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)

    # Focus changes may be processed asynchronously after AttachThreadInput detaches,
    # and WoW may have several top-level windows — verify by PID, not handle, with a brief poll.
    deadline = time.monotonic() + 0.5
    while True:
        current = foreground_pid()
        if current == pid:
            return True
        if time.monotonic() >= deadline:
            logging.error("WoW did not become foreground (foreground pid=%s, want %s)", current, pid)
            return False
        time.sleep(0.05)


def ensure_foreground(pid: int) -> bool:
    """Re-check right before typing: anything can steal focus between focusing and the first keystroke."""
    if sys.platform != "win32":
        return True
    current = foreground_pid()
    if current == pid:
        return True
    logging.warning("Foreground window belongs to pid %s, not WoW (%s); refocusing", current, pid)
    return focus_window_for_pid(pid)


def ensure_credentials(force: bool = False) -> dict[str, str]:
    creds = load_credentials()
    if force or not creds["path"] or not creds["password"]:
        prompted = prompt_for_credentials(prefill=creds)
        if prompted is None:
            raise SystemExit("Setup cancelled.")
        save_credentials(prompted)
        creds = prompted
    apply_settings(creds)
    return creds


def launch_and_login() -> None:
    cfg = ensure_credentials()

    exe_path = Path(cfg["path"])
    if not exe_path.is_file():
        logging.error("Configured WoW executable does not exist: %s", cfg["path"])
        messagebox.showerror(
            "WoW not found",
            f"The configured WoW executable does not exist:\n{cfg['path']}\n\n"
            "Run with --setup or hold Shift while launching to update it.",
        )
        return

    try:
        proc = subprocess.Popen(str(exe_path))
    except OSError as exc:
        logging.exception("Failed to start WoW")
        messagebox.showerror("Launch failed", f"Could not start WoW:\n{exc}")
        return

    logging.debug("Started WoW (pid=%s) from %s", proc.pid, exe_path)

    if not wait_for_window_for_pid(proc.pid, timeout=TIMINGS.window_wait_seconds):
        messagebox.showerror(
            "Window not found",
            f"WoW did not show a window within {TIMINGS.window_wait_seconds}s; password was not typed.\n\n"
            f"Details written to:\n{LOG_PATH}",
        )
        raise SystemExit(1)

    wait_for_stable_window(proc.pid)
    time.sleep(TIMINGS.login_ui_load_seconds)

    if not focus_window_for_pid(proc.pid):
        messagebox.showerror(
            "Focus failed",
            f"Could not focus the WoW window; password was not typed.\n\nDetails written to:\n{LOG_PATH}",
        )
        raise SystemExit(1)

    # Activation is asynchronous: the foreground pid can already be WoW while the client is still
    # processing the activation, and keystrokes sent in that gap are dropped.
    time.sleep(TIMINGS.focus_settle_seconds)
    release_stuck_modifiers()

    hwnd = find_window_for_pid(proc.pid)
    rect = window_rect(hwnd) if hwnd is not None else None
    save_debug_image(capture(rect), "before_password")

    # Typing the password into whatever else grabbed focus would leak it into another window.
    if not ensure_foreground(proc.pid):
        messagebox.showerror(
            "Focus lost",
            "Another window took the focus just before the password was typed, so nothing was typed.\n\n"
            f"Details written to:\n{LOG_PATH}",
        )
        raise SystemExit(1)

    logging.debug(
        "Typing password into window %s, rect %s, keyboard layout %s", hwnd, rect, foreground_keyboard_layout(),
    )
    type_text(cfg["password"])
    press_enter()

    if not cfg["totp_secret"]:
        return

    state = two_fa_state(rect)
    if state == "stale":
        messagebox.showwarning(
            "2FA skipped",
            "The 2FA prompt appeared to be on screen, but the screen capture is frozen: repeated "
            "screenshots are byte-identical, so what was matched is not what is on screen now.\n\n"
            "No code was typed. If the game is waiting for a 2FA code, enter it manually.\n\n"
            f"Details written to:\n{LOG_PATH}",
        )
        return
    if state != "found":
        return

    if not ensure_foreground(proc.pid):
        logging.error("Lost focus while waiting for the 2FA prompt; not typing a code")
        return

    totp = pyotp.TOTP(cfg["totp_secret"])
    remaining = totp.interval - (time.time() % totp.interval)
    if remaining < TIMINGS.totp_min_remaining_seconds:
        # Typing a code that rolls over mid-entry is rejected by the server as a wrong code.
        logging.debug("Current TOTP expires in %.1fs; waiting for the next one", remaining)
        time.sleep(remaining + 0.2)
    logging.debug("Typing TOTP code (valid for %.1fs)", totp.interval - (time.time() % totp.interval))

    type_text(totp.now())
    press_enter()


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch WoW with credentials stored in Windows Credential Manager.")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open the setup dialog to (re)enter path, password, TOTP secret, and debug mode.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Force debug mode on for this run, whatever the stored setting is.",
    )
    args = parser.parse_args()

    setup_logging(debug=args.debug or keyring.get_password(KEYRING_SERVICE, "debug") == "1")

    try:
        if args.setup or shift_held():
            ensure_credentials(force=True)
            return
        launch_and_login()
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Unhandled error")
        messagebox.showerror("WoW Launcher error", f"{exc}\n\nDetails written to:\n{LOG_PATH}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
