import os, json, datetime, warnings, requests, yfinance as yf

warnings.filterwarnings("ignore")

TICKERS = ["AAPL", "MSFT", "^GSPC", "BTC-USD", "AUDUSD=X"]  # edit these
GEMINI_MODEL = "gemini-3.8-flash"  # check AI Studio for the current free model

# 1. Fetch market data
rows = []
for t in TICKERS:
    try:
        h = yf.Ticker(t).history(period="5d")
        if len(h) >= 2:
            chg = (h["Close"].iloc[-1] / h["Close"].iloc[-2] - 1) * 100
            rows.append({"ticker": t,
                         "price": round(float(h["Close"].iloc[-1]), 2),
                         "change_pct": round(float(chg), 2)})
    except Exception as e:
        print(f"Failed {t}: {e}")

# 2. Summarize with Gemini (retries, then falls back to raw data)
import time

fallback = "\n".join(f"{r['ticker']}: {r['price']} ({r['change_pct']:+}%)" for r in rows)
summary = fallback
prompt = ("Write a short, factual market update (max 120 words) from this data. "
          "No buy/sell advice.\n" + json.dumps(rows))
MODELS = [GEMINI_MODEL, "gemini-2.5-flash"]  # backup model if the first is overloaded


def ask_gemini():
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
                print(f"Gemini {model} attempt {attempt + 1} error: {e}")
                time.sleep(5 * (attempt + 1))
                continue
            print(f"Gemini {model} attempt {attempt + 1} status:", g.status_code)
            if g.status_code == 200:
                try:
                    return g.json()["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    print("Gemini reply:", g.text[:500])
                    break
            if g.status_code in (429, 500, 503):  # busy: wait and retry
                time.sleep(5 * (attempt + 1))
                continue
            print("Gemini reply:", g.text[:500])
            break  # other error (bad key etc): try next model
    return None


try:
    summary = ask_gemini() or fallback
except Exception as e:
    print(f"Gemini failed, using raw data: {e}")

# 3. Save data to the repo
today = datetime.date.today().isoformat()
os.makedirs("data", exist_ok=True)
with open(f"data/{today}.json", "w") as f:
    json.dump({"rows": rows, "summary": summary}, f, indent=2)

# 4. Send to Telegram
text = f"📈 Daily Update {today}\n\n{summary}"
if summary != fallback:
    text += f"\n\n{fallback}"

resp = requests.post(
    f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
    data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text},
    timeout=30,
)
print("Telegram:", resp.status_code, resp.text)
if resp.status_code == 404:
    print("404 = Telegram does not recognise TELEGRAM_TOKEN. Re-copy it from @BotFather "
          "(no spaces, quotes or the word 'bot' in front).")
resp.raise_for_status()
