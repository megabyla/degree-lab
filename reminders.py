#!/usr/bin/env python3
"""
° Lab Daily Briefing
Parses Obsidian shopping list, sends Telegram via Jarvis bot.
Only fires if there are still pending items — goes silent once everything is ordered/delivered.
"""

import os
import re
import requests
from datetime import datetime

_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

def _load_env():
    env = {}
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

_ENV = _load_env()

_VAULT_LAB   = _ENV.get("VAULT_LAB",   "/root/obsidian-vault/°-lab")
SHOPPING_FILE = f"{_VAULT_LAB}/07-shopping/Pending Orders.md"
LOG_FILE      = _ENV.get("LOG_FILE",   "/root/enose/logs/daily_briefing.log")
PHASE = "V0"  # Update as you progress: V0 → V1 → V2 → V3

PHASE_TODOS = {
    "V0": [
        "□ Wire BME680 → enable I2C → confirm sensor reads",
        "□ Mount sensor in jar lid (M2 hardware)",
        "□ Power on → start 48hr burn-in (label: burn-in, do not touch)",
        "□ Day 2: first baseline session — empty jar, clean air, morning + evening",
        "□ Days 2-9: baseline week — 7 days minimum, label: baseline",
        "□ Day 9: first drift check — ethanol on blotter, 3 sessions across 3 days",
        "□ Confirm drift check variance < 8%",
        "□ Day 12: first ingredient session — iso-e-super-PA, 3 replicates",
        "□ 3 sessions across 3 days per ingredient before fingerprint is stable",
        "□ Run PCA after each ingredient addition",
        "□ Day 18+: add hedione-PA, then ambroxan-10pct-PA",
        "□ Week 6+: first Rettre bottle session (only after 3+ stable ingredients)",
    ],
    "V1": [
        "□ Order MQ-3 sensor",
        "□ Order MCP3008 ADC chip",
        "□ Wire MQ-3 into breadboard",
        "□ Run PCA comparison V0 vs V1",
        "□ Confirm MQ-3 improves separation",
    ],
    "V2": [
        "□ Sample Rettre bottle — save 4 time-point fingerprints",
        "□ Sample all ingredients individually",
        "□ Run supplier comparison",
        "□ Build first blend attempt",
    ],
    "V3": [
        "□ Score blend iterations against Rettre target",
        "□ Begin white cotton approximation",
        "□ Library: 8+ ingredients validated",
    ],
}


def load_telegram_config():
    return _ENV["TELEGRAM_BOT_TOKEN"], _ENV["TELEGRAM_CHAT_ID"]


def send_telegram(message: str):
    token, chat_id = load_telegram_config()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }, timeout=10)
    resp.raise_for_status()


def parse_pending_items(filepath):
    """Return list of (item, price) tuples for rows with status 'pending'."""
    if not os.path.exists(filepath):
        return None  # file missing — don't silently skip

    pending = []
    with open(filepath, "r") as f:
        for line in f:
            if "| pending |" in line.lower():
                parts = [p.strip() for p in line.split("|") if p.strip()]
                # Skip bold totals rows
                if not parts or parts[0].startswith("**"):
                    continue
                item = parts[0]
                price = parts[1] if len(parts) > 1 else ""
                pending.append((item, price))
    return pending


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generate_briefing(pending):
    now = datetime.now()
    date_str = now.strftime("%A, %b %d")

    lines = [f"<b>° Lab — {date_str} · Phase {PHASE}</b>"]

    lines.append("\n<b>📦 Still pending:</b>")
    for item, price in pending:
        lines.append(f"  → {_esc(item)}  {_esc(price)}".rstrip())

    lines.append(f"\n<b>📋 {PHASE} next up:</b>")
    for todo in PHASE_TODOS.get(PHASE, []):
        if todo.startswith("□"):
            lines.append(f"  {_esc(todo)}")

    lines.append("\n<i>⚡ Drift check before sampling · 3 replicates min · Purge 3min between samples</i>")
    return "\n".join(lines)


def log(text):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{datetime.now().isoformat()}]\n{text}\n\n")


if __name__ == "__main__":
    pending = parse_pending_items(SHOPPING_FILE)

    if pending is None:
        msg = "° Lab reminder: shopping file not found — check vault path"
        print(msg)
        log(msg)
        send_telegram(msg)
    elif not pending:
        # Nothing pending — go silent, just log
        log("No pending items. Reminder suppressed.")
        print("No pending items. No Telegram sent.")
    else:
        briefing = generate_briefing(pending)
        print(briefing)
        log(briefing)
        send_telegram(briefing)
        print(f"\nSent via Telegram. Logged to {LOG_FILE}")
