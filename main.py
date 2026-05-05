import time
import pyautogui
import subprocess

# --- CONFIGURATION ---
WOW_PATH = r"E:\GAMES\World of Warcraft 3.3.5a\Wow.exe"
ACCOUNT = "tyrion321"
PASSWORD = "yH8yiY524zqT3qJ"
# ---------------------


def launch_and_login():
    # 1. Start the game
    subprocess.Popen(WOW_PATH)

    # 2. Wait for the game to load the login screen
    # Adjust this sleep time based on how fast your PC loads the game
    time.sleep(5)

    # 3. Type credentials
    # 'interval' makes it look more like human typing to avoid triggers
    # pyautogui.typewrite(ACCOUNT, interval=0.1)
    # pyautogui.press('tab')
    pyautogui.typewrite(PASSWORD, interval=0.2)
    pyautogui.press('enter')


if __name__ == "__main__":
    launch_and_login()
