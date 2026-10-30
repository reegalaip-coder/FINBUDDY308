"""
FinBuddy hourly alerts.

Runs every hour (see .github/workflows/hourly.yml). For every ticker in watchlist.txt
it checks the move since the previous close. If a stock, crypto, index or currency is
up or down ALERT_PCT (default 2%) or more, you get a Telegram message straight away.

No repeat spam: you are alerted once at 2%, again only if the move grows to 4%, 6%...
or flips direction. The alert memory is kept in state/alerts.json.

Information only, not financial advice.
"""
import os, json, time, datetime, warnings, requests, yfinance as yf

warnings.filterwarnings("ignore")

WATCHLIST_FILE = "watchlist.txt"
STATE_FILE = "state/alerts.json"
DEFAULT_TICKERS = ["AAPL", "MSFT", "CBA.AX", "BHP.AX", "BTC-USD"]
ALERT_PCT = float(os.environ.get("ALERT_PCT") or 2.0)   # change this to alert on bigger/smaller moves
NEWS_AGE_HOURS = 36


def read_watchlist():
    items = []
    if os.path.exists(WATCHLIST_FILE):
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
    return out


def fetch(t):
    for attempt in range(2):
        try:
            print(f"Fetching {t}...", flush=True)
            h = yf.Ticker(t).history(period="5d", timeout=15)
            close = h["Close"].dropna()
            if len(close) >= 2:
                return close
        except Exception as e:
            print(f"  {t} failed: {e}", flush=True)
        time.sleep(2)
    return None


def headline(t):
    """One recent headline for context (best effort)."""
    try:
        items = yf.Ticker(t).news or []
    except Exception:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    for it in items:
        c = it.get("content", it)
        title, ts = c.get("title"), c.get("pubDate") or c.get("providerPublishTime")
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
            return title
    return None


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def should_alert(chg, bar_date, old):
    """Alert at +/-ALERT_PCT, then again at each further ALERT_PCT step or on a reversal.
    Returns the new level to remember, or None for no alert."""
    step = int(abs(chg) // ALERT_PCT)
    if step == 0:
        return None
    level = step if chg > 0 else -step
    if not old or old.get("bar_date") != bar_date:
        return level                       # first alert for this trading day
    prev = old.get("level", 0)
    if (level > 0) != (prev > 0):
        return level                       # direction flipped
    if abs(level) > abs(prev):
        return level                       # move grew by another step
    return None


def check(tickers, state):
    """Returns (alerts, new_state, failed). Pure logic apart from fetch()."""
    alerts, failed = [], []
    new_state = dict(state)
    for t in tickers:
        close = fetch(t)
        if close is None:
            failed.append(t)
            continue
        price, prev = float(close.iloc[-1]), float(close.iloc[-2])
        chg = (price / prev - 1) * 100
        bar_date = close.index[-1].date().isoformat()   # the trading day this move belongs to
        level = should_alert(chg, bar_date, state.get(t))
        if level is not None:
            alerts.append({"ticker": t, "price": price, "prev": prev, "chg": chg})
            new_state[t] = {"bar_date": bar_date, "level": level}
    # forget old entries
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    new_state = {k: v for k, v in new_state.items() if v.get("bar_date", "") >= cutoff}
    return alerts, new_state, failed


def build_message(alerts):
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("Australia/Sydney")).strftime("%a %H:%M Sydney")
    except Exception:
        now = datetime.datetime.now().strftime("%H:%M")
    alerts = sorted(alerts, key=lambda a: abs(a["chg"]), reverse=True)
    blocks = [f"🚨 Watchlist alert ({now})"]
    for a in alerts:
        icon = "🟢" if a["chg"] > 0 else "🔴"
        word = "up" if a["chg"] > 0 else "down"
        lines = [f"{icon} {a['ticker']} {word} {abs(a['chg']):.1f}% on the day",
                 f"  Price {a['price']:,.2f} (previous close {a['prev']:,.2f})"]
        h = headline(a["ticker"])
        if h:
            lines.append(f"  📰 {h}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def send_telegram(text):
    resp = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000]},
        timeout=30,
    )
    print("Telegram:", resp.status_code, resp.text, flush=True)
    if resp.status_code == 404:
        print("404 = Telegram does not recognise TELEGRAM_TOKEN. Re-copy it from @BotFather.")
    resp.raise_for_status()


def main():
    tickers = read_watchlist()
    print(f"Checking {len(tickers)} tickers for moves of {ALERT_PCT}% or more: "
          f"{', '.join(tickers)}", flush=True)
    state = load_state()
    alerts, new_state, failed = check(tickers, state)
    if failed:
        print("No data for:", ", ".join(failed), flush=True)   # not messaged hourly, to avoid noise
    if alerts:
        send_telegram(build_message(alerts))
        save_state(new_state)           # only after a successful send, so a failed send retries next hour
    else:
        print("No new moves to report.", flush=True)
        if new_state != state:
            save_state(new_state)


if __name__ == "__main__":
    main()
