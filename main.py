import configparser
import subprocess
import sys
import time
from pathlib import Path

import pyautogui
import pyotp


def base_dir() -> Path:
    # When frozen, config.ini is bundled via --add-data and PyInstaller
    # extracts it to sys._MEIPASS at startup.
    meipass: str | None = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return Path(meipass)
    return Path(__file__).parent


def load_config() -> configparser.ConfigParser:
    config_path = base_dir() / "config.ini"
    if not config_path.exists():
        raise SystemExit(f"Missing config file: {config_path}")
    config = configparser.ConfigParser()
    config.read(config_path)
    return config


def launch_and_login() -> None:
    cfg = load_config()["wow"]

    subprocess.Popen(cfg["path"])
    time.sleep(5)

    pyautogui.typewrite(cfg["password"], interval=0.09)
    pyautogui.press("enter")

    time.sleep(2)

    code = pyotp.TOTP(cfg["totp_secret"]).now()
    pyautogui.typewrite(code, interval=0.09)
    pyautogui.press("enter")


if __name__ == "__main__":
    launch_and_login()
