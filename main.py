import argparse
import ctypes
import logging
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Literal
import keyring
import keyring.errors
import pyautogui
import pyotp

VK_SHIFT = 0x10

WINDOW_WAIT_SECONDS = 30
LOGIN_UI_LOAD_SECONDS = 2.5
TYPING_INTERVAL_SECONDS = 0.09

TWO_FA_TIMEOUT_SECONDS = 6
TWO_FA_CONFIDENCE = 0.9
TWO_FA_REFERENCE = "assets/2fa_prompt_small.jpg"

WINDOW_ICON = "assets/wotlk_icon.ico"

KEYRING_SERVICE = "wow-launcher"
KEYRING_KEYS = ("path", "password", "totp_secret")

LOG_PATH = Path(tempfile.gettempdir()) / "wow-launcher.log"


def setup_logging() -> None:
    logging.basicConfig(filename=str(LOG_PATH), level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def base_dir() -> Path:
    # When frozen, bundled assets are extracted to sys._MEIPASS at startup.
    meipass: str | None = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return Path(meipass)
    return Path(__file__).parent


def two_fa_visible() -> bool:
    reference = base_dir() / TWO_FA_REFERENCE
    try:
        location = pyautogui.locateOnScreen(
            str(reference),
            confidence=TWO_FA_CONFIDENCE,
            minSearchTime=TWO_FA_TIMEOUT_SECONDS,
        )
    except pyautogui.ImageNotFoundException:
        return False
    return location is not None


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
        result["path"] = path_value
        result["password"] = pw_value
        result["totp_secret"] = totp_value
        root.destroy()

    button_frame = tk.Frame(root)
    button_frame.grid(row=4, column=0, columnspan=3, sticky="e", padx=10, pady=(8, 12))
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


def wait_for_window_for_pid(pid: int, timeout: float) -> bool:
    if sys.platform != "win32":
        return True
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

    enum_proc = EnumProc(callback)
    deadline = time.monotonic() + timeout
    while True:
        found.clear()
        user32.EnumWindows(enum_proc, 0)
        if found:
            return True
        if time.monotonic() >= deadline:
            logging.error("Timed out after %ss waiting for WoW window (pid=%s)", timeout, pid)
            return False
        time.sleep(0.2)


def focus_window_for_pid(pid: int) -> bool:
    if sys.platform != "win32":
        return True
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    if not found:
        logging.error("No visible window found for WoW pid %s", pid)
        return False
    hwnd = found[0]
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
    foreground_pid = ctypes.c_ulong()
    while True:
        user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(foreground_pid))
        if foreground_pid.value == pid:
            return True
        if time.monotonic() >= deadline:
            logging.error(
                "WoW did not become foreground (foreground pid=%s, want %s)",
                foreground_pid.value, pid,
            )
            return False
        time.sleep(0.05)


def ensure_credentials(force: bool = False) -> dict[str, str]:
    creds = load_credentials()
    if force or not creds["path"] or not creds["password"]:
        prompted = prompt_for_credentials(prefill=creds)
        if prompted is None:
            raise SystemExit("Setup cancelled.")
        save_credentials(prompted)
        creds = prompted
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

    if not wait_for_window_for_pid(proc.pid, timeout=WINDOW_WAIT_SECONDS):
        messagebox.showerror(
            "Window not found",
            f"WoW did not show a window within {WINDOW_WAIT_SECONDS}s; password was not typed.\n\n"
            f"Details written to:\n{LOG_PATH}",
        )
        raise SystemExit(1)

    time.sleep(LOGIN_UI_LOAD_SECONDS)

    if not focus_window_for_pid(proc.pid):
        messagebox.showerror(
            "Focus failed",
            f"Could not focus the WoW window; password was not typed.\n\nDetails written to:\n{LOG_PATH}",
        )
        raise SystemExit(1)

    pyautogui.typewrite(cfg["password"], interval=TYPING_INTERVAL_SECONDS)
    pyautogui.press("enter")

    if not cfg["totp_secret"]:
        return

    if not two_fa_visible():
        return

    code = pyotp.TOTP(cfg["totp_secret"]).now()
    pyautogui.typewrite(code, interval=TYPING_INTERVAL_SECONDS)
    pyautogui.press("enter")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Launch WoW with credentials stored in Windows Credential Manager.")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open the setup dialog to (re)enter path, password, and TOTP secret.",
    )
    args = parser.parse_args()

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
