#!/usr/bin/env python3
"""
° Lab Bot — standalone Telegram poller for /lab and /wear commands.
Completely independent of JARVIS. Runs in a screen session.

Usage:
  screen -S lab python3 /root/enose/lab_bot.py

Commands:
  /lab Brand Name — your note       add or update a perfume target
  /lab list                         show current targets summary
  /wear Brand Name — your notes     log a dry down entry (any time point)
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
ENV_FILE       = "/root/trading-bots/mission-control/sonic/.env"
TARGETS_FILE   = "/root/obsidian-vault/°-lab/05-targets/Perfume Targets.md"
WEAR_DIR       = "/root/obsidian-vault/°-lab/08-wear-journal"
VAULT_PATH     = "/root/obsidian-vault"
POLL_INTERVAL  = 3

STATUS_ICONS = {"target": "🎯", "reference": "📖", "watching": "👀", "killed": "❌"}

LAB_SYSTEM_PROMPT = """You are a parser for a perfume lab tracking system. Extract structured data from a natural language message about a perfume.

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
  watching  — "maybe", "someday", "not sure", "keep an eye", "interesting", "on the radar", "need to smell"
  killed    — "not interested", "ruling out", "too mainstream", "not for me", "kill", "remove", "done with"

Return only the JSON object, no other text."""

WEAR_SYSTEM_PROMPT = """You are a parser for a perfume dry down journal. Extract structured data from a natural language wear note.

Return ONLY valid JSON with these fields:
  brand           — brand name (string)
  name            — perfume name (string)
  time_point      — one of: T+0, T+30min, T+1hr, T+2hr, T+4hr, T+6hr, T+8hr, or "ongoing" if unclear
  nose_notes      — what the user smells, cleaned up (max 20 words)
  degree_read     — temperature estimate like "~68°" if mentioned or inferable, otherwise null
  skin_notes      — anything about how it behaves on skin specifically, otherwise null
  vibe            — one of: love, like, neutral, dislike, evolving — overall feeling at this time point

Time point inference:
  T+0       — "first spray", "opening", "on application", "just put it on"
  T+30min   — "after 30", "half hour in"
  ongoing   — "right now", "currently", "wearing now", "settling", no time specified

Return only the JSON object, no other text."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
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


def call_claude(system_prompt, text, api_key, max_tokens=300):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": max_tokens,
            "system": system_prompt,
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


def git_push(commit_msg):
    try:
        subprocess.run(["git", "-C", VAULT_PATH, "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", VAULT_PATH, "commit", "-m", commit_msg], check=True, capture_output=True)
        subprocess.run(["git", "-C", VAULT_PATH, "push"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"[git error] {e}")


# ── /lab ──────────────────────────────────────────────────────────────────────

def update_targets(brand, name, status, degree, note, date):
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


def cmd_lab(text, api_key, token, chat_id):
    body = re.sub(r"^/lab\s*", "", text, flags=re.IGNORECASE).strip()

    if body.lower() == "list":
        send(token, chat_id, cmd_lab_list())
        return

    if not body:
        send(token, chat_id, "Usage: /lab Brand Name — your note about it")
        return

    send(token, chat_id, "⏳ Parsing...")
    parsed = call_claude(LAB_SYSTEM_PROMPT, body, api_key)

    if not parsed or not parsed.get("brand") or not parsed.get("name"):
        send(token, chat_id, "Couldn't parse that. Try: /lab Maison Margiela Beach Walk — love the dry down")
        return

    brand  = parsed["brand"].strip()
    name   = parsed["name"].strip()
    status = parsed.get("status", "watching").strip()
    degree = parsed.get("degree_estimate") or "—"
    note   = parsed.get("note", "").strip()
    date   = datetime.now().strftime("%Y-%m-%d")

    updated = update_targets(brand, name, status, degree, note, date)
    git_push(f"lab: {status} — {brand} {name}")

    icon   = STATUS_ICONS.get(status, "•")
    action = "Updated" if updated else "Added"
    send(token, chat_id,
        f"{icon} {action}: {brand} — {name}\n"
        f"Status: {status}\n"
        f"Note: {note}\n"
        f"Vault updated.")


def cmd_lab_list():
    with open(TARGETS_FILE) as f:
        lines = f.read().split("\n")
    entries = {"target": [], "reference": [], "watching": [], "killed": []}
    for line in lines:
        if (line.startswith("|")
                and not line.startswith("| Brand")
                and not line.startswith("|----")):
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) >= 3 and cols[2].lower() in entries:
                entries[cols[2].lower()].append(f"{cols[0]} — {cols[1]}")
    out = ["° Perfume Targets\n━━━━━━━━━━━━━━━━━━━━"]
    for status, icon in STATUS_ICONS.items():
        if entries[status]:
            out.append(f"\n{icon} {status.capitalize()}:")
            for e in entries[status]:
                out.append(f"  {e}")
    return "\n".join(out) if len(out) > 2 else "No targets yet."


# ── /wear ─────────────────────────────────────────────────────────────────────

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def wear_file_path(brand, name, date):
    return os.path.join(WEAR_DIR, f"{date}-{slug(brand)}-{slug(name)}.md")


def write_wear_entry(brand, name, time_point, nose_notes, degree_read, skin_notes, vibe, date, now_str):
    path = wear_file_path(brand, name, date)
    degree_str = degree_read or "—"
    skin_str   = skin_notes or "—"
    vibe_icons = {"love": "❤️", "like": "👍", "neutral": "😐", "dislike": "👎", "evolving": "🔄"}
    vibe_icon  = vibe_icons.get(vibe, "•")

    new_entry = (
        f"\n### {time_point} · {now_str}\n"
        f"**Nose:** {nose_notes}\n"
        f"**Degree read:** {degree_str}\n"
        f"**Skin:** {skin_str}\n"
        f"**Vibe:** {vibe_icon} {vibe}\n"
    )

    if os.path.exists(path):
        with open(path, "a") as f:
            f.write(new_entry)
        return False  # appended to existing
    else:
        content = (
            f"---\n"
            f"tags: wear-journal\n"
            f"brand: {brand}\n"
            f"name: {name}\n"
            f"date: {date}\n"
            f"---\n\n"
            f"# {brand} — {name}\n"
            f"*{date}*\n"
            + new_entry
        )
        with open(path, "w") as f:
            f.write(content)
        return True  # new file


def cmd_wear(text, api_key, token, chat_id):
    body = re.sub(r"^/wear\s*", "", text, flags=re.IGNORECASE).strip()

    if not body:
        send(token, chat_id, "Usage: /wear Brand Name — what you're smelling right now")
        return

    send(token, chat_id, "⏳ Logging...")
    parsed = call_claude(WEAR_SYSTEM_PROMPT, body, api_key, max_tokens=400)

    if not parsed or not parsed.get("brand") or not parsed.get("name"):
        send(token, chat_id, "Couldn't parse that. Try: /wear Tamburins Chamo — warm tea, settling nicely")
        return

    brand       = parsed["brand"].strip()
    name        = parsed["name"].strip()
    time_point  = parsed.get("time_point", "ongoing").strip()
    nose_notes  = parsed.get("nose_notes", "").strip()
    degree_read = parsed.get("degree_read")
    skin_notes  = parsed.get("skin_notes")
    vibe        = parsed.get("vibe", "neutral").strip()
    date        = datetime.now().strftime("%Y-%m-%d")
    now_str     = datetime.now().strftime("%H:%M")

    is_new = write_wear_entry(brand, name, time_point, nose_notes, degree_read, skin_notes, vibe, date, now_str)
    git_push(f"wear: {brand} {name} {time_point}")

    action = "New session" if is_new else "Entry added"
    send(token, chat_id,
        f"📝 {action}: {brand} — {name}\n"
        f"Time point: {time_point}\n"
        f"Nose: {nose_notes}\n"
        f"Vibe: {vibe}\n"
        f"Vault updated.")


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
            cmd  = text.lower()

            if cmd.startswith("/lab"):
                print(f"[lab] {text!r}")
                cmd_lab(text, api_key, token, chat_id)
            elif cmd.startswith("/wear"):
                print(f"[wear] {text!r}")
                cmd_wear(text, api_key, token, chat_id)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
