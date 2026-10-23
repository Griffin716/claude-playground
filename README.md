# Forward test — with-trend 0DTE SPY short (shadow trading)

Logs what the strategy *would* have done each day at live NBBO prices. No orders are placed.
Rules are frozen in `forward_test.py` (RSI>70 sell ATM put / RSI<30 sell ATM call, ladder every 5 pts,
entries 09:30–13:30 ET, exit at 50% of credit / 2× credit / 60 min / 15:55 flat).

Backtest (2021-10 → 2026-10): +$32k, $9.68/trade, 65.5% win rate, PF 1.34, positive every year.
The forward test answers: does that hold on data the rules never saw?

## Setup (phone browser, ~15 min, one time)

1. **GitHub account** (free): https://github.com/signup
2. **New repository**: https://github.com/new → name `spy-forward-test` → **Public** (public repos get
   unlimited free Actions minutes; private ones get 2,000/month ≈ 8 trading days) → Create.
3. **Upload the files**: in the repo tap *Add file → Upload files* and upload `forward_test.py`, `report.py`,
   `README.md`. Commit.
4. **Workflow file** (needs a folder path, so create it by hand): *Add file → Create new file*, name it exactly
   `.github/workflows/forward_test.yml`, paste the contents of that file from this zip, commit.
5. **Secrets**: repo *Settings → Secrets and variables → Actions → New repository secret*, add three:
   `THETADATA_API_KEY`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`.
6. **Test it now**: *Actions* tab → `forward-test` → *Run workflow*. Outside market hours it prints
   "market closed today" and stops. During hours it starts logging.

From then on it runs itself every trading day (09:28 and 12:25 ET starts). Results accumulate in
`trades_live.csv` in the repo; the Actions log of each run ends with the report.

## Reading results
Open `trades_live.csv` on GitHub or send it to Claude. `report.py` prints live avg/trade, win rate and PF next
to the backtest's. With ~$60 per-trade standard deviation, you need ~150 trades (≈ 6–8 weeks) before the live
average is meaningfully different from zero, and ~500 before you can tell $5/trade from $10/trade.

## Costs
Theta Data Options Value $40/mo (real-time NBBO) + Alpaca free (SPY 1-min bars) + GitHub free.

## Known differences from the backtest
* GitHub cron can start a few minutes late. The script catches up on the first loop; late entries are flagged `(late: …)` in the log.
* Alpaca IEX bars are a subset of consolidated volume; RSI can differ slightly from the backtest's IEX history (same source, so consistent with it).
* Fills are NBBO, not real executions. Real stops on fast days would be worse.
