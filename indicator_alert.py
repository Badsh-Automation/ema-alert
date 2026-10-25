"""
EMA 3rd Cross Signal -> Email alert (sirf alert, koi order nahi)
Pine Script indicator ka Python version. Timeframe: 1H
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

# ===== Settings (Pine Script wali same) =====
SYMBOL = os.getenv("SYMBOL", "GC=F")          # Gold futures (yfinance)
LABEL = os.getenv("LABEL", "XAUUSD")          # mail mein jo naam dikhe
FAST_LEN = 9
SLOW_LEN = 21
SIGNAL_ON = 3      # kitne number ke cross par signal
RESET_AT = 3       # kitne cross ke baad count wapas 1
PHASE_OFFSET = int(os.getenv("PHASE_OFFSET", "0"))  # TradingView se count match karne ke liye (0, 1 ya 2)

STATE_FILE = "state.json"
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


def compute_signals(close):
    """[(bar_index, 'BUY'/'SELL'), ...] return karta hai."""
    fast = ema_pine(close, FAST_LEN)
    slow = ema_pine(close, SLOW_LEN)
    n = PHASE_OFFSET
    result = []
    for i in range(1, len(close)):
        if np.isnan(fast[i]) or np.isnan(slow[i]) or np.isnan(fast[i - 1]) or np.isnan(slow[i - 1]):
            continue
        bull = fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
        bear = fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]
        if bull or bear:
            n = 1 if n >= RESET_AT else n + 1
            if n == SIGNAL_ON:
                result.append((i, "BUY" if bull else "SELL"))
    return result


def fetch_data():
    df = yf.download(SYMBOL, interval="1h", period="730d",
                     auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError("Data nahi mila (yfinance empty)")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("UTC")
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
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save_state(ts):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_signal_time": ts.isoformat()}, f)


def main():
    if os.getenv("TEST_MAIL") == "1":
        send_mail(f"{LABEL} test mail", "Setup sahi chal raha hai. Ye test mail hai.")
        print("Test mail bhej diya.")
        return

    df = fetch_data()
    close = df["Close"].to_numpy(dtype=float)
    sigs = compute_signals(close)
    print(f"Candles: {len(df)}, last closed candle: {df.index[-1]}, total signals: {len(sigs)}")
    for i, side in sigs[-6:]:
        t = df.index[i].astimezone(IST).strftime("%d-%b %H:%M IST")
        print("SIGNAL:", side, t, round(close[i], 2))
            
    state = load_state()
    if state is None:
        # pehli baar: purane signals ka mail nahi, bas baseline set karo
        base = df.index[sigs[-1][0]] if sigs else df.index[-1]
        save_state(base.to_pydatetime())
        print("Pehli run: baseline set hui, mail nahi bheja.")
        return

    last = pd.Timestamp(state["last_signal_time"])
    if last.tzinfo is None:
        last = last.tz_localize("UTC")

    new = [(i, s) for i, s in sigs if df.index[i] > last]
    for i, side in new:
        t = df.index[i]
        price = close[i]
        t_ist = t.astimezone(IST).strftime("%d-%b-%Y %H:%M IST")
        # candle ka start time hai, close = start + 1h
        t_close = (t + timedelta(hours=1)).astimezone(IST).strftime("%d-%b-%Y %H:%M IST")
        subject = f"{side} Signal | {LABEL} 1H | EMA 3rd Cross"
        body = (f"EMA 3rd Cross: {side}\n"
                f"Symbol: {LABEL} ({SYMBOL})\nTimeframe: 1H\n"
                f"Candle: {t_ist}  (close: {t_close})\n"
                f"Close price: {price:.2f}\n"
                f"EMA {FAST_LEN}/{SLOW_LEN}\n\n"
                f"Ye sirf alert hai, order nahi laga.")
        send_mail(subject, body)
        print("Mail sent:", side, t)
        save_state(t.to_pydatetime())

    if not new:
        print("Koi naya signal nahi.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
