import argparse
import ctypes
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import keyring
import keyring.errors
import pyautogui
import pyotp

VK_SHIFT = 0x10

TWO_FA_REFERENCE = "assets/2fa_prompt_small.jpg"
TWO_FA_TIMEOUT_SECONDS = 6
TWO_FA_CONFIDENCE = 0.9

KEYRING_SERVICE = "wow-launcher"
KEYRING_KEYS = ("path", "password", "totp_secret")


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


def prompt_for_credentials(prefill: dict[str, str]) -> dict[str, str] | None:
    root = tk.Tk()
    root.title("wow-launcher setup")
    root.resizable(False, False)

    path_var = tk.StringVar(value=prefill.get("path", ""))
    pw_var = tk.StringVar(value=prefill.get("password", ""))
    totp_var = tk.StringVar(value=prefill.get("totp_secret", ""))

    tk.Label(
        root,
        text="Credentials are stored in Windows Credential Manager,\nencrypted under your Windows user account.",
        justify="left",
        fg="#555",
    ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(12, 8))

    tk.Label(root, text="WoW executable:").grid(row=1, column=0, sticky="w", padx=10, pady=4)
    tk.Entry(root, textvariable=path_var, width=48).grid(row=1, column=1, padx=4, pady=4)

    def browse() -> None:
        chosen = filedialog.askopenfilename(
            parent=root, title="Select Wow.exe", filetypes=[("Executable", "*.exe")]
        )
        if chosen:
            path_var.set(chosen)

    tk.Button(root, text="Browse…", command=browse).grid(row=1, column=2, padx=(4, 10), pady=4)

    tk.Label(root, text="Password:").grid(row=2, column=0, sticky="w", padx=10, pady=4)
    tk.Entry(root, textvariable=pw_var, show="•", width=48).grid(
        row=2, column=1, columnspan=2, sticky="we", padx=(4, 10), pady=4
    )

    tk.Label(root, text="TOTP secret (optional):").grid(row=3, column=0, sticky="w", padx=10, pady=4)
    tk.Entry(root, textvariable=totp_var, show="•", width=48).grid(
        row=3, column=1, columnspan=2, sticky="we", padx=(4, 10), pady=4
    )

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

    root.mainloop()
    return result or None


def shift_held() -> bool:
    if sys.platform != "win32":
        return False
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)


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

    subprocess.Popen(cfg["path"])
    time.sleep(5)

    pyautogui.typewrite(cfg["password"], interval=0.09)
    pyautogui.press("enter")

    if not cfg["totp_secret"]:
        return

    if not two_fa_visible():
        return

    code = pyotp.TOTP(cfg["totp_secret"]).now()
    pyautogui.typewrite(code, interval=0.09)
    pyautogui.press("enter")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch WoW with credentials stored in Windows Credential Manager.")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open the setup dialog to (re)enter path, password, and TOTP secret.",
    )
    args = parser.parse_args()

    if args.setup or shift_held():
        ensure_credentials(force=True)
        return

    launch_and_login()


if __name__ == "__main__":
    main()
