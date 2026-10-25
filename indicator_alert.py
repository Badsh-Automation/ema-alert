"""
EMA 3rd Cross Signal -> Email alert (sirf alert, koi order nahi)
Multi-symbol, Timeframe: 1H. Symbols aur offset symbols.json mein.
"""
import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

FAST_LEN = 9
SLOW_LEN = 21
SIGNAL_ON = 3      # kitne number ke cross par signal
RESET_AT = 3       # kitne cross ke baad count wapas 1
START_DATE = os.getenv("START_DATE", "2026-01-01")  # ginti isi date se shuru hogi

STATE_FILE = "state.json"
CONFIG_FILE = "symbols.json"
IST = timezone(timedelta(hours=5, minutes=30))


def ema_pine(values, length):
    """TradingView ta.ema jaisa: pehli value SMA se seed hoti hai."""
    out = np.full(len(values), np.nan)
    if len(values) < length:
        return out
    out[length - 1] = values[:length].mean()
    a = 2.0 / (length + 1)
    for i in range(length, len(values)):
        out[i] = a * values[i] + (1 - a) * out[i - 1]
    return out


def find_crosses(close):
    """Saare EMA cross: [(bar_index, 'BUY'/'SELL'), ...]  (BUY = fast neeche se upar)."""
    fast = ema_pine(close, FAST_LEN)
    slow = ema_pine(close, SLOW_LEN)
    out = []
    for i in range(1, len(close)):
        if np.isnan(fast[i]) or np.isnan(slow[i]) or np.isnan(fast[i - 1]) or np.isnan(slow[i - 1]):
            continue
        if fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]:
            out.append((i, "BUY"))
        elif fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]:
            out.append((i, "SELL"))
    return out


def signals_after_anchor(crosses, anchor_idx):
    """Anchor (TradingView ka ek asli signal) ke baad ke cross gino; har 3rd par signal."""
    n = SIGNAL_ON
    out = []
    for i, side in crosses:
        if i <= anchor_idx:
            continue
        n = 1 if n >= RESET_AT else n + 1
        if n == SIGNAL_ON:
            out.append((i, side))
    return out


def fetch_data(ticker):
    df = yf.download(ticker, interval="1h", period="730d",
                     auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"Data nahi mila ({ticker})")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("UTC")
    df = df[df.index >= pd.Timestamp(START_DATE, tz="UTC")]
    # adhoori (chalti hui) candle hata do, sirf closed candles
    now = datetime.now(timezone.utc)
    if len(df) and df.index[-1] + timedelta(hours=1) > now:
        df = df.iloc[:-1]
    return df


def send_mail(subject, body):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.getenv("MAIL_TO") or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        raw = json.load(f)
    # purana format (sirf gold) -> naya format
    if "last_signal_time" in raw:
        return {"XAUUSD": raw["last_signal_time"]}
    return raw


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def fmt_price(p):
    return f"{p:.2f}" if p >= 10 else f"{p:.5f}"


def fmt_ist(t):
    return t.astimezone(IST).strftime("%d-%b %H:%M IST")


def process(label, cfg, state):
    ticker = cfg["ticker"]
    anchor = cfg.get("anchor")
    df = fetch_data(ticker)
    close = df["Close"].to_numpy(dtype=float)
    crosses = find_crosses(close)
    print(f"\n### {label} ({ticker}) candles={len(df)} last closed={df.index[-1]}")

    if not anchor:
        print("NOT CALIBRATED: mail band hai. TradingView par last signal ka exact time (IST) dhundo.")
        print("Neeche last 8 cross hain (TradingView ka signal inhi mein se ek hoga):")
        for i, side in crosses[-8:]:
            print("CROSS:", side, fmt_ist(df.index[i]), fmt_price(close[i]))
        return

    a_utc = pd.Timestamp(anchor["time"], tz="Asia/Kolkata").tz_convert("UTC")
    a_side = anchor["side"].upper()
    if a_utc not in df.index:
        raise RuntimeError(f"Anchor candle data mein nahi mili ({anchor['time']} IST). Data incomplete ho sakta hai, mail band.")
    a_idx = df.index.get_loc(a_utc)
    if (a_idx, a_side) not in crosses:
        near = [(fmt_ist(df.index[i]), sd) for i, sd in crosses[-8:]]
        raise RuntimeError(f"Anchor match nahi hua ({anchor['time']} IST {a_side}). Mail band. Last crosses: {near}")

    sigs = signals_after_anchor(crosses, a_idx)
    print("Anchor OK:", anchor["time"], "IST", a_side)
    for i, side in sigs[-3:]:
        print("SIGNAL:", side, fmt_ist(df.index[i]), fmt_price(close[i]))

    last_str = state.get(label)
    if last_str is None:
        base = df.index[sigs[-1][0]] if sigs else a_utc
        state[label] = base.isoformat()
        save_state(state)
        print("Pehli run: baseline set hui, mail nahi bheja.")
        return

    last = pd.Timestamp(last_str)
    if last.tzinfo is None:
        last = last.tz_localize("UTC")

    new = [(i, sd) for i, sd in sigs if df.index[i] > last]
    for i, side in new:
        t = df.index[i]
        subject = f"{side} Signal | {label} 1H | EMA 3rd Cross"
        body = (f"EMA 3rd Cross: {side}\n"
                f"Symbol: {label} ({ticker})\nTimeframe: 1H\n"
                f"Candle: {fmt_ist(t)}  (close: {fmt_ist(t + timedelta(hours=1))})\n"
                f"Close price: {fmt_price(close[i])}\n"
                f"EMA {FAST_LEN}/{SLOW_LEN}\n\n"
                f"Ye sirf alert hai, order nahi laga.")
        send_mail(subject, body)
        print("Mail sent:", label, side, t)
        state[label] = t.isoformat()
        save_state(state)
    if not new:
        print("Koi naya signal nahi.")


def main():
    if os.getenv("TEST_MAIL") == "1":
        send_mail("EMA alert test mail", "Setup sahi chal raha hai. Ye test mail hai.")
        print("Test mail bhej diya.")
        return

    with open(CONFIG_FILE) as f:
        config = json.load(f)
    state = load_state()

    failed = []
    for label, cfg in config.items():
        try:
            process(label, cfg, state)
        except Exception as e:
            print(f"ERROR {label}: {e}", file=sys.stderr)
            failed.append(label)
    if failed:
        print("Fail hue:", failed, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
