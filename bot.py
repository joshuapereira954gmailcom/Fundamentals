"""
╔══════════════════════════════════════════════════════════════════╗
║     SMC AI TRADING INTELLIGENCE BOT v3.1                       ║
║     Live Signals | AI Analysis | Self-Learning | Grey Levels    ║
║     Chart Bias Analysis | SMT Divergence | Claude Vision        ║
║     Powered by Claude AI + Smart Money Concepts                 ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, time, json, logging, requests, feedparser, base64, threading
from datetime import datetime, timezone
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN",    "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID",  "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TWELVEDATA_KEY    = os.getenv("TWELVEDATA_API_KEY", "")

PAIRS        = ["EUR/USD", "GBP/USD"]
DB_FILE      = Path("signal_memory.json")
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
seen_headlines: set = set()
last_update_id = 0
pending_images: dict = {}
pending_lock   = threading.Lock()

# ══════════════════════════════════════════════════════════════
# MEMORY
# ══════════════════════════════════════════════════════════════

def load_memory() -> dict:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text())
        except:
            pass
    return {
        "signals": [], "total_signals": 0,
        "wins": 0, "losses": 0, "win_rate": 0.0,
        "best_setup": "", "worst_setup": "",
        "setup_stats": {}, "session_stats": {}, "pair_stats": {},
        "last_updated": "",
    }

def save_memory(mem: dict):
    mem["last_updated"] = datetime.now(timezone.utc).isoformat()
    DB_FILE.write_text(json.dumps(mem, indent=2))

def record_signal(mem: dict, sig: dict) -> dict:
    mem["signals"].append({
        "id": mem["total_signals"] + 1,
        "ts": datetime.now(timezone.utc).isoformat(),
        "pair": sig.get("pair"), "bias": sig.get("bias"),
        "entry": sig.get("entry"), "sl": sig.get("sl"),
        "tp1": sig.get("tp1"), "tp2": sig.get("tp2"),
        "confidence": sig.get("confidence"),
        "setup": sig.get("setup"), "session": sig.get("session"),
        "outcome": "PENDING",
    })
    mem["total_signals"] += 1
    save_memory(mem)
    return mem

def record_outcome(mem: dict, sig_id: int, outcome: str) -> dict:
    for s in mem["signals"]:
        if s["id"] == sig_id and s["outcome"] == "PENDING":
            s["outcome"] = outcome
            mem["wins" if outcome == "WIN" else "losses"] += 1
            tot = mem["wins"] + mem["losses"]
            mem["win_rate"] = round(mem["wins"] / tot * 100, 1) if tot else 0

            def update_stat(d, key, outcome):
                if key not in d:
                    d[key] = {"wins": 0, "losses": 0, "win_rate": 0}
                d[key]["wins" if outcome == "WIN" else "losses"] += 1
                t = d[key]["wins"] + d[key]["losses"]
                d[key]["win_rate"] = round(d[key]["wins"] / t * 100, 1) if t else 0

            update_stat(mem["setup_stats"],   s.get("setup", "?"),   outcome)
            update_stat(mem["session_stats"], s.get("session", "?"), outcome)
            update_stat(mem["pair_stats"],    s.get("pair", "?"),    outcome)

            if mem["setup_stats"]:
                b = max(mem["setup_stats"].items(), key=lambda x: x[1]["win_rate"])
                w = min(mem["setup_stats"].items(), key=lambda x: x[1]["win_rate"])
                mem["best_setup"]  = f"{b[0]} ({b[1]['win_rate']}%)"
                mem["worst_setup"] = f"{w[0]} ({w[1]['win_rate']}%)"
            break
    save_memory(mem)
    return mem

def learning_context(mem: dict) -> str:
    if mem["total_signals"] == 0:
        return "No history yet. First signal — use neutral confidence baseline."
    return f"""
LEARNING DATA ({mem['total_signals']} signals tracked):
Win Rate: {mem['win_rate']}% | Wins: {mem['wins']} | Losses: {mem['losses']}
Best Setup: {mem.get('best_setup','N/A')}
Worst Setup: {mem.get('worst_setup','N/A')}
Setup Stats: {json.dumps(mem['setup_stats'])}
Session Stats: {json.dumps(mem['session_stats'])}
Pair Stats: {json.dumps(mem['pair_stats'])}
INSTRUCTION: Adjust confidence UP for historically strong setups/sessions.
Adjust confidence DOWN for historically weak ones. Learn from the data.
""".strip()

# ══════════════════════════════════════════════════════════════
# PRICE LEVELS
# ══════════════════════════════════════════════════════════════

def get_levels(pair: str) -> dict:
    try:
        url = (f"https://api.twelvedata.com/time_series"
               f"?symbol={pair}&interval=1day&outputsize=6&apikey={TWELVEDATA_KEY}")
        d = requests.get(url, timeout=10).json()
        if "values" not in d:
            raise ValueError("Bad response")
        v   = d["values"]
        cur = float(v[0]["close"])
        pdh = float(v[1]["high"])
        pdl = float(v[1]["low"])
        pdo = float(v[1]["open"])
        pwh = max(float(x["high"]) for x in v)
        pwl = min(float(x["low"])  for x in v)
        eq  = round((pdh + pdl) / 2, 5)
        pip = lambda a, b: round(abs(a-b)*10000, 1)
        return {
            "pair": pair, "current": round(cur,5),
            "pdh": round(pdh,5), "pdl": round(pdl,5),
            "pdo": round(pdo,5), "pwh": round(pwh,5),
            "pwl": round(pwl,5), "eq": eq,
            "bias": "BULLISH" if cur > pdo else "BEARISH",
            "pips_pdh": pip(cur,pdh), "pips_pdl": pip(cur,pdl),
            "pips_pwh": pip(cur,pwh), "pips_pwl": pip(cur,pwl),
            "pips_eq":  pip(cur,eq),
            "above_pdh": cur > pdh, "below_pdl": cur < pdl,
        }
    except Exception as e:
        log.warning(f"Levels error {pair}: {e}")
        return {"pair": pair, "error": str(e), "current": 0, "bias": "NEUTRAL"}

# ══════════════════════════════════════════════════════════════
# SESSION
# ══════════════════════════════════════════════════════════════

def get_session() -> str:
    h = datetime.now(timezone.utc).hour
    if 7  <= h < 10: return "LONDON"
    if 12 <= h < 15: return "NEW YORK"
    if 0  <= h < 3:  return "ASIA"
    return "OFF-SESSION"

def session_emoji(s: str) -> str:
    return {"LONDON":"🇬🇧","NEW YORK":"🇺🇸","ASIA":"🌏"}.get(s,"🌐")

# ══════════════════════════════════════════════════════════════
# NEWS
# ══════════════════════════════════════════════════════════════

HI_KW = ["CPI","inflation","NFP","non-farm","unemployment","federal reserve",
          "FOMC","powell","rate","ECB","lagarde","BOE","bailey","GDP",
          "ceasefire","geopolitical","EURUSD","GBPUSD","dollar","euro","pound","PMI",
          "gold","silver","XAU","XAG"]

def get_news(n=5) -> list:
    out = []
    for feed in ["https://www.forexlive.com/feed/news",
                 "https://www.fxstreet.com/rss/news"]:
        try:
            for e in feedparser.parse(feed).entries[:10]:
                t = e.get("title","")
                if any(k.lower() in t.lower() for k in HI_KW):
                    out.append(t)
                if len(out) >= n: return out
        except: pass
    return out

def sentiment(text: str) -> dict:
    t = text.lower()
    BULL_USD = ["strong jobs","beat expectations","hawkish","rate hike","above forecast","stronger than expected","hot cpi","resilient"]
    BEAR_USD = ["weak jobs","miss","dovish","rate cut","below forecast","weaker than expected","cool inflation","recession","ceasefire","de-escalation"]
    BULL_EUR = ["ecb hawkish","euro strong","eurozone growth","lagarde hawkish"]
    BEAR_EUR = ["ecb dovish","euro weak","eurozone recession","lagarde dovish"]
    ub=sum(1 for k in BULL_USD if k in t); ud=sum(1 for k in BEAR_USD if k in t)
    eb=sum(1 for k in BULL_EUR if k in t); ed=sum(1 for k in BEAR_EUR if k in t)
    eu = "BUY" if (ud>ub+1 or eb>ed) else "SELL" if (ub>ud+1 or ed>eb) else "WAIT"
    gb = "BUY" if ud>ub+1 else "SELL" if ub>ud+1 else "WAIT"
    c  = sum([ub,ud,eb,ed])
    conf = "HIGH 🔥" if c>=4 else "MEDIUM ⚡" if c>=2 else "LOW 💡"
    return {"eurusd": eu, "gbpusd": gb, "conf": conf}

# ══════════════════════════════════════════════════════════════
# CALENDAR
# ══════════════════════════════════════════════════════════════

def get_events() -> list:
    try:
        events = requests.get(CALENDAR_URL, timeout=15).json()
        now    = datetime.now(timezone.utc)
        out    = []
        for ev in events:
            if ev.get("impact","").lower() != "high": continue
            if ev.get("country","") not in ["USD","EUR","GBP"]: continue
            try:
                dt   = datetime.fromisoformat(ev["date"].replace("Z","+00:00"))
                diff = (dt - now).total_seconds() / 60
                if 0 < diff <= 120:
                    out.append({"title": ev.get("title",""), "country": ev.get("country",""),
                                "minutes": int(diff), "forecast": ev.get("forecast","N/A"),
                                "previous": ev.get("previous","N/A")})
            except: continue
        return out
    except: return []

# ══════════════════════════════════════════════════════════════
# CLAUDE
# ══════════════════════════════════════════════════════════════

def ask_claude(prompt: str) -> str:
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        return r.json()["content"][0]["text"]
    except Exception as e:
        log.error(f"Claude error: {e}")
        return ""

def ask_claude_vision(images: list, prompt: str) -> str:
    content = []
    for img in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]},
        })
    content.append({"type": "text", "text": prompt})
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-opus-4-5", "max_tokens": 2000,
                  "messages": [{"role": "user", "content": content}]},
            timeout=60,
        )
        return r.json()["content"][0]["text"]
    except Exception as e:
        log.error(f"Claude Vision error: {e}")
        return "❌ Vision analysis failed. Check logs."

def build_bias_prompt(num_charts: int, session: str, news: list, events: list) -> str:
    nw = "\n".join(news) if news else "No major news available."
    ev = "\n".join([f"- {e['country']} {e['title']} in {e['minutes']}min "
                    f"(Fcst:{e['forecast']} Prev:{e['previous']})"
                    for e in events]) if events else "No high-impact events next 2hrs."
    return f"""You are an elite SMC and ICT trading analyst specialising in XAU/USD, XAG/USD, EUR/USD, and GBP/USD.

{num_charts} chart image(s) sent. Current session: {session}.

Live news:
{nw}

Upcoming high-impact events:
{ev}

Analyse every chart. Use actual price levels visible in the charts only. Do not fabricate levels.

---
📊 CHART BIAS — {session} | {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}

INSTRUMENT(S): [list each]
TIMEFRAME(S): [list each]

---
🎯 DAILY BIAS
Direction: BULLISH / BEARISH / NEUTRAL
Conviction: HIGH / MEDIUM / LOW
Reasoning: [2-3 sentences on structure and price action]

---
📐 MARKET STRUCTURE
Trend: [HTF direction]
Last BOS/CHoCH: [level and direction]
Phase: [Accumulation / Distribution / Trending / Retracement]

---
🔁 SMT DIVERGENCE
Present: YES / NO
Type: [Bearish SMT / Bullish SMT / None]
Detail: [which instrument leading vs lagging and what it signals]
Actionable: YES / NO — [why]

---
📍 KEY LEVELS
[List all visible: OBs, FVGs, EQH/EQL, PDH/PDL, liquidity pools with price levels]

---
⚠️ INVALIDATION
Bull invalidation: [price + reason]
Bear invalidation: [price + reason]

---
🗞️ FUNDAMENTALS
[Macro context, central bank stance, upcoming catalysts. Flag any news/event conflict with technical bias.]

---
✅ TRADE SETUP
Direction: LONG / SHORT / NO SETUP
Entry zone: [price range]
SL: [level + logic]
TP1 / TP2: [levels]
RR: [estimate]
Trigger needed: [e.g. M15 BOS, sweep of X, reaction at Y]

---
📝 NOTES
[Confluence, prop firm warnings, session timing, volatility flags]"""

# ══════════════════════════════════════════════════════════════
# CHART ANALYSIS
# ══════════════════════════════════════════════════════════════

def download_image_as_base64(file_id: str) -> dict:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10,
        )
        file_path = r.json()["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        img_r = requests.get(url, timeout=30)
        b64 = base64.b64encode(img_r.content).decode()
        media_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
        return {"data": b64, "media_type": media_type}
    except Exception as e:
        log.error(f"Image download error: {e}")
        return {}

def process_chart_images(chat_id: str, file_ids: list):
    tg_to(chat_id, f"📡 Analysing {len(file_ids)} chart(s)... hang tight.")
    images = [img for fid in file_ids if (img := download_image_as_base64(fid))]
    if not images:
        tg_to(chat_id, "❌ Could not download chart images. Try again.")
        return
    session = get_session()
    result  = ask_claude_vision(images, build_bias_prompt(len(images), session, get_news(5), get_events()))
    if len(result) <= 4096:
        tg_to(chat_id, result)
    else:
        for chunk in [result[i:i+4000] for i in range(0, len(result), 4000)]:
            tg_to(chat_id, chunk)
            time.sleep(0.5)

def flush_pending_images(chat_id: str):
    time.sleep(5)
    with pending_lock:
        items = pending_images.pop(chat_id, [])
    if items:
        process_chart_images(chat_id, [i["file_id"] for i in items])

# ══════════════════════════════════════════════════════════════
# SIGNAL GENERATOR
# ══════════════════════════════════════════════════════════════

def generate_signal(pair, lv, news, events, session, mem) -> dict:
    lc = learning_context(mem)
    nw = "\n".join(news) if news else "No major news."
    ev = "\n".join([f"- {e['country']} {e['title']} in {e['minutes']}min "
                    f"(Fcst:{e['forecast']} Prev:{e['previous']})"
                    for e in events]) if events else "No events next 2hrs."
    lvl = f"""
Current Price : {lv.get('current','N/A')}
PDH (BSL)     : {lv.get('pdh','N/A')}  [{lv.get('pips_pdh','?')} pips away]
PDL (SSL)     : {lv.get('pdl','N/A')}  [{lv.get('pips_pdl','?')} pips away]
PWH           : {lv.get('pwh','N/A')}  [{lv.get('pips_pwh','?')} pips away]
PWL           : {lv.get('pwl','N/A')}  [{lv.get('pips_pwl','?')} pips away]
Equilibrium   : {lv.get('eq','N/A')}   [{lv.get('pips_eq','?')} pips away]
HTF Bias      : {lv.get('bias','N/A')}
Above PDH     : {lv.get('above_pdh',False)}
Below PDL     : {lv.get('below_pdl',False)}""".strip()

    prompt = f"""You are an elite institutional forex trading AI using Smart Money Concepts (SMC).
Analyse this data for {pair} and generate ONE precise trade signal.

GREY LEVEL TECHNOLOGY:
{lvl}

SESSION: {session} {session_emoji(session)}
TIME: {datetime.now(timezone.utc).strftime('%H:%M UTC')}

LIVE NEWS:
{nw}

UPCOMING EVENTS:
{ev}

SELF-LEARNING DATA:
{lc}

Respond ONLY in this exact JSON format. No other text:
{{
  "bias": "BUY" or "SELL" or "NO TRADE",
  "setup": "PDH Sweep+BOS" or "PDL Sweep+BOS" or "EQ Retest" or "PWH Break" or "PWL Break" or "FVG Entry" or "Momentum" or "NO TRADE",
  "entry_zone": "price or zone",
  "stop_loss": "price",
  "tp1": "price",
  "tp2": "price",
  "rr": "e.g. 1:2.5",
  "confidence": 0-100,
  "key_level": "most important level right now",
  "news_alignment": "ALIGNED" or "CONFLICTED" or "NEUTRAL",
  "session_quality": "HIGH" or "MEDIUM" or "LOW",
  "avoid_reason": "if NO TRADE explain why",
  "reasoning": "2-3 sentences max",
  "smart_money_note": "one key SMC observation",
  "risk_warning": "specific risk right now"
}}

Rules:
- News conflicts with technicals = reduce confidence 20% or NO TRADE
- High impact event in <30 min = NO TRADE
- No clear setup = NO TRADE
- Only BUY/SELL if confidence above 60""".strip()

    resp = ask_claude(prompt)
    try:
        clean = resp.strip()
        if "`" in clean: clean = clean.split("`")[1]
        if clean.startswith("json"): clean = clean[4:]
        return json.loads(clean)
    except Exception as e:
        log.error(f"Parse error: {e} | {resp[:100]}")
        return {"bias":"NO TRADE","avoid_reason":"AI parse error","confidence":0}

# ══════════════════════════════════════════════════════════════
# FORMATTERS
# ══════════════════════════════════════════════════════════════

def fmt_signal(pair, sig, lv, session, mem, sid) -> str:
    bias = sig.get("bias","NO TRADE")
    conf = sig.get("confidence",0)
    be   = {"BUY":"🟢","SELL":"🔴","NO TRADE":"⚪"}.get(bias,"⚪")
    ce   = "🔥" if conf>=75 else "⚡" if conf>=55 else "💡"
    ae   = {"ALIGNED":"✅","CONFLICTED":"❌","NEUTRAL":"➖"}.get(sig.get("news_alignment","NEUTRAL"),"➖")
    se   = {"HIGH":"🔥","MEDIUM":"⚡","LOW":"💡"}.get(sig.get("session_quality","LOW"),"💡")
    pd   = pair.replace("/","")

    if bias == "NO TRADE":
        return f"""⚪ <b>NO TRADE — {pd}</b>
━━━━━━━━━━━━━━━━━━━━━━
{session_emoji(session)} {session} | {datetime.now(timezone.utc).strftime('%H:%M UTC')}
📊 Price: <code>{lv.get('current','N/A')}</code>

🚫 <b>Reason:</b> {sig.get('avoid_reason','N/A')}
🔑 <b>Watch Level:</b> {sig.get('key_level','N/A')}
⚠️ <b>Risk:</b> {sig.get('risk_warning','N/A')}
💭 {sig.get('reasoning','')}

📈 Bot Stats: <b>{mem['win_rate']}%</b> win rate | {mem['total_signals']} signals
━━━━━━━━━━━━━━━━━━━━━━
<i>SMC AI Bot v3.1 | Not financial advice</i>"""

    return f"""{be} <b>SIGNAL #{sid} — {pd} {bias}</b>
━━━━━━━━━━━━━━━━━━━━━━
{session_emoji(session)} <b>{session}</b> {se} | {datetime.now(timezone.utc).strftime('%H:%M UTC')}

💰 <b>ENTRY:</b>  <code>{sig.get('entry_zone','N/A')}</code>
🛑 <b>SL:</b>     <code>{sig.get('stop_loss','N/A')}</code>
🎯 <b>TP1:</b>    <code>{sig.get('tp1','N/A')}</code>
🏆 <b>TP2:</b>    <code>{sig.get('tp2','N/A')}</code>
⚖️ <b>R:R:</b>    {sig.get('rr','N/A')}

━━━━━━━━━━━━━━━━━━━━━━
📊 <b>GREY LEVELS</b>
💵 Price:  <code>{lv.get('current','N/A')}</code>
🟢 PDH:    <code>{lv.get('pdh','N/A')}</code>  ({lv.get('pips_pdh','?')} pips)
🔴 PDL:    <code>{lv.get('pdl','N/A')}</code>  ({lv.get('pips_pdl','?')} pips)
⚪ EQ:     <code>{lv.get('eq','N/A')}</code>
🔑 Key:    {sig.get('key_level','N/A')}

━━━━━━━━━━━━━━━━━━━━━━
🧠 <b>AI ANALYSIS</b>
📐 Setup:  {sig.get('setup','N/A')}
📰 News:   {ae} {sig.get('news_alignment','N/A')}
{ce} <b>Confidence: {conf}%</b>

💭 {sig.get('reasoning','N/A')}
🏦 <i>{sig.get('smart_money_note','')}</i>
⚠️ {sig.get('risk_warning','N/A')}

━━━━━━━━━━━━━━━━━━━━━━
📈 <b>BOT STATS</b>
Win Rate: <b>{mem['win_rate']}%</b> | Signals: {mem['total_signals']}
Best: {mem.get('best_setup','Building…')}

<i>Reply /win{sid} or /loss{sid} to train the AI 🧠</i>
━━━━━━━━━━━━━━━━━━━━━━
<i>SMC AI Bot v3.1 | Not financial advice</i>"""

def fmt_stats(mem) -> str:
    def lines(d):
        return "\n".join([f"  {k}: {v['win_rate']}% ({v['wins']}W/{v['losses']}L)"
                          for k,v in d.items()]) or "  No data yet"
    return f"""📊 <b>AI BOT PERFORMANCE</b>
━━━━━━━━━━━━━━━━━━━━━━
🏆 Win Rate: <b>{mem['win_rate']}%</b>
📈 Signals: {mem['total_signals']} | ✅ {mem['wins']}W / ❌ {mem['losses']}L

<b>By Setup:</b>
{lines(mem['setup_stats'])}

<b>By Session:</b>
{lines(mem['session_stats'])}

<b>By Pair:</b>
{lines(mem['pair_stats'])}

🥇 Best:  {mem.get('best_setup','Building…')}
⚠️ Worst: {mem.get('worst_setup','Building…')}
━━━━━━━━━━━━━━━━━━━━━━
<i>Bot learns from every /win and /loss command</i>"""

# ══════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════

def tg(msg: str) -> bool:
    return tg_to(TELEGRAM_CHAT_ID, msg)

def tg_to(chat_id: str, msg: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"TG error: {e}"); return False

# ══════════════════════════════════════════════════════════════
# SCHEDULED TASKS
# ══════════════════════════════════════════════════════════════

def task_signals():
    log.info("🤖 AI Signal scan…")
    mem     = load_memory()
    session = get_session()
    news    = get_news(5)
    events  = get_events()
    for pair in PAIRS:
        lv = get_levels(pair)
        if lv.get("error"):
            continue
        sig = generate_signal(pair, lv, news, events, session, mem)
        sid = mem["total_signals"] + 1
        msg = fmt_signal(pair, sig, lv, session, mem, sid)
        if tg(msg) and sig.get("bias") in ("BUY","SELL"):
            mem = record_signal(mem, {
                "pair": pair, "bias": sig.get("bias"),
                "entry": sig.get("entry_zone"), "sl": sig.get("stop_loss"),
                "tp1": sig.get("tp1"), "tp2": sig.get("tp2"),
                "confidence": sig.get("confidence"),
                "setup": sig.get("setup"), "session": session,
            })
        time.sleep(3)

def task_news():
    log.info("📰 News scan…")
    for feed_url in ["https://www.forexlive.com/feed/news", "https://www.fxstreet.com/rss/news"]:
        try:
            for e in feedparser.parse(feed_url).entries[:8]:
                t = e.get("title",""); s = e.get("summary",""); l = e.get("link","")
                if t in seen_headlines: continue
                if any(k.lower() in t.lower() for k in HI_KW):
                    seen_headlines.add(t)
                    sent = sentiment(t+" "+s)
                    f    = {"BUY":"🟢","SELL":"🔴","WAIT":"⚪"}
                    tg(f"""⚡ <b>NEWS ALERT</b>
━━━━━━━━━━━━━━━━━━━━━━
📌 <b>{t}</b>
📝 {s[:180]}…
{f.get(sent['eurusd'],'⚪')} EURUSD: {sent['eurusd']}
{f.get(sent['gbpusd'],'⚪')} GBPUSD: {sent['gbpusd']}
🎯 Confidence: {sent['conf']}
🔗 <a href="{l}">Read more</a>
━━━━━━━━━━━━━━━━━━━━━━
<i>SMC AI Bot | Not financial advice</i>""")
                    time.sleep(1)
        except Exception as ex:
            log.warning(f"News error: {ex}")

def task_calendar():
    log.info("📅 Calendar check…")
    for ev in get_events():
        flag = {"USD":"🇺🇸","EUR":"🇪🇺","GBP":"🇬🇧"}.get(ev["country"],"🌐")
        tg(f"""🗓 <b>EVENT — {ev['minutes']} MIN WARNING</b>
━━━━━━━━━━━━━━━━━━━━━━
{flag} <b>{ev['country']} | {ev['title']}</b>
📊 Forecast: {ev['forecast']} | Previous: {ev['previous']}
⚠️ Close/widen stops. No entries within 5 min.
━━━━━━━━━━━━━━━━━━━━━━
<i>SMC AI Bot | Not financial advice</i>""")
        time.sleep(1)

def task_killzone():
    now = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    for name, oh, om in [("LONDON",7,0),("NEW YORK",12,0),("ASIA",0,0)]:
        if h == oh and abs(m - om) <= 1:
            lines = []
            for pair in PAIRS:
                lv = get_levels(pair)
                if not lv.get("error"):
                    lines.append(f"• {pair}: <code>{lv.get('current','?')}</code> | "
                                 f"PDH:<code>{lv.get('pdh','?')}</code> "
                                 f"PDL:<code>{lv.get('pdl','?')}</code> | "
                                 f"{lv.get('bias','?')}")
            tg(f"""🕐 <b>{session_emoji(name)} {name} KILL ZONE — OPEN</b>
━━━━━━━━━━━━━━━━━━━━━━
⚡ Highest manipulation probability window.

<b>Current Levels:</b>
{chr(10).join(lines) or "Fetching levels…"}

🎯 Plan: Wait for sweep → BOS → FVG retest → entry
━━━━━━━━━━━━━━━━━━━━━━
<i>SMC AI Bot | Not financial advice</i>""")
            break

def task_commands():
    global last_update_id
    try:
        r   = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                           params={"offset": last_update_id + 1}, timeout=10)
        mem = load_memory()
        changed = False

        for upd in r.json().get("result", []):
            last_update_id = upd["update_id"]
            msg_obj = upd.get("message", {})
            text    = msg_obj.get("text", "").strip().lower()
            chat_id = str(msg_obj.get("chat", {}).get("id", TELEGRAM_CHAT_ID))

            # Photo handler
            if "photo" in msg_obj:
                file_id = msg_obj["photo"][-1]["file_id"]
                with pending_lock:
                    if chat_id not in pending_images:
                        pending_images[chat_id] = []
                        threading.Thread(target=flush_pending_images, args=(chat_id,), daemon=True).start()
                    pending_images[chat_id].append({"file_id": file_id, "ts": time.time()})
                continue

            # Document handler (image sent as file)
            if "document" in msg_obj:
                doc = msg_obj["document"]
                if doc.get("mime_type","").startswith("image/"):
                    file_id = doc["file_id"]
                    with pending_lock:
                        if chat_id not in pending_images:
                            pending_images[chat_id] = []
                            threading.Thread(target=flush_pending_images, args=(chat_id,), daemon=True).start()
                        pending_images[chat_id].append({"file_id": file_id, "ts": time.time()})
                continue

            # Text commands
            if text.startswith("/win") or text.startswith("/loss"):
                outcome = "WIN" if text.startswith("/win") else "LOSS"
                try:
                    sid = int(text.replace("/win","").replace("/loss",""))
                    mem = record_outcome(mem, sid, outcome)
                    changed = True
                    tg_to(chat_id, f"✅ Signal #{sid} = <b>{outcome}</b>\nWin rate: <b>{mem['win_rate']}%</b> 🧠")
                except: pass

            elif text == "/stats":
                tg_to(chat_id, fmt_stats(mem))

            elif text == "/signal":
                tg_to(chat_id, "🤖 Generating signal now...")
                task_signals()

            elif text == "/session":
                s = get_session()
                tg_to(chat_id, f"🕐 <b>{datetime.now(timezone.utc).strftime('%H:%M UTC')}</b>\n📍 {session_emoji(s)} {s}")

            elif text == "/bias":
                tg_to(chat_id,
                      "📸 <b>Chart Bias Analysis</b>\n"
                      "━━━━━━━━━━━━━━━━━━━━━━\n"
                      "Send 1 chart → full bias report\n"
                      "Send 2 charts → SMT divergence read\n\n"
                      "Returns: bias, BOS/CHoCH, SMT divergence, "
                      "OBs, FVGs, invalidation, fundamentals, trade setup\n\n"
                      "Just send the image(s) now 👇")

            elif text in ("/start", "/help"):
                tg_to(chat_id,
                      "🤖 <b>SMC AI Bot v3.1</b>\n"
                      "━━━━━━━━━━━━━━━━━━━━━━\n"
                      "/signal  — AI signal now\n"
                      "/stats   — Performance report\n"
                      "/session — Current session\n"
                      "/bias    — Chart analysis guide\n"
                      "/win5    — Mark signal 5 WIN\n"
                      "/loss5   — Mark signal 5 LOSS\n\n"
                      "📸 Send a chart screenshot for instant SMC bias.")

        if changed: save_memory(mem)
    except Exception as e:
        log.warning(f"Command error: {e}")

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    log.info("🚀 SMC AI Bot v3.1 starting…")
    mem = load_memory()
    tg(f"""🚀 <b>SMC AI TRADING BOT v3.1 — ONLINE</b>
━━━━━━━━━━━━━━━━━━━━━━
✅ AI Signal Engine (every 4 hrs)
✅ News Scanner (every 5 min)
✅ Calendar Alerts (every 1 hr)
✅ Kill Zone Alerts
✅ Self-Learning Memory
✅ Chart Bias Analysis — just send a screenshot
✅ SMT Divergence — send 2 charts together

🧠 {mem['total_signals']} signals tracked | {mem['win_rate']}% win rate

/signal /stats /bias /session
{datetime.now(timezone.utc).strftime('%H:%M UTC | %d %b %Y')}
━━━━━━━━━━━━━━━━━━━━━━
<i>Gets smarter every day.</i>""")

    task_news()
    task_calendar()
    task_signals()

    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(task_signals,  "interval", hours=4,   id="signals")
    sched.add_job(task_news,     "interval", minutes=5, id="news")
    sched.add_job(task_calendar, "interval", hours=1,   id="calendar")
    sched.add_job(task_killzone, "interval", minutes=1, id="killzone")
    sched.add_job(task_commands, "interval", minutes=1, id="commands")

    log.info("✅ All systems running.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("🛑 Stopped.")

if __name__ == "__main__":
    main()
