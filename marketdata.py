"""
Shared price helpers for agent.py and alerts.py.

Adds ready-made lines for gold and silver in rupees (and Australian dollars), built from
the futures prices (GC=F, SI=F) and the exchange rate. Put these in watchlist.txt like any
other ticker:

    GOLD-INR     gold in rupees per 10 grams (the usual Indian quote)
    SILVER-INR   silver in rupees per kg
    GOLD-AUD     gold in Australian dollars per gram
    SILVER-AUD   silver in Australian dollars per 10 grams

These are spot-style calculations: futures price x exchange rate x unit conversion.
They will NOT match a jeweller's price, which includes import duty, GST and margin.
"""
import pandas as pd
import yfinance as yf

OUNCE_G = 31.1034768  # grams in one troy ounce (futures are priced per troy ounce, in US dollars)

ALIASES = {
    "GOLD-INR":   {"base": "GC=F", "fx": "USDINR=X", "op": "mul", "factor": 10 / OUNCE_G,
                   "label": "Gold", "cur": "INR", "prefix": "₹", "unit": "per 10 g"},
    "SILVER-INR": {"base": "SI=F", "fx": "USDINR=X", "op": "mul", "factor": 1000 / OUNCE_G,
                   "label": "Silver", "cur": "INR", "prefix": "₹", "unit": "per kg"},
    "GOLD-AUD":   {"base": "GC=F", "fx": "AUDUSD=X", "op": "div", "factor": 1 / OUNCE_G,
                   "label": "Gold", "cur": "AUD", "prefix": "A$", "unit": "per gram"},
    "SILVER-AUD": {"base": "SI=F", "fx": "AUDUSD=X", "op": "div", "factor": 10 / OUNCE_G,
                   "label": "Silver", "cur": "AUD", "prefix": "A$", "unit": "per 10 g"},
}


def base_symbol(t):
    """The real Yahoo symbol behind a ticker (used for news lookups)."""
    return ALIASES[t]["base"] if t in ALIASES else t


def display(t):
    a = ALIASES.get(t)
    return f"{a['label']} in {a['cur']} {a['unit']}" if a else t


def _indian_group(whole):
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def money(x, t=""):
    """Format a price. Rupees get no decimals and Indian grouping (1,13,171)."""
    a = ALIASES.get(t)
    if a and a["cur"] == "INR":
        s = f"{abs(x):.0f}"
        return ("-" if x < 0 else "") + a["prefix"] + _indian_group(s)
    prefix = a["prefix"] if a else ""
    return f"{prefix}{x:,.2f}"


def _clean(s):
    s = s.dropna()
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_localize(None)
    s.index = s.index.normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def history(t, period="1y"):
    """Price history for a ticker. For gold/silver lines, converts the futures price."""
    a = ALIASES.get(t)
    if not a:
        return yf.Ticker(t).history(period=period, timeout=15)
    base = _clean(yf.Ticker(a["base"]).history(period=period, timeout=15)["Close"])
    fx = _clean(yf.Ticker(a["fx"]).history(period=period, timeout=15)["Close"])
    fx = fx.reindex(base.index, method="ffill")   # use the latest exchange rate on or before each date
    conv = base * fx * a["factor"] if a["op"] == "mul" else base / fx * a["factor"]
    return pd.DataFrame({"Close": conv.dropna()})
