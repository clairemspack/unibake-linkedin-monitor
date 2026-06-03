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

BW_BASE = "https://api.brandwatch.com"  # Engage API

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
    # Brandwatch Engage API uses apiKey as a query parameter, not Bearer token
    return {}

def bw_params(key: str, extra: dict = None) -> dict:
    params = {"apiKey": key}
    if extra:
        params.update(extra)
    return params


def verify_bw_key(key: str) -> None:
    print("  Verifying Brandwatch API key...")
    r = requests.get(f"{BW_BASE}/v2/me", params=bw_params(key), timeout=15)
    if r.status_code == 401:
        print("x  Invalid Brandwatch API key (401). Check your key.")
        sys.exit(1)
    r.raise_for_status()
    me   = r.json()
    name = me.get("username") or me.get("email") or "unknown"
    print(f"  Connected as {name}")


def fetch_linkedin_channels(key: str) -> list[dict]:
    print("  Fetching LinkedIn owned channels...")
    r = requests.get(f"{BW_BASE}/v2/channels", params=bw_params(key), timeout=15)
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
            params=bw_params(key, {"since": since, "limit": 200}),
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
    silent_names = ", ".join(m["market"] for m in summary["silent"]) or "None"
    top          = summary["top"]
    top_str      = f"{top['market']} with {top['posts']} posts" if top else "N/A"

    prompt = f"""You are helping Claire, Global Digital & Brand Manager at Lantmannen Unibake, \
write a bi-weekly LinkedIn follow-up to her local communications managers.

LinkedIn publishing data - last {summary['lookback']} days as of {summary['date']}:

ACTIVE markets (3+ posts): {active_names}
LOW ACTIVITY markets (1-2 posts): {low_names}
SILENT markets (0 posts): {silent_names}
Top performer: {top_str}
Total markets tracked: {summary['total']}

Write a concise, professional internal follow-up email from Claire to the communications \
managers of the LOW and SILENT markets.

Requirements:
- Warm but direct tone - internal colleagues, not vendors
- Reference the specific {summary['lookback']}-day window and today's date ({summary['date']})
- Name the specific markets that need attention
- Acknowledge the top performer briefly as a benchmark
- Offer support, not just pressure
- Under 200 words, no corporate filler
- Include a subject line

Format:
Subject: [subject line]

[email body]"""

    client  = anthropic.Anthropic(api_key=anthropic_key)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ─── HTML REPORT ─────────────────────────────────────────────────────────────────
STATUS_COLOR = {
    "active": {"badge_bg": "#DCFCE7", "badge_fg": "#166534", "bar": "#27AE60"},
    "low":    {"badge_bg": "#FEF3C7", "badge_fg": "#92400E", "bar": "#B45309"},
    "silent": {"badge_bg": "#FEE2E2", "badge_fg": "#991B1B", "bar": "#C0392B"},
}


def rel_date(date_str) -> str:
    if not date_str:
        return "Never"
    try:
        d    = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        diff = (datetime.now().date() - d).days
        if diff == 0:  return "Today"
        if diff == 1:  return "Yesterday"
        return f"{diff} days ago"
    except Exception:
        return date_str


def build_html_report(summary: dict, brief: str) -> str:
    rows      = ""
    max_posts = max((m["posts"] for m in summary["markets"]), default=1) or 1

    for m in summary["markets"]:
        st    = m["status"]
        cfg   = STATUS_COLOR[st]
        bar_w = int(m["posts"] / max_posts * 100)
        eng   = f"{m['engagement']:,}" if m["engagement"] else "-"
        last  = rel_date(m["last_post"])

        rows += f"""
        <tr>
          <td class="td-market">
            <span class="market-name">{m['market']}</span>
            {f'<span class="market-code">{m["code"]}</span>' if m["code"] else ''}
          </td>
          <td class="td-posts">
            <div class="bar-wrap">
              <div class="bar" style="width:{bar_w}%;background:{cfg['bar']}"></div>
              <span class="bar-num" style="color:{cfg['badge_fg']}">{m['posts']}</span>
            </div>
          </td>
          <td class="td-eng">{eng}</td>
          <td class="td-last">{last}</td>
          <td><span class="badge" style="background:{cfg['badge_bg']};color:{cfg['badge_fg']}">{st.capitalize()}</span></td>
        </tr>"""

    n_active   = len(summary["active"])
    n_low      = len(summary["low"])
    n_silent   = len(summary["silent"])
    brief_html = (brief
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("\n", "<br>"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LinkedIn Activity Report - {summary['date']}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500;9..40,600&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{--bg:#F2EDE4;--surface:#FDFAF5;--green:#1B4332;--text:#1A1714;--muted:#78716C;--border:#E0D8CC}}
body{{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text)}}
header{{background:var(--green);padding:20px 40px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
.hbrand{{display:flex;align-items:center;gap:12px;color:white;font-family:'DM Serif Display',serif;font-size:18px}}
.hbadge{{width:36px;height:36px;background:rgba(255,255,255,0.15);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:15px}}
.hmeta{{color:rgba(255,255,255,0.6);font-size:13px}}
.body{{max-width:960px;margin:0 auto;padding:36px 32px 60px}}
h2{{font-family:'DM Serif Display',serif;font-size:26px;margin-bottom:6px}}
.sub{{font-size:13px;color:var(--muted);margin-bottom:28px}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:32px}}
.stat{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 24px;display:flex;align-items:center;gap:14px}}
.dot{{width:12px;height:12px;border-radius:50%;flex-shrink:0}}
.snum{{font-family:'DM Serif Display',serif;font-size:34px;line-height:1}}
.slabel{{font-size:12px;color:var(--muted);font-weight:500;margin-top:3px}}
table{{width:100%;border-collapse:collapse;background:var(--surface);border-radius:12px;overflow:hidden;border:1px solid var(--border);margin-bottom:36px}}
th{{background:#F5F0E8;font-size:11px;font-weight:600;letter-spacing:0.07em;text-transform:uppercase;color:var(--muted);padding:12px 16px;text-align:left;border-bottom:1px solid var(--border)}}
td{{padding:12px 16px;border-bottom:1px solid var(--border);font-size:14px;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
.market-name{{font-weight:600}}
.market-code{{font-size:11px;color:var(--muted);margin-left:6px}}
.td-posts{{min-width:180px}}
.bar-wrap{{display:flex;align-items:center;gap:8px}}
.bar{{height:8px;border-radius:4px;min-width:3px}}
.bar-num{{font-weight:600;font-size:13px;min-width:20px}}
.td-eng,.td-last{{color:var(--muted);font-size:13px}}
.badge{{padding:3px 9px;border-radius:20px;font-size:11px;font-weight:600;letter-spacing:0.04em}}
.brief-box{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:28px}}
.brief-title{{font-family:'DM Serif Display',serif;font-size:20px;margin-bottom:6px}}
.brief-sub{{font-size:13px;color:var(--muted);margin-bottom:20px}}
.brief-body{{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:20px;font-size:14px;line-height:1.75}}
@media(max-width:600px){{.stats{{grid-template-columns:1fr}}.body{{padding:20px}}header{{padding:16px 20px}}}}
</style>
</head>
<body>
<header>
  <div class="hbrand"><div class="hbadge">L</div>Lantmannen Unibake - LinkedIn Activity Report</div>
  <div class="hmeta">Last {summary['lookback']} days - {summary['date']}</div>
</header>
<div class="body">
  <h2>Publishing Activity</h2>
  <p class="sub">{summary['total']} markets tracked - {summary['date']}</p>
  <div class="stats">
    <div class="stat"><div class="dot" style="background:#27AE60"></div>
      <div><div class="snum">{n_active}</div><div class="slabel">Active (3+ posts)</div></div></div>
    <div class="stat"><div class="dot" style="background:#B45309"></div>
      <div><div class="snum">{n_low}</div><div class="slabel">Low (1-2 posts)</div></div></div>
    <div class="stat"><div class="dot" style="background:#C0392B"></div>
      <div><div class="snum">{n_silent}</div><div class="slabel">Silent (0 posts)</div></div></div>
  </div>
  <table>
    <thead><tr><th>Market</th><th>Posts (14d)</th><th>Engagement</th><th>Last post</th><th>Status</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="brief-box">
    <div class="brief-title">Follow-up Brief</div>
    <div class="brief-sub">Drafted by Claude - {summary['date']}</div>
    <div class="brief-body">{brief_html}</div>
  </div>
</div>
</body>
</html>"""


# ─── EMAIL ───────────────────────────────────────────────────────────────────────
def send_email(cfg: dict, summary: dict, brief: str, html_path: Path) -> None:
    ec = cfg.get("email", {})
    if not ec.get("enabled") or not ec.get("recipients"):
        return

    print("  Sending email...")
    subject = (
        f"LinkedIn Activity Report - {summary['date']} "
        f"({len(summary['silent'])} silent, {len(summary['low'])} low)"
    )

    msg            = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = ec["sender"]
    msg["To"]      = ", ".join(ec["recipients"])

    text_body  = f"LinkedIn Activity Report - {summary['date']}\n\n"
    text_body += f"Active : {len(summary['active'])} markets\n"
    text_body += f"Low    : {len(summary['low'])} - {', '.join(m['market'] for m in summary['low']) or 'none'}\n"
    text_body += f"Silent : {len(summary['silent'])} - {', '.join(m['market'] for m in summary['silent']) or 'none'}\n\n"
    text_body += "-" * 50 + "\nFOLLOW-UP BRIEF\n" + "-" * 50 + "\n\n"
    text_body += brief + "\n\n(Full HTML report attached)"

    msg.attach(MIMEText(text_body, "plain"))

    from email.mime.base import MIMEBase
    from email import encoders
    with open(html_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f"attachment; filename={html_path.name}")
    msg.attach(part)

    with smtplib.SMTP(ec.get("smtp_server", "smtp.gmail.com"), ec.get("smtp_port", 587)) as server:
        server.starttls()
        server.login(ec["sender"], ec["password"])
        server.sendmail(ec["sender"], ec["recipients"], msg.as_string())

    print(f"  Email sent to: {', '.join(ec['recipients'])}")


# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Lantmannen Unibake LinkedIn Activity Monitor")
    parser.add_argument("--email",   action="store_true", help="Send report by email after running")
    parser.add_argument("--dry-run", action="store_true", help="Use mock data, skip Brandwatch API")
    args = parser.parse_args()

    cfg = load_config()

    print("\n-- Lantmannen Unibake - LinkedIn Activity Monitor --")

    # 1. Get market data
    if args.dry_run:
        print("\n[1/3] Using mock data (--dry-run)")
        markets = MOCK_MARKETS
    else:
        bw_key = cfg.get("brandwatch_api_key", "")
        if not bw_key:
            print("No Brandwatch API key found. Set BW_API_KEY env var or add to config.json.")
            sys.exit(1)
        print("\n[1/3] Connecting to Brandwatch...")
        verify_bw_key(bw_key)
        markets = fetch_all_activity(bw_key, cfg["lookback_days"])

    # 2. Analyse
    print("\n[2/3] Analysing activity...")
    summary = build_summary(markets, cfg)
    print(f"  Active : {len(summary['active'])} markets")
    print(f"  Low    : {len(summary['low'])} - {', '.join(m['market'] for m in summary['low']) or 'none'}")
    print(f"  Silent : {len(summary['silent'])} - {', '.join(m['market'] for m in summary['silent']) or 'none'}")

    # 3. Generate brief
    print("\n[3/3] Generating follow-up brief with Claude...")
    anthropic_key = cfg.get("anthropic_api_key", "")
    if not anthropic_key:
        print("No Anthropic API key found. Set ANTHROPIC_API_KEY env var or add to config.json.")
        sys.exit(1)
    brief = generate_brief(summary, anthropic_key)

    # Output
    date_str  = datetime.now().strftime("%Y-%m-%d")
    html_path = Path(__file__).parent / f"report_{date_str}.html"
    html      = build_html_report(summary, brief)
    html_path.write_text(html, encoding="utf-8")
    print(f"\n  HTML report saved: {html_path.name}")

    # Save data.json for the live dashboard artifact
    json_path = Path(__file__).parent / "data.json"
    export = {
        "generated": datetime.now().isoformat(),
        "lookback_days": cfg["lookback_days"],
        "brief": brief,
        "markets": [
            {
                "id":         m["market"].lower().replace(" ", "-"),
                "market":     m["market"],
                "code":       m.get("code", ""),
                "posts":      m["posts"],
                "engagement": m["engagement"],
                "last_post":  m.get("last_post"),
                "status":     m["status"],
            }
            for m in summary["markets"]
        ],
    }
    json_path.write_text(json.dumps(export, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  data.json saved: {json_path.name}")

    print("\n" + "-" * 60)
    print("FOLLOW-UP BRIEF")
    print("-" * 60)
    print(brief)
    print("-" * 60)

    if args.email:
        print("\nSending email...")
        send_email(cfg, summary, brief, html_path)

    print("\nDone.\n")


if __name__ == "__main__":
    main()
