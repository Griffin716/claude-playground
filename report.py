#!/usr/bin/env python3
"""Summarize trades_live.csv and compare with the backtest's per-trade expectation."""
import os, pandas as pd
BT = dict(avg=9.68, win=0.655, pf=1.34, avg_credit=1.52)   # backtest 2021-10..2026-10, same rules
if not os.path.exists("trades_live.csv"):
    print("no live trades yet"); raise SystemExit
t = pd.read_csv("trades_live.csv")
w, l = t[t.pnl > 0], t[t.pnl <= 0]
pf = w.pnl.sum() / abs(l.pnl.sum()) if len(l) and l.pnl.sum() else float("inf")
days = pd.to_datetime(t.exit_ts).dt.date.nunique()
print(f"Live: {len(t)} trades over {days} days | total ${t.pnl.sum():,.0f} | avg ${t.pnl.mean():.2f}/trade "
      f"| win {len(w)/len(t):.1%} | PF {pf:.2f} | avg credit ${t.credit.mean():.2f}")
print(f"Backtest expectation: avg ${BT['avg']:.2f}/trade | win {BT['win']:.1%} | PF {BT['pf']:.2f} | avg credit ${BT['avg_credit']:.2f}")
se = t.pnl.std() / max(1, len(t)) ** 0.5
print(f"Std error of live avg: ${se:.2f}  (need ~{int((2*t.pnl.std()/BT['avg'])**2) if len(t)>1 else '?'} trades for +/-50% precision on the mean)")
print(t.groupby("exit_reason").pnl.agg(["count", "sum", "mean"]).round(1).to_string())
