"""
╔══════════════════════════════════════════════════════╗
║     SMC FOREX NEWS ALERT BOT v1.0                   ║
║     Monitors: CPI | NFP | Fed | ECB | BoE | Geo     ║
║     Sends: Formatted Telegram alerts                 ║
║     Pairs: EURUSD | GBPUSD                          ║
╚══════════════════════════════════════════════════════╝
"""

import os
import time
import json
import logging
import requests
import feedparser
from datetime import datetime, timezone
from apscheduler.schedulers.blocking import BlockingScheduler

# ─── CONFIG ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
CHECK_INTERVAL   = 300   # seconds (5 min news scan)
CALENDAR_INTERVAL = 3600 # seconds (1 hour calendar scan)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── NEWS SOURCES (RSS FEEDS) ──────────────────────────────
RSS_FEEDS = [
    {"name": "ForexLive",     "url": "https://www.forexlive.com/feed/news"},
    {"name": "FXStreet",      "url": "https://www.fxstreet.com/rss/news"},
    {"name": "Reuters FX",    "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"name": "Investing.com", "url": "https://www.investing.com/rss/news_25.rss"},
    {"name": "MarketWatch",   "url": "https://feeds.marketwatch.com/marketwatch/marketpulse/"},
]

# ─── HIGH IMPACT KEYWORDS ──────────────────────────────────
HIGH_IMPACT = [
    "CPI", "consumer price index", "inflation",
    "NFP", "non-farm payroll", "jobs report", "unemployment",
    "federal reserve", "fed rate", "FOMC", "powell", "rate decision",
    "ECB", "lagarde", "european central bank", "rate cut", "rate hike",
    "bank of england", "BOE", "bailey", "interest rate",
    "GDP", "gross domestic product",
    "ceasefire", "geopolitical", "war", "conflict", "sanctions",
    "EURUSD", "EUR/USD", "GBPUSD", "GBP/USD",
    "dollar", "euro", "pound", "sterling",
    "ISM", "PMI", "retail sales", "trade balance",
]

# ─── SENTIMENT RULES ───────────────────────────────────────
BULL_USD = [
    "strong jobs", "beat expectations", "hawkish fed", "rate hike",
    "above forecast", "stronger than expected", "hot cpi", "high inflation",
    "powell hawkish", "fed hike", "resilient economy", "robust growth",
]
BEAR_USD = [
    "weak jobs", "miss expectations", "dovish fed", "rate cut",
    "below forecast", "weaker than expected", "cool inflation", "low inflation",
    "powell dovish", "fed cut", "recession", "slowdown", "ceasefire",
    "de-escalation", "risk-on", "safe haven fades",
]
BULL_EUR = [
    "ecb hawkish", "euro strong", "eurozone growth", "ecb rate hike",
    "germany strong", "eurozone beat", "lagarde hawkish",
]
BEAR_EUR = [
    "ecb dovish", "euro weak", "eurozone recession", "ecb cut",
    "germany weak", "eurozone miss", "lagarde dovish",
]

# ─── SEEN HEADLINES CACHE ──────────────────────────────────
seen_headlines: set = set()

# ─── ECONOMIC CALENDAR ─────────────────────────────────────
# Using free Forex Factory calendar API (community endpoint)
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

HIGH_IMPACT_EVENTS = [
    "Non-Farm Employment Change", "Unemployment Rate", "CPI m/m", "CPI y/y",
    "Core CPI m/m", "GDP q/q", "GDP m/m", "Retail Sales m/m",
    "FOMC Statement", "Fed Funds Rate", "FOMC Press Conference",
    "ECB Main Refinancing Rate", "ECB Monetary Policy Statement",
    "BOE Official Bank Rate", "BOE Monetary Policy Summary",
    "ISM Manufacturing PMI", "ISM Services PMI",
    "PPI m/m", "Core PCE Price Index m/m",
    "Flash Manufacturing PMI", "Flash Services PMI",
]

# ─── TELEGRAM SENDER ───────────────────────────────────────
def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info("✅ Alert sent to Telegram")
            return True
        else:
            log.error(f"❌ Telegram error: {r.text}")
            return False
    except Exception as e:
        log.error(f"❌ Telegram exception: {e}")
        return False

# ─── SENTIMENT ENGINE ──────────────────────────────────────
def classify_sentiment(text: str) -> dict:
    text_lower = text.lower()

    usd_bull = sum(1 for k in BULL_USD if k in text_lower)
    usd_bear = sum(1 for k in BEAR_USD if k in text_lower)
    eur_bull = sum(1 for k in BULL_EUR if k in text_lower)
    eur_bear = sum(1 for k in BEAR_EUR if k in text_lower)

    # USD Sentiment
    if usd_bull > usd_bear + 1:
        usd_sent = "🔴 BEARISH EUR/GBP (USD Strong)"
        eurusd_bias = "SELL"
        gbpusd_bias = "SELL"
    elif usd_bear > usd_bull + 1:
        usd_sent = "🟢 BULLISH EUR/GBP (USD Weak)"
        eurusd_bias = "BUY"
        gbpusd_bias = "BUY"
    else:
        usd_sent = "⚪ NEUTRAL"
        eurusd_bias = "WAIT"
        gbpusd_bias = "WAIT"

    # EUR specific
    if eur_bull > eur_bear:
        eurusd_bias = "BUY"
    elif eur_bear > eur_bull:
        eurusd_bias = "SELL"

    # Confidence score
    total_signals = usd_bull + usd_bear + eur_bull + eur_bear
    if total_signals >= 4:
        confidence = "HIGH 🔥"
    elif total_signals >= 2:
        confidence = "MEDIUM ⚡"
    else:
        confidence = "LOW 💡"

    return {
        "usd_sentiment": usd_sent,
        "eurusd_bias": eurusd_bias,
        "gbpusd_bias": gbpusd_bias,
        "confidence": confidence,
        "bull_signals": usd_bull + eur_bull,
        "bear_signals": usd_bear + eur_bear,
    }

def is_high_impact(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(kw.lower() in combined for kw in HIGH_IMPACT)

# ─── NEWS SCANNER ──────────────────────────────────────────
def scan_news():
    log.info("🔍 Scanning news feeds...")
    now = datetime.now(timezone.utc)
    alerts_sent = 0

    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:10]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")
                link    = entry.get("link", "")

                if title in seen_headlines:
                    continue

                if is_high_impact(title, summary):
                    seen_headlines.add(title)
                    sentiment = classify_sentiment(title + " " + summary)

                    msg = format_news_alert(
                        source=feed_info["name"],
                        title=title,
                        summary=summary[:280] + "..." if len(summary) > 280 else summary,
                        link=link,
                        sentiment=sentiment,
                        now=now,
                    )
                    send_telegram(msg)
                    alerts_sent += 1
                    time.sleep(1)  # rate limit

        except Exception as e:
            log.warning(f"⚠️ Feed error [{feed_info['name']}]: {e}")

    log.info(f"✅ News scan complete — {alerts_sent} alerts sent")

# ─── CALENDAR SCANNER ──────────────────────────────────────
def scan_calendar():
    log.info("📅 Scanning economic calendar...")
    try:
        r = requests.get(CALENDAR_URL, timeout=15)
        events = r.json()
        now = datetime.now(timezone.utc)

        upcoming = []
        for ev in events:
            if ev.get("impact", "").lower() != "high":
                continue
            ev_title    = ev.get("title", "")
            ev_country  = ev.get("country", "")
            ev_time_str = ev.get("date", "")
            ev_forecast = ev.get("forecast", "N/A")
            ev_previous = ev.get("previous", "N/A")

            if ev_country not in ["USD", "EUR", "GBP"]:
                continue

            try:
                ev_dt = datetime.fromisoformat(ev_time_str.replace("Z", "+00:00"))
            except Exception:
                continue

            # Alert if event is within next 60 minutes
            diff_minutes = (ev_dt - now).total_seconds() / 60
            if 0 < diff_minutes <= 60:
                upcoming.append({
                    "title":    ev_title,
                    "country":  ev_country,
                    "time":     ev_dt.strftime("%H:%M UTC"),
                    "forecast": ev_forecast,
                    "previous": ev_previous,
                    "minutes":  int(diff_minutes),
                })

        if upcoming:
            for ev in upcoming:
                msg = format_calendar_alert(ev)
                send_telegram(msg)
                time.sleep(1)
        else:
            log.info("📅 No high-impact events in next 60 min")

    except Exception as e:
        log.error(f"❌ Calendar scan error: {e}")

# ─── MESSAGE FORMATTERS ────────────────────────────────────
def format_news_alert(source, title, summary, link, sentiment, now) -> str:
    flag_map = {
        "BUY":  "🟢",
        "SELL": "🔴",
        "WAIT": "⚪",
    }
    eurusd_flag = flag_map.get(sentiment["eurusd_bias"], "⚪")
    gbpusd_flag = flag_map.get(sentiment["gbpusd_bias"], "⚪")

    return f"""
⚡ <b>SMC NEWS ALERT</b>
━━━━━━━━━━━━━━━━━━━━━
📰 <b>Source:</b> {source}
🕐 <b>Time:</b> {now.strftime("%H:%M UTC")} | {now.strftime("%d %b %Y")}

📌 <b>{title}</b>

📝 {summary}

━━━━━━━━━━━━━━━━━━━━━
📊 <b>SENTIMENT ANALYSIS</b>
{sentiment["usd_sentiment"]}

{eurusd_flag} <b>EURUSD Bias:</b> {sentiment["eurusd_bias"]}
{gbpusd_flag} <b>GBPUSD Bias:</b> {sentiment["gbpusd_bias"]}
🎯 <b>Confidence:</b> {sentiment["confidence"]}

🔗 <a href="{link}">Read Full Article</a>
━━━━━━━━━━━━━━━━━━━━━
<i>SMC Alert System | Not financial advice</i>
""".strip()

def format_calendar_alert(ev) -> str:
    flag_map = {"USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧"}
    flag = flag_map.get(ev["country"], "🌐")

    # Pre-classify likely market impact
    impact_hint = ""
    if ev["country"] == "USD":
        impact_hint = "📉 USD move expected | Watch EURUSD + GBPUSD"
    elif ev["country"] == "EUR":
        impact_hint = "📉 EUR move expected | Watch EURUSD"
    elif ev["country"] == "GBP":
        impact_hint = "📉 GBP move expected | Watch GBPUSD"

    return f"""
🗓 <b>HIGH IMPACT EVENT — {ev['minutes']} MIN WARNING</b>
━━━━━━━━━━━━━━━━━━━━━
{flag} <b>{ev['country']} | {ev['title']}</b>
🕐 <b>Release Time:</b> {ev['time']}

📊 <b>Forecast:</b> {ev['forecast']}
📈 <b>Previous:</b> {ev['previous']}

⚠️ {impact_hint}

🎯 <b>SMC Protocol:</b>
• Widen stops before release
• Wait for candle close post-news
• Enter only on FVG retest after BOS
• No trades 2 min before / after release
━━━━━━━━━━━━━━━━━━━━━
<i>SMC Alert System | Not financial advice</i>
""".strip()

def format_startup_message() -> str:
    return f"""
🚀 <b>SMC FOREX ALERT BOT — ONLINE</b>
━━━━━━━━━━━━━━━━━━━━━
✅ News Scanner: ACTIVE (every 5 min)
✅ Calendar Monitor: ACTIVE (every 1 hr)
✅ Pairs Monitored: EURUSD | GBPUSD
✅ Currencies: USD | EUR | GBP

📡 <b>Alert Triggers:</b>
• High-impact news detected
• Economic event &lt; 60 min away
• Central bank statements
• Geopolitical developments

🕐 Started: {datetime.now(timezone.utc).strftime("%H:%M UTC | %d %b %Y")}
━━━━━━━━━━━━━━━━━━━━━
<i>Bot is live. Alerts incoming.</i>
""".strip()

# ─── MAIN ──────────────────────────────────────────────────
def main():
    log.info("🚀 SMC Forex Alert Bot starting...")

    # Send startup ping
    send_telegram(format_startup_message())

    # Run initial scans
    scan_news()
    scan_calendar()

    # Schedule recurring scans
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(scan_news,     "interval", seconds=CHECK_INTERVAL,    id="news_scan")
    scheduler.add_job(scan_calendar, "interval", seconds=CALENDAR_INTERVAL, id="cal_scan")

    log.info(f"⏱ News scan every {CHECK_INTERVAL//60} min | Calendar every {CALENDAR_INTERVAL//60} min")
    log.info("✅ Bot running. Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("🛑 Bot stopped.")

if __name__ == "__main__":
    main()
