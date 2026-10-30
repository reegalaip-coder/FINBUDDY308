"""
FinBuddy: watchlist analysis.

Reads your stocks from watchlist.txt, checks each one, and messages you on Telegram
ONLY about the ones with something notable today (big move, unusual volume, new
52-week high/low, moving-average cross, overbought/oversold, earnings coming up).

On-demand: run the workflow manually and type tickers in the "tickers" box to get
a full analysis of those stocks right now, whether or not anything is unusual.

No buy/sell advice. This is information, not financial advice.
"""
import os, json, time, datetime, warnings, requests, yfinance as yf

warnings.filterwarnings("ignore")

# ---------- settings (edit these) ----------
WATCHLIST_FILE = "watchlist.txt"
DEFAULT_TICKERS = ["AAPL", "MSFT", "CBA.AX", "BHP.AX", "BTC-USD"]  # used if watchlist.txt is missing

MOVE_PCT = 3.0         # flag if price moved at least this % since the previous close
VOLUME_X = 2.0         # flag if volume is at least this many times the 20-day average
NEAR_52W_PCT = 1.0     # flag if within this % of the 52-week high or low
RSI_HIGH, RSI_LOW = 70, 30
EARNINGS_DAYS = 3      # flag if earnings are within this many days
MAX_NEWS = 3           # headlines shown per flagged stock
NEWS_AGE_HOURS = 36    # only headlines newer than this
SEND_WHEN_QUIET = True # send a short "nothing notable" message on quiet days

GEMINI_MODEL = "gemini-3.8-flash"
MODELS = [GEMINI_MODEL, "gemini-2.5-flash"]  # backup model if the first is overloaded


# ---------- reading the watchlist ----------
def read_watchlist():
    """Returns (tickers, manual). manual=True when tickers were typed into the workflow."""
    override = os.environ.get("TICKERS", "").strip()
    items = []
    manual = False
    if override:
        items = override.replace(",", " ").split()
        manual = True
    elif os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    items += line.replace(",", " ").split()
    if not items:
        items = list(DEFAULT_TICKERS)
    seen, out = set(), []
    for t in items:
        t = t.upper()
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out, manual


# ---------- fetching ----------
def fetch(t):
    for attempt in range(2):
        try:
            print(f"Fetching {t}...", flush=True)
            h = yf.Ticker(t).history(period="1y", timeout=15)
            if len(h) >= 2:
                return h
        except Exception as e:
            print(f"  {t} failed: {e}", flush=True)
        time.sleep(2)
    return None


def get_news(t):
    out = []
    try:
        items = yf.Ticker(t).news or []
    except Exception:
        return out
    now = datetime.datetime.now(datetime.timezone.utc)
    for it in items:
        c = it.get("content", it)
        title = c.get("title")
        ts = c.get("pubDate") or c.get("providerPublishTime")
        if not title:
            continue
        try:
            if isinstance(ts, (int, float)):
                when = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            else:
                when = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            continue
        if (now - when).total_seconds() <= NEWS_AGE_HOURS * 3600:
            out.append(title)
        if len(out) >= MAX_NEWS:
            break
    return out


def earnings_soon(t):
    try:
        cal = yf.Ticker(t).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        for d in dates or []:
            if isinstance(d, datetime.datetime):
                d = d.date()
            days = (d - datetime.date.today()).days
            if 0 <= days <= EARNINGS_DAYS:
                return d.isoformat(), days
    except Exception:
        pass
    return None


# ---------- analysis ----------
def num(x, nd=2):
    try:
        x = float(x)
        return None if x != x else round(x, nd)  # NaN -> None
    except Exception:
        return None


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down
    return num((100 - 100 / (1 + rs)).iloc[-1], 0)


def analyse(t, h):
    """Look at one stock's last year of prices and list anything notable."""
    close = h["Close"].dropna()
    if len(close) < 2:
        return None
    price, prev = float(close.iloc[-1]), float(close.iloc[-2])
    chg = (price / prev - 1) * 100
    r = {"ticker": t, "price": num(price), "change_pct": num(chg), "flags": []}
    f = r["flags"]

    if abs(chg) >= MOVE_PCT:
        f.append(f"{'Up' if chg > 0 else 'Down'} {abs(chg):.1f}% on the day")

    # volume vs the 20-day average (indices/currencies have none, so skipped)
    if "Volume" in h:
        vol = h["Volume"].dropna()
        if len(vol) >= 21 and float(vol.iloc[-1]) > 0:
            avg = float(vol.iloc[-21:-1].mean())
            if avg > 0:
                x = float(vol.iloc[-1]) / avg
                r["volume_x"] = num(x, 1)
                if x >= VOLUME_X:
                    f.append(f"Volume {x:.1f}x the 20-day average")

    # 52-week range
    if len(close) >= 120:
        yr = close.iloc[-252:]
        hi, lo = float(yr.max()), float(yr.min())
        r["range_pos_pct"] = num((price - lo) / (hi - lo) * 100, 0) if hi > lo else None
        if price >= hi:
            f.append("New 52-week high")
        elif price >= hi * (1 - NEAR_52W_PCT / 100):
            f.append("Within 1% of its 52-week high")
        if price <= lo:
            f.append("New 52-week low")
        elif price <= lo * (1 + NEAR_52W_PCT / 100):
            f.append("Within 1% of its 52-week low")

    # moving averages
    if len(close) >= 202:
        ma50 = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()
        r["vs_200d"] = "above" if price > float(ma200.iloc[-1]) else "below"
        if price > float(ma200.iloc[-1]) and prev <= float(ma200.iloc[-2]):
            f.append("Closed above its 200-day average")
        if price < float(ma200.iloc[-1]) and prev >= float(ma200.iloc[-2]):
            f.append("Closed below its 200-day average")
        now_up = float(ma50.iloc[-1]) > float(ma200.iloc[-1])
        was_up = float(ma50.iloc[-2]) > float(ma200.iloc[-2])
        if now_up and not was_up:
            f.append("50-day average crossed above the 200-day (golden cross)")
        if was_up and not now_up:
            f.append("50-day average crossed below the 200-day (death cross)")

    # RSI
    if len(close) >= 30:
        v = rsi(close)
        r["rsi"] = v
        if v is not None and v >= RSI_HIGH:
            f.append(f"RSI {v:.0f} (overbought zone)")
        if v is not None and v <= RSI_LOW:
            f.append(f"RSI {v:.0f} (oversold zone)")
    return r


# ---------- Gemini summary ----------
def ask_gemini(prompt):
    for model in MODELS:
        for attempt in range(4):
            try:
                g = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                    timeout=60,
                )
            except Exception as e:
                print(f"Gemini {model} attempt {attempt + 1} error: {e}", flush=True)
                time.sleep(5 * (attempt + 1))
                continue
            print(f"Gemini {model} attempt {attempt + 1} status:", g.status_code, flush=True)
            if g.status_code == 200:
                try:
                    return g.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                except Exception:
                    print("Gemini reply:", g.text[:500])
                    break
            if g.status_code in (429, 500, 503):  # busy: wait and retry
                time.sleep(5 * (attempt + 1))
                continue
            print("Gemini reply:", g.text[:500])
            break  # other error (bad key etc): try next model
    return None


def summarise(items):
    prompt = (
        "You are a careful financial news assistant. Below is data for stocks that look "
        "notable today (flags, price change, headlines). Write a short, factual update "
        "(max 150 words) saying what stands out and why it might matter. Use ONLY the data "
        "given. Do not invent news, give buy/sell advice or predict prices.\n"
        + json.dumps(items)
    )
    try:
        return ask_gemini(prompt)
    except Exception as e:
        print(f"Gemini failed: {e}", flush=True)
        return None


# ---------- message building ----------
def fmt(r, manual):
    icon = "🟢" if (r["change_pct"] or 0) >= 0 else "🔴"
    lines = [f"{icon} {r['ticker']}  {r['price']} ({(r['change_pct'] or 0):+.1f}%)"]
    for fl in r["flags"]:
        lines.append(f"  • {fl}")
    if manual:
        if not r["flags"]:
            lines.append("  • Nothing unusual today")
        bits = []
        if r.get("rsi") is not None:
            bits.append(f"RSI {r['rsi']:.0f}")
        if r.get("range_pos_pct") is not None:
            bits.append(f"at {r['range_pos_pct']:.0f}% of 52-week range")
        if r.get("vs_200d"):
            bits.append(f"{r['vs_200d']} 200-day avg")
        if bits:
            lines.append("  " + " · ".join(bits))
    for n in r.get("news", []):
        lines.append(f"  📰 {n}")
    return "\n".join(lines)


def send_telegram(text):
    chunks, cur = [], ""
    for part in text.split("\n\n"):
        if len(cur) + len(part) + 2 > 3800 and cur:
            chunks.append(cur)
            cur = part
        else:
            cur = f"{cur}\n\n{part}" if cur else part
    chunks.append(cur)
    for c in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
            data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": c},
            timeout=30,
        )
        print("Telegram:", resp.status_code, resp.text, flush=True)
        if resp.status_code == 404:
            print("404 = Telegram does not recognise TELEGRAM_TOKEN. Re-copy it from @BotFather "
                  "(no spaces, quotes or the word 'bot' in front).")
        resp.raise_for_status()


# ---------- main ----------
def main():
    tickers, manual = read_watchlist()
    print(f"Checking {len(tickers)} stocks ({'on-demand' if manual else 'watchlist'}): "
          f"{', '.join(tickers)}", flush=True)

    results, failed = [], []
    for t in tickers:
        h = fetch(t)
        r = analyse(t, h) if h is not None else None
        if r is None:
            failed.append(t)
            continue
        e = earnings_soon(t)
        if e:
            r["flags"].append("Earnings " + ("today" if e[1] == 0 else f"in {e[1]} day(s) ({e[0]})"))
        results.append(r)

    shown = results if manual else [r for r in results if r["flags"]]
    shown.sort(key=lambda r: abs(r["change_pct"] or 0), reverse=True)
    for r in shown:
        if r["flags"]:
            r["news"] = get_news(r["ticker"])

    today = datetime.date.today().isoformat()
    parts = []
    summary = None
    flagged = [r for r in shown if r["flags"]]
    if flagged:
        summary = summarise([{"ticker": r["ticker"], "price": r["price"],
                              "change_pct": r["change_pct"], "flags": r["flags"],
                              "headlines": r.get("news", [])} for r in flagged])

    if manual:
        parts.append(f"🔎 Analysis you asked for, {today}")
    elif flagged:
        parts.append(f"📈 Watchlist update {today}: {len(flagged)} of {len(results)} stocks notable")
    elif SEND_WHEN_QUIET and results:
        mover = max(results, key=lambda r: abs(r["change_pct"] or 0))
        parts.append(f"✅ Watchlist quiet {today}: nothing notable across {len(results)} stocks.\n"
                     f"Biggest mover: {mover['ticker']} ({(mover['change_pct'] or 0):+.1f}%)")
    if summary and parts:
        parts.append(summary)
    parts += [fmt(r, manual) for r in shown]
    if failed:
        parts.append("⚠️ Could not get data for: " + ", ".join(failed)
                     + "\n(check spelling; ASX needs .AX, e.g. CBA.AX)")

    if not manual:
        os.makedirs("data", exist_ok=True)
        with open(f"data/{today}.json", "w") as f:
            json.dump({"results": results, "failed": failed, "summary": summary}, f, indent=2)

    if parts:
        send_telegram("\n\n".join(parts))
    else:
        print("Nothing notable and quiet messages are off: no message sent.")


if __name__ == "__main__":
    main()
