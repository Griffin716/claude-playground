#!/usr/bin/env python3
"""
Forward test (shadow trading) of the with-trend 0DTE short strategy on live data.

Rules (frozen from the backtest, 2021-10 .. 2026-10):
  * RSI(14) on completed 5-min SPY bars, regular hours.
  * RSI > 70 -> SELL the ATM put.  RSI < 30 -> SELL the ATM call.  (with-trend)
  * One more contract per further 5 RSI points inside the same episode (catch-up on).
  * New entries only 09:30-13:30 ET.
  * Exit on first of: buy back at 50% of credit (take profit), buy back at 2x credit
    (stop, i.e. loss = 100% of credit), 60 minutes open, or 15:55 ET flat.
  * Fills are logged at the live NBBO: sell at bid, buy back at ask, $0.65/contract/side.
    No real orders are sent. This is a paper record to compare with the backtest.

Data: SPY 1-min bars from Alpaca (free IEX feed), option NBBO from Theta Data
(Options Value plan, real-time). State lives in state.json, closed trades in
trades_live.csv, so the process can be killed and restarted at any minute.

Run:  python forward_test.py --until 12:30      (ET; loop exits at that time or 15:58)
      python forward_test.py --replay 2026-09-15 --bars spy_1min.csv.gz --options quotes.csv.gz
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, subprocess, sys, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
P = dict(rsi_period=14, upper=70.0, lower=30.0, step=5.0, take_profit=0.50, stop_loss=1.00,
         max_hold_min=60, last_entry="13:30", flat_time="15:55", commission=0.65, multiplier=100)
STATE, TRADES = "state.json", "trades_live.csv"
TRADE_COLS = ["entry_ts", "exit_ts", "right", "strike", "expiry", "entry_spot", "exit_spot", "entry_rsi",
              "ladder_level", "credit", "exit_cost", "ret_pct", "hold_min", "exit_reason", "pnl"]


# ----------------------------------------------------------------------------- data feeds
class LiveFeed:
    def __init__(self):
        import requests
        self.rq = requests
        missing = [k for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "THETADATA_API_KEY") if not os.environ.get(k, "").strip()]
        if missing:
            raise SystemExit(f"CONFIG ERROR: missing repository secret(s): {', '.join(missing)}")
        self.H = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"].strip(), "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"].strip()}
        # Alpaca: accept either paper or live keys
        self.alpaca_base = None
        for base in ("https://paper-api.alpaca.markets", "https://api.alpaca.markets"):
            r = self.rq.get(f"{base}/v2/clock", headers=self.H, timeout=20)
            if r.status_code == 200:
                self.alpaca_base = base; break
        if not self.alpaca_base:
            raise SystemExit(f"ALPACA ERROR: keys rejected (HTTP {r.status_code}: {r.text[:120]}). "
                             "Check APCA_API_KEY_ID / APCA_API_SECRET_KEY secrets (paper-trading keys are fine).")
        print(f"alpaca ok ({'paper' if 'paper' in self.alpaca_base else 'live'} keys)")
        try:
            from thetadata import ThetaClient
            self.theta = ThetaClient(api_key=os.environ["THETADATA_API_KEY"].strip(), dataframe_type="pandas")
        except Exception as e:
            raise SystemExit(f"THETADATA ERROR: could not log in with THETADATA_API_KEY: {e!r}")
        print("theta data ok")

    def market_open_today(self) -> tuple[bool, dt.time | None]:
        r = self.rq.get(f"{self.alpaca_base}/v2/clock", headers=self.H, timeout=20); r.raise_for_status()
        j = r.json(); nxt_close = pd.Timestamp(j["next_close"]).tz_convert(ET)
        today = dt.datetime.now(ET).date()
        return (nxt_close.date() == today), nxt_close.time()

    def bars(self, now: dt.datetime) -> pd.Series:
        """1-min closes (ET index) for the last 3 trading days up to now."""
        start = (now - dt.timedelta(days=6)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows, tok = [], None
        while True:
            prm = dict(timeframe="1Min", start=start, limit=10000, feed="iex", adjustment="raw", sort="asc")
            if tok: prm["page_token"] = tok
            r = self.rq.get("https://data.alpaca.markets/v2/stocks/SPY/bars", headers=self.H, params=prm, timeout=30)
            r.raise_for_status(); j = r.json(); rows += j.get("bars") or []; tok = j.get("next_page_token")
            if not tok: break
        df = pd.DataFrame(rows); ts = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET)
        s = pd.Series(df["c"].astype(float).values, index=ts).sort_index()
        return s[~s.index.duplicated(keep="last")]

    def quote(self, strike: float, expiry: dt.date, right: str):
        df = self.theta.option_snapshot_quote(symbol="SPY", expiration=expiry, strike=f"{strike:.2f}",
                                              right="call" if right == "C" else "put")
        if df is None or len(df) == 0: return None
        df.columns = [c.lower() for c in df.columns]; row = df.iloc[-1]
        bid, ask = float(row["bid"]), float(row["ask"])
        return (bid, ask) if ask > 0 and ask >= bid >= 0 else None


class ReplayFeed:
    """Replays one historical day from the backtest files, minute by minute, to validate the logic."""
    def __init__(self, day: str, bars_path: str, options_path: str):
        b = pd.read_csv(bars_path); ts = pd.to_datetime(b.timestamp, utc=True).dt.tz_convert(ET)
        self.all = pd.Series(b.close.astype(float).values, index=ts).sort_index()
        self.all = self.all[~self.all.index.duplicated(keep="last")]
        o = pd.read_csv(options_path, dtype={"expiry": str}).dropna()
        o["ts"] = pd.to_datetime(o.timestamp, utc=True).dt.tz_convert(ET)
        self.q = {k: g.set_index("ts")[["bid", "ask"]].sort_index() for k, g in o.groupby(["expiry", "strike", "right"])}
        self.day = pd.Timestamp(day).date(); self.now = None
    def market_open_today(self): return True, dt.time(16, 0)
    def bars(self, now): return self.all[self.all.index <= now]
    def quote(self, strike, expiry, right):
        g = self.q.get((expiry.strftime("%Y%m%d"), float(strike), right))
        if g is None: return None
        i = g.index.searchsorted(self.now, side="right") - 1
        if i < 0 or (self.now - g.index[i]) > pd.Timedelta(minutes=5): return None
        return float(g.bid.iloc[i]), float(g.ask.iloc[i])


# ----------------------------------------------------------------------------- strategy
def wilder_rsi(close: pd.Series, n=14) -> pd.Series:
    d = close.diff(); g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return (100 - 100 / (1 + g / l.replace(0, np.nan))).where(l != 0, 100.0)


def rsi_5min(closes: pd.Series) -> pd.Series:
    tod = closes.index - closes.index.normalize()
    rth = closes[(tod >= pd.Timedelta(hours=9, minutes=30)) & (tod < pd.Timedelta(hours=16))]
    five = rth.resample("5min", label="right", closed="right").last().dropna()
    t5 = five.index - five.index.normalize()
    five = five[(t5 > pd.Timedelta(hours=9, minutes=30)) & (t5 <= pd.Timedelta(hours=16))]
    return wilder_rsi(five, P["rsi_period"])


def load_state() -> dict:
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"day": None, "positions": [], "episode": None, "fired": [], "last_bar": None}


def save_state(s: dict):
    json.dump(s, open(STATE, "w"), indent=1, default=str)


def append_trade(row: dict):
    df = pd.DataFrame([row], columns=TRADE_COLS)
    df.to_csv(TRADES, mode="a", header=not os.path.exists(TRADES), index=False)


def git_commit(msg: str):
    if os.environ.get("GIT_COMMIT", "0") != "1": return
    try:
        subprocess.run(["git", "add", STATE, TRADES], check=False)
        subprocess.run(["git", "commit", "-q", "-m", msg], check=False)
        subprocess.run(["git", "push", "-q"], check=False)
    except Exception as e:
        print("git:", e)


def expiry_for(day: dt.date, feed) -> dt.date:
    # SPY expires every weekday now; if today is a holiday the job never runs. Use today.
    return day


def t_(s: str) -> dt.time: return dt.time(int(s[:2]), int(s[3:]))


def step(now: dt.datetime, feed, st: dict, verbose=True) -> bool:
    """One minute of logic. Returns False when the session is finished."""
    today = now.date()
    if st["day"] != str(today):
        if st["positions"]: print("WARN: positions carried from another day were dropped:", st["positions"])
        st.update(day=str(today), positions=[], episode=None, fired=[], last_bar=None)

    closes = feed.bars(now)
    spot = float(closes.iloc[-1])
    flat = now.time() >= t_(P["flat_time"])

    # ---- manage open positions every minute ----
    keep = []
    for pos in st["positions"]:
        q = feed.quote(pos["strike"], dt.date.fromisoformat(pos["expiry"]), pos["right"])
        ask = q[1] if q else None
        reason = None
        if ask is not None:
            r = 1 - ask / pos["credit"]
            if r >= P["take_profit"]: reason = "take_profit"
            elif r <= -P["stop_loss"]: reason = "stop_loss"
        entry_ts = dt.datetime.fromisoformat(pos["entry_ts"])
        if reason is None and (now - entry_ts) >= dt.timedelta(minutes=P["max_hold_min"]): reason = "time_1h"
        if reason is None and flat: reason = "eod_flat"
        if reason is None:
            keep.append(pos); continue
        if ask is None:
            ask = max(0.0, (spot - pos["strike"]) if pos["right"] == "C" else (pos["strike"] - spot)); reason += "_intrinsic"
        pnl = (pos["credit"] - ask) * P["multiplier"] - 2 * P["commission"]
        row = dict(entry_ts=pos["entry_ts"], exit_ts=now.isoformat(), right=pos["right"], strike=pos["strike"],
                   expiry=pos["expiry"], entry_spot=pos["entry_spot"], exit_spot=spot, entry_rsi=pos["entry_rsi"],
                   ladder_level=pos["ladder_level"], credit=pos["credit"], exit_cost=ask,
                   ret_pct=round((pos["credit"] - ask) / pos["credit"] * 100, 2),
                   hold_min=int((now - entry_ts).total_seconds() // 60), exit_reason=reason, pnl=round(pnl, 2))
        append_trade(row)
        if verbose: print(f"  CLOSE {reason:14s} {pos['right']} {pos['strike']} credit {pos['credit']:.2f} -> {ask:.2f}  P&L ${pnl:+.0f}")
        git_commit(f"close {pos['right']}{pos['strike']} {reason} {pnl:+.0f}")
    st["positions"] = keep
    if flat:
        save_state(st); return False

    # ---- signal on a newly completed 5-min bar ----
    rsi = rsi_5min(closes).dropna()
    done = rsi[(rsi.index <= now) & (rsi.index.date == today)]   # today's bars whose close time has passed
    if done.empty: save_state(st); return True
    bar_ts, rv = done.index[-1], float(done.iloc[-1])
    if st["last_bar"] == bar_ts.isoformat():
        save_state(st); return True
    st["last_bar"] = bar_ts.isoformat()
    if (now - bar_ts.to_pydatetime()) > dt.timedelta(minutes=3):
        print(f"  (late: acting on {bar_ts.time()} bar at {now.time()})")

    if rv > P["upper"]: side, level = "P", int((rv - P["upper"]) // P["step"])      # sell put with the uptrend
    elif rv < P["lower"]: side, level = "C", int((P["lower"] - rv) // P["step"])    # sell call with the downtrend
    else: side, level = None, None
    if side != st["episode"]:
        st["episode"], st["fired"] = side, []
    if verbose: print(f"{now.strftime('%H:%M')} SPY {spot:.2f}  RSI5 {rv:5.1f}  open {len(st['positions'])}")
    if side is None or now.time() > t_(P["last_entry"]):
        save_state(st); return True

    for lv in range(0, level + 1):
        if lv in st["fired"]: continue
        st["fired"].append(lv)
        strike = float(round(spot)); exp = expiry_for(today, feed)
        q = feed.quote(strike, exp, side)
        if not q or q[0] <= 0:
            print(f"  no quote for {side} {strike} {exp}; skipped"); continue
        st["positions"].append(dict(entry_ts=now.isoformat(), right=side, strike=strike, expiry=exp.isoformat(),
                                    credit=q[0], entry_spot=spot, entry_rsi=round(rv, 2), ladder_level=lv))
        if verbose: print(f"  SELL {side} {strike} exp {exp} @ bid {q[0]:.2f} (ask {q[1]:.2f})  level {lv}")
        git_commit(f"open {side}{strike} rsi {rv:.1f}")
    save_state(st); return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="15:58", help="ET time to stop looping")
    ap.add_argument("--replay", help="YYYY-MM-DD: replay a historical day instead of live")
    ap.add_argument("--bars"); ap.add_argument("--options")
    a = ap.parse_args()

    if a.replay:
        feed = ReplayFeed(a.replay, a.bars, a.options)
        for f in (STATE, TRADES):
            if os.path.exists(f): os.remove(f)
        st = load_state()
        day = pd.Timestamp(a.replay).tz_localize(ET)
        for m in range(390):
            now = (day + pd.Timedelta(hours=9, minutes=30 + m)).to_pydatetime()
            if now not in feed.all.index: continue
            feed.now = pd.Timestamp(now)
            if not step(now, feed, st, verbose=False): break
        if os.path.exists(TRADES): print(pd.read_csv(TRADES).to_string())
        else: print("no trades on", a.replay)
        return

    feed = LiveFeed()
    is_open, _ = feed.market_open_today()
    now = dt.datetime.now(ET)
    if not is_open or now.time() >= dt.time(15, 58):
        # after-hours self-test so a manual run still proves the wiring works
        try:
            s = feed.bars(now); print(f"spy bars ok: last close {s.iloc[-1]:.2f} at {s.index[-1]}")
            q = feed.quote(float(round(float(s.iloc[-1]))), now.date(), "P")
            print(f"option quote ok: {q}" if q else "option quote: none returned (normal outside market hours)")
        except Exception as e:
            print(f"self-test warning: {e!r}")
        print("market closed; nothing to do — credentials verified, next session runs automatically"); return
    st = load_state()
    stop_at = t_(a.until)
    print(f"forward test running until {a.until} ET")
    while True:
        now = dt.datetime.now(ET)
        if now.time() >= stop_at or now.time() >= dt.time(15, 58): break
        if now.time() < dt.time(9, 31):
            time.sleep(20); continue
        try:
            if not step(now, feed, st): break
        except Exception as e:
            print(f"{now.strftime('%H:%M')} error: {e!r}"); time.sleep(10)
        # sleep to the next minute boundary + 3s so the bar is complete at the feed
        nxt = (now + dt.timedelta(minutes=1)).replace(second=3, microsecond=0)
        time.sleep(max(1.0, (nxt - dt.datetime.now(ET)).total_seconds()))
    save_state(st); git_commit("end of session")
    print("session finished")


if __name__ == "__main__":
    main()
