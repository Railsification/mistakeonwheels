# core/logger.py
from datetime import datetime
from colorama import init as colorama_init, Fore, Style

colorama_init(autoreset=True)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ok(msg: str):
    print(f"[{_ts()}] {Fore.GREEN}[OK]{Style.RESET_ALL} {msg}")


def warn(msg: str):
    print(f"[{_ts()}] {Fore.YELLOW}[WARN]{Style.RESET_ALL} {msg}")


def err(msg: str):
    print(f"[{_ts()}] {Fore.RED}[ERR]{Style.RESET_ALL} {msg}")


def info(msg: str):
    print(f"[{_ts()}] {Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}")


def log_cmd(name: str, interaction):
    user = f"{interaction.user} ({interaction.user.id})"
    chan = f"#{interaction.channel}" if interaction.channel else "DM"
    guild = f"{interaction.guild}" if interaction.guild else "DM"
    print(f"[{_ts()}] {Fore.CYAN}[CMD]{Style.RESET_ALL} /{name} by {user} in {chan} ({guild})")
