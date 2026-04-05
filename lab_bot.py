#!/usr/bin/env python3
"""
° Lab Bot — standalone Telegram poller for /lab commands only.
Completely independent of JARVIS. Runs in a screen session.

Usage:
  screen -S lab python3 /root/enose/lab_bot.py

Commands:
  /lab Brand Name — your note       add or update a perfume target
  /lab list                          show current targets summary
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime

import requests
import yaml

# ── Config ────────────────────────────────────────────────────────────────────

JARVIS_CONFIG  = "/root/jarvis/config.yaml"
ENV_FILE       = "/root/jarvis/.env"
TARGETS_FILE   = "/root/obsidian-vault/°-lab/05-targets/Perfume Targets.md"
VAULT_PATH     = "/root/obsidian-vault"
POLL_INTERVAL  = 3   # seconds

STATUS_ICONS = {"target": "🎯", "reference": "📖", "watching": "👀", "killed": "❌"}

SYSTEM_PROMPT = """You are a parser for a perfume lab tracking system. Extract structured data from a natural language message about a perfume.

Return ONLY valid JSON with these fields:
  brand           — brand name (string)
  name            — perfume name (string)
  status          — one of: target, reference, watching, killed
  degree_estimate — temperature estimate as string like "~64°" if mentioned, otherwise null
  note            — the user's comment cleaned up as a short phrase (max 12 words)
  is_update       — true if the user is updating an existing entry, false if adding new

Status inference rules:
  target    — "reverse engineer", "clone", "recreate", "reformulate", "making this", "already own", "working on"
  reference — "study", "learn from", "inspiration", "understand", "research", "love the", "want to study"
  watching  — "maybe", "someday", "not sure", "keep an eye", "interesting", "on the radar"
  killed    — "not interested", "ruling out", "too mainstream", "not for me", "kill", "remove", "done with"

Return only the JSON object, no other text."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
    """Load Telegram credentials and Anthropic key."""
    with open(JARVIS_CONFIG) as f:
        cfg = yaml.safe_load(f)
    tg = cfg["telegram"]

    anthropic_key = None
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    anthropic_key = line.split("=", 1)[1]
                    break

    return tg["bot_token"], tg["chat_id"], anthropic_key


def send(token, chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=8,
        )
    except Exception as e:
        print(f"[send error] {e}")


def get_updates(token, offset):
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as e:
        print(f"[poll error] {e}")
    return []


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_with_claude(text, api_key):
    """Call Claude Haiku to parse natural language into structured fields."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": text}],
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[claude error] {resp.status_code}: {resp.text}")
        return None

    content = resp.json()["content"][0]["text"].strip()
    clean = re.sub(r"```(?:json)?\s*|\s*```", "", content).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        print(f"[parse error] bad JSON: {content!r}")
        return None


# ── Vault ─────────────────────────────────────────────────────────────────────

def update_vault(brand, name, status, degree, note, date):
    """Insert or update row in the markdown table. Returns True if updated existing."""
    with open(TARGETS_FILE) as f:
        lines = f.read().split("\n")

    new_row = f"| {brand} | {name} | {status} | {degree} | {note} | {date} |"
    updated = False
    new_lines = []

    for line in lines:
        if (line.startswith("|")
                and not line.startswith("| Brand")
                and not line.startswith("|----")):
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if (len(cols) >= 2
                    and cols[0].lower() == brand.lower()
                    and cols[1].lower() == name.lower()):
                new_lines.append(new_row)
                updated = True
                continue
        new_lines.append(line)

    if not updated:
        insert_at = len(new_lines)
        while insert_at > 0 and new_lines[insert_at - 1].strip() == "":
            insert_at -= 1
        new_lines.insert(insert_at, new_row)

    with open(TARGETS_FILE, "w") as f:
        f.write("\n".join(new_lines))

    return updated


def git_push(brand, name, status):
    try:
        subprocess.run(["git", "-C", VAULT_PATH, "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", VAULT_PATH, "commit", "-m", f"lab: {status} — {brand} {name}"],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "-C", VAULT_PATH, "push"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"[git error] {e}")


def cmd_list():
    """Return a summary of current targets."""
    with open(TARGETS_FILE) as f:
        lines = f.read().split("\n")

    entries = {"target": [], "reference": [], "watching": [], "killed": []}
    for line in lines:
        if (line.startswith("|")
                and not line.startswith("| Brand")
                and not line.startswith("|----")):
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) >= 3:
                status = cols[2].lower()
                if status in entries:
                    entries[status].append(f"{cols[0]} — {cols[1]}")

    lines_out = ["° Perfume Targets\n━━━━━━━━━━━━━━━━━━━━"]
    for status, icon in STATUS_ICONS.items():
        if entries[status]:
            lines_out.append(f"\n{icon} {status.capitalize()}:")
            for e in entries[status]:
                lines_out.append(f"  {e}")

    return "\n".join(lines_out) if len(lines_out) > 2 else "No targets yet."


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    token, chat_id, api_key = load_config()
    if not api_key:
        print("No ANTHROPIC_API_KEY found — exiting")
        return

    offset = 0
    print(f"° Lab Bot running — polling every {POLL_INTERVAL}s")

    while True:
        updates = get_updates(token, offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) != chat_id:
                continue

            text = msg.get("text", "").strip()
            if not text.lower().startswith("/lab"):
                continue

            body = re.sub(r"^/lab\s*", "", text, flags=re.IGNORECASE).strip()
            print(f"[lab] received: {text!r}")

            if body.lower() == "list":
                send(token, chat_id, cmd_list())
                continue

            if not body:
                send(token, chat_id, "Usage: /lab Brand Name — your note about it")
                continue

            send(token, chat_id, "⏳ Parsing...")
            parsed = parse_with_claude(body, api_key)

            if not parsed or not parsed.get("brand") or not parsed.get("name"):
                send(token, chat_id, "Couldn't parse that. Try: /lab Maison Margiela Beach Walk — love the dry down")
                continue

            brand  = parsed["brand"].strip()
            name   = parsed["name"].strip()
            status = parsed.get("status", "watching").strip()
            degree = parsed.get("degree_estimate") or "—"
            note   = parsed.get("note", "").strip()
            date   = datetime.now().strftime("%Y-%m-%d")

            updated = update_vault(brand, name, status, degree, note, date)
            git_push(brand, name, status)

            icon   = STATUS_ICONS.get(status, "•")
            action = "Updated" if updated else "Added"
            send(token, chat_id,
                f"{icon} {action}: {brand} — {name}\n"
                f"Status: {status}\n"
                f"Note: {note}\n"
                f"Vault updated.")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
