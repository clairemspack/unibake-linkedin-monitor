"""
Lantmännen Unibake — LinkedIn Activity Monitor
================================================
Pulls LinkedIn owned channel data from Brandwatch,
analyses it with Claude, and produces a follow-up brief.

Usage:
    python monitor.py                  # Run, save HTML report, print brief
    python monitor.py --email          # Also send email to recipients in config
    python monitor.py --dry-run        # Use mock data (no API calls)

Config:
    config.json (local) OR environment variables (GitHub Actions / CI):
        BW_API_KEY          Brandwatch API key
        ANTHROPIC_API_KEY   Anthropic API key
        EMAIL_SENDER        Sender email address
        EMAIL_PASSWORD      Sender email password / app password
        EMAIL_RECIPIENTS    Comma-separated recipient addresses

Output:
    - report_YYYY-MM-DD.html           # Dashboard saved next to this script
    - Follow-up email printed to terminal
"""

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import requests

# ─── CONFIG ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "brandwatch_api_key": "",
    "anthropic_api_key": "",
    "lookback_days": 14,
    "active_threshold": 3,
    "low_threshold": 1,
    "email": {
        "enabled": False,
        "smtp_server": "smtp.office365.com",
        "smtp_port": 587,
        "sender": "",
        "password": "",
        "recipients": []
    }
}

BW_BASE = "https://api.brandwatch.com"

# ─── MOCK DATA (--dry-run) ──────────────────────────────────────────────────────
MOCK_MARKETS = [
    {"market": "Germany",        "code": "DE", "posts": 12, "engagement": 589, "last_post": "2025-05-25"},
    {"market": "Netherlands",    "code": "NL", "posts": 9,  "engagement": 341, "last_post": "2025-05-24"},
    {"market": "Sweden",         "code": "SE", "posts": 8,  "engagement": 278, "last_post": "2025-05-24"},
    {"market": "Denmark",        "code": "DK", "posts": 7,  "engagement": 215, "last_post": "2025-05-23"},
    {"market": "Norway",         "code": "NO", "posts": 6,  "engagement": 187, "last_post": "2025-05-22"},
    {"market": "Poland",         "code": "PL", "posts": 5,  "engagement": 143, "last_post": "2025-05-21"},
    {"market": "Austria",        "code": "AT", "posts": 4,  "engagement": 98,  "last_post": "2025-05-20"},
    {"market": "Czech Republic", "code": "CZ", "posts": 3,  "engagement": 76,  "last_post": "2025-05-19"},
    {"market": "Switzerland",    "code": "CH", "posts": 2,  "engagement": 54,  "last_post": "2025-05-17"},
    {"market": "Belgium",        "code": "BE", "posts": 1,  "engagement": 29,  "last_post": "2025-05-14"},
    {"market": "Hungary",        "code": "HU", "posts": 1,  "engagement": 18,  "last_post": "2025-05-13"},
    {"market": "UK",             "code": "GB", "posts": 0,  "engagement": 0,   "last_post": None},
    {"market": "France",         "code": "FR", "posts": 0,  "engagement": 0,   "last_post": None},
    {"market": "Finland",        "code": "FI", "posts": 0,  "engagement": 0,   "last_post": None},
    {"market": "Romania",        "code": "RO", "posts": 0,  "engagement": 0,   "last_post": None},
]


# ─── CONFIG HELPERS ─────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    else:
        cfg = DEFAULT_CONFIG.copy()

    # Environment variables always win — covers GitHub Actions and local .env usage
    cfg["brandwatch_api_key"] = os.getenv("BW_API_KEY") or cfg.get("brandwatch_api_key", "")
    cfg["anthropic_api_key"]  = os.getenv("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key", "")

    # Email env vars (set as GitHub Secrets)
    if os.getenv("EMAIL_SENDER"):
        cfg.setdefault("email", {})
        cfg["email"]["sender"]     = os.getenv("EMAIL_SENDER", "")
        cfg["email"]["password"]   = os.getenv("EMAIL_PASSWORD", "")
        cfg["email"]["recipients"] = [
            r.strip() for r in os.getenv("EMAIL_RECIPIENTS", "").split(",") if r.strip()
        ]
        if cfg["email"]["recipients"]:
            cfg["email"]["enabled"] = True

    return cfg


# ─── BRANDWATCH ─────────────────────────────────────────────────────────────────
def bw_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def verify_bw_key(key: str) -> None:
    print("  Verifying Brandwatch API key...")
    r = requests.get(f"{BW_BASE}/v2/me", headers=bw_headers(key), timeout=15)
    if r.status_code == 401:
        print("x  Invalid Brandwatch API key (401). Check your key.")
        sys.exit(1)
    r.raise_for_status()
    me   = r.json()
    name = me.get("username") or me.get("email") or "unknown"
    print(f"  Connected as {name}")


def fetch_linkedin_channels(key: str) -> list[dict]:
    print("  Fetching LinkedIn owned channels...")
    r = requests.get(f"{BW_BASE}/v2/channels", headers=bw_headers(key), timeout=15)
    r.raise_for_status()
    data = r.json()
    all_channels = data.get("channels") or data.get("results") or data.get("data") or []

    li = [
        c for c in all_channels
        if "linkedin" in (c.get("type") or "").lower()
        or "linkedin" in (c.get("platform") or "").lower()
    ]

    if not li:
        print("  No LinkedIn channels found. Check that pages are connected in Brandwatch.")
        sys.exit(1)

    print(f"  Found {len(li)} LinkedIn channel(s)")
    return li


def fetch_channel_activity(key: str, channel: dict, since: str) -> dict:
    cid  = channel["id"]
    name = channel.get("name") or channel.get("label") or channel.get("handle") or str(cid)
    code = channel.get("country_code") or ""

    try:
        r = requests.get(
            f"{BW_BASE}/v2/channels/{cid}/posts",
            headers=bw_headers(key),
            params={"since": since, "limit": 200},
            timeout=20,
        )
        r.raise_for_status()
        pd    = r.json()
        posts = pd.get("posts") or pd.get("results") or pd.get("data") or []

        engagement = sum(p.get("engagement") or p.get("likes") or 0 for p in posts)

        last_post = None
        if posts:
            ts = posts[0].get("created_time") or posts[0].get("date")
            if ts:
                last_post = ts[:10]

        return {
            "market":     name,
            "code":       code,
            "posts":      len(posts),
            "engagement": engagement,
            "last_post":  last_post,
        }

    except Exception as e:
        print(f"    Could not fetch activity for {name}: {e}")
        return {"market": name, "code": code, "posts": 0, "engagement": 0, "last_post": None}


def fetch_all_activity(key: str, lookback_days: int) -> list[dict]:
    channels = fetch_linkedin_channels(key)
    since    = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    print(f"  Fetching {lookback_days}-day activity for each channel...")

    results = []
    for i, ch in enumerate(channels, 1):
        name = ch.get("name") or ch.get("label") or ch.get("id")
        print(f"    [{i}/{len(channels)}] {name}")
        results.append(fetch_channel_activity(key, ch, since))

    return sorted(results, key=lambda x: x["posts"], reverse=True)


# ─── ANALYSIS ────────────────────────────────────────────────────────────────────
def classify(m: dict, active_t: int, low_t: int) -> str:
    p = m["posts"]
    if p == 0:       return "silent"
    if p < active_t: return "low"
    return "active"


def build_summary(markets: list[dict], cfg: dict) -> dict:
    at = cfg["active_threshold"]
    lt = cfg["low_threshold"]

    for m in markets:
        m["status"] = classify(m, at, lt)

    active = [m for m in markets if m["status"] == "active"]
    low    = [m for m in markets if m["status"] == "low"]
    silent = [m for m in markets if m["status"] == "silent"]

    return {
        "markets":  markets,
        "active":   active,
        "low":      low,
        "silent":   silent,
        "top":      active[0] if active else None,
        "total":    len(markets),
        "date":     datetime.now().strftime("%d %B %Y"),
        "lookback": cfg["lookback_days"],
    }


# ─── CLAUDE ──────────────────────────────────────────────────────────────────────
def generate_brief(summary: dict, anthropic_key: str) -> str:
    print("  Calling Claude to draft follow-up brief...")

    active_names = ", ".join(m["market"] for m in summary["active"]) or "None"
    low_names    = ", ".join(
        f"{m['market']} ({m['posts']} post{'s' if m['posts'] != 1 else ''})"
        for m in summary["low"]
    ) or "None"
