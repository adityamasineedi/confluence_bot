"""FastAPI metrics server — exposes trade stats and signal history via HTTP."""
import os
import sqlite3
import json as _json
import urllib.request
from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="confluence_bot metrics", version="0.1.0")

_DB_PATH = os.environ.get("DB_PATH", "confluence_bot.db")

# Live DataCache reference — set by main.py after cache is initialised.
# All endpoints that need live Coinglass data read from this.
_cache = None


def set_cache(cache) -> None:
    """Register the live DataCache instance. Called from main.py at startup."""
    global _cache
    _cache = cache


def _get_conn() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    except Exception:
        conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_db_regime(symbol: str) -> str | None:
    """Return the most recently logged regime for a symbol from DB, or None."""
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT regime FROM regimes WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                (symbol.upper(),),
            ).fetchone()
        return row["regime"] if row else None
    except Exception:
        return None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/signals/recent")
async def recent_signals(limit: int = 50) -> JSONResponse:
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return JSONResponse([dict(r) for r in rows])
    except Exception:
        return JSONResponse([])


@app.get("/trades/recent")
async def recent_trades(limit: int = 20) -> JSONResponse:
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return JSONResponse([dict(r) for r in rows])
    except Exception:
        return JSONResponse([])


@app.get("/stats/summary")
async def stats_summary() -> dict:
    try:
        with _get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            wins  = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE pnl_usdt > 0"
            ).fetchone()[0]
            pnl   = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdt),0) FROM trades"
            ).fetchone()[0]
            by_regime_rows = conn.execute(
                """SELECT regime, direction, COUNT(*) as cnt,
                          COALESCE(SUM(pnl_usdt),0) as pnl
                   FROM trades GROUP BY regime, direction"""
            ).fetchall()
            fired_today = conn.execute(
                """SELECT COUNT(*) FROM signals
                   WHERE fire=1 AND ts >= date('now')"""
            ).fetchone()[0]
        return {
            "total_trades":   total,
            "win_rate":       round(wins / total, 4) if total else 0.0,
            "total_pnl_usdt": round(pnl, 2),
            "fired_today":    fired_today,
            "by_regime":      [dict(r) for r in by_regime_rows],
        }
    except Exception:
        return {
            "total_trades": 0, "win_rate": 0.0,
            "total_pnl_usdt": 0.0, "fired_today": 0, "by_regime": [],
        }


@app.get("/trades/open")
async def open_trades() -> JSONResponse:
    """Return all currently open trades keyed by symbol."""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT symbol, direction, entry, stop_loss, take_profit, size, ts, regime "
                "FROM trades WHERE status='OPEN' ORDER BY ts DESC"
            ).fetchall()
        by_sym: dict[str, dict] = {}
        for r in rows:
            sym = r["symbol"]
            if sym not in by_sym:          # keep most recent open per symbol
                by_sym[sym] = dict(r)
        return JSONResponse(by_sym)
    except Exception:
        return JSONResponse({})


@app.get("/debug/{symbol}")
def debug_symbol(symbol: str) -> JSONResponse:
    """
    Live debug breakdown for one symbol:
    - Last 3 signal evaluations from DB (score, signals fired/missed, fire bool)
    - Live ADX, funding, EMA200 status, CVD warmup from Binance REST
    - Which filter gates pass/fail right now
    """
    sym = symbol.upper()

    # ── DB: last 3 signal rows ────────────────────────────────────────────────
    recent_signals: list[dict] = []
    try:
        import json as _json
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT ts, regime, direction, score, signals, fire "
                "FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 3",
                (sym,),
            ).fetchall()
        for r in rows:
            sigs = _json.loads(r["signals"]) if r["signals"] else {}
            fired  = [k for k, v in sigs.items() if v]
            missed = [k for k, v in sigs.items() if not v]
            recent_signals.append({
                "ts":        r["ts"],
                "regime":    r["regime"],
                "direction": r["direction"],
                "score":     round(r["score"], 4),
                "fire":      bool(r["fire"]),
                "fired":     fired,
                "missed":    missed,
            })
    except Exception as exc:
        recent_signals = [{"error": str(exc)}]

    # ── Live: fetch 4H candles for ADX + filter gates ─────────────────────────
    live: dict = {}
    try:
        candles_4h = _get_klines(sym, "4h", 40)
        candles_1d = _get_klines(sym, "1d", 10)
        if candles_4h:
            adx_info   = _calc_adx_live(candles_4h[-35:])
            closes_4h  = [c["c"] for c in candles_4h]
            ema200     = _calc_ema(closes_4h, 200) if len(closes_4h) >= 200 else 0.0
            price      = candles_4h[-1]["c"]
            adx_prev   = _calc_adx_live(candles_4h[-38:-3]) if len(candles_4h) >= 38 else adx_info
            adx_rising = adx_info["adx"] >= adx_prev["adx"]
            di_gap     = adx_info["plus_di"] - adx_info["minus_di"]
            di_gap_short = adx_info["minus_di"] - adx_info["plus_di"]

            # Daily bar direction
            daily_green = False
            if candles_1d and len(candles_1d) >= 1:
                d = candles_1d[-1]
                daily_green = d["c"] >= d["o"]

            # Funding rate
            funding = 0.0
            try:
                fund_data = _fetch_json(
                    f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&limit=1"
                )
                if fund_data:
                    funding = float(fund_data[0]["fundingRate"])
            except Exception:
                pass

            # 24h volume
            vol_24h = 0.0
            try:
                tk = _fetch_json(
                    f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}"
                )
                vol_24h = float(tk.get("quoteVolume", 0))
            except Exception:
                pass

            live = {
                "price":       round(price, 4),
                "adx":         round(adx_info["adx"], 2),
                "plus_di":     round(adx_info["plus_di"], 2),
                "minus_di":    round(adx_info["minus_di"], 2),
                "adx_rising":  adx_rising,
                "ema200":      round(ema200, 4) if ema200 else None,
                "above_ema200": (price > ema200) if ema200 else None,
                "daily_green": daily_green,
                "funding":     round(funding, 6),
                "vol_24h_m":   round(vol_24h / 1e6, 1),
                "filter_gates": {
                    "trend_long": {
                        "above_ema200":    (price > ema200) if ema200 else None,
                        "di_gap_gte5":     di_gap >= 5.0,
                        "adx_rising":      adx_rising,
                        "daily_green":     daily_green,
                        "funding_neutral": funding < 0.0003,
                        "vol_ok":          vol_24h >= 50_000_000,
                    },
                    "trend_short": {
                        "below_ema200":    (price < ema200) if ema200 else None,
                        "di_gap_gte5":     di_gap_short >= 5.0,
                        "adx_rising":      adx_rising,
                        "funding_not_panic": funding > -0.0003,
                        "vol_ok":          vol_24h >= 50_000_000,
                    },
                },
            }
    except Exception as exc:
        live = {"error": str(exc)}

    # ── CVD warmup ────────────────────────────────────────────────────────────
    cvd_warmup_remaining = 0.0
    try:
        from data.binance_ws import get_cvd_warmup_remaining as _cvd_remaining
        cvd_warmup_remaining = round(_cvd_remaining(sym), 1)
    except Exception:
        pass

    # ── Active deal status ────────────────────────────────────────────────────
    active_deal = None
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT direction, entry, stop_loss, take_profit, ts "
                "FROM trades WHERE symbol=? AND status='OPEN' ORDER BY ts DESC LIMIT 1",
                (sym,),
            ).fetchone()
        if row:
            active_deal = dict(row)
    except Exception:
        pass

    return JSONResponse({
        "symbol":               sym,
        "cvd_warmup_remaining": cvd_warmup_remaining,
        "cvd_ready":            cvd_warmup_remaining == 0.0,
        "active_deal":          active_deal,
        "recent_signals":       recent_signals,
        "live":                 live,
    })


@app.get("/regime/{symbol}")
async def current_regime(symbol: str) -> dict:
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT regime, ts FROM regimes WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                (symbol.upper(),),
            ).fetchone()
        if row:
            return {"symbol": symbol.upper(), "regime": row["regime"], "ts": row["ts"]}
    except Exception:
        pass
    return {"symbol": symbol.upper(), "regime": "UNKNOWN", "ts": None}


@app.get("/regimes/recent")
async def recent_regimes(limit: int = 20) -> JSONResponse:
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM regimes ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return JSONResponse([dict(r) for r in rows])
    except Exception:
        return JSONResponse([])


@app.get("/signals/live")
def signals_live() -> JSONResponse:
    """Live Binance snapshot for all 9 symbols — price, 24h change, funding, ADX, regime."""
    import datetime
    symbols = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","AVAXUSDT","ADAUSDT","DOTUSDT","DOGEUSDT","SUIUSDT"]
    result = []
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Bulk fetch 24h tickers in one call
    tickers: dict[str, dict] = {}
    try:
        raw = _fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
        tickers = {r["symbol"]: r for r in raw if r["symbol"] in symbols}
    except Exception:
        pass

    # Bulk fetch latest funding rates in one call
    fundings: dict[str, float] = {}
    try:
        raw_f = _fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
        for r in raw_f:
            if r["symbol"] in symbols:
                fundings[r["symbol"]] = float(r.get("lastFundingRate", 0))
    except Exception:
        pass

    for sym in symbols:
        tk  = tickers.get(sym, {})
        price    = float(tk.get("lastPrice", 0))
        chg_pct  = float(tk.get("priceChangePercent", 0))
        vol_24h  = float(tk.get("quoteVolume", 0))
        funding  = fundings.get(sym, 0.0)

        # ADX from 4h klines
        adx_val = 0.0
        plus_di = 0.0
        minus_di = 0.0
        try:
            c4h = _get_klines(sym, "4h", 40)
            if len(c4h) >= 30:
                adx_info = _calc_adx_live(c4h[-35:])
                adx_val  = adx_info["adx"]
                plus_di  = adx_info["plus_di"]
                minus_di = adx_info["minus_di"]
        except Exception:
            pass

        # Regime from DB
        regime = _get_db_regime(sym) or "UNKNOWN"

        # Build a signals dict reflecting live conditions
        funding_extreme  = abs(funding) >= 0.0005
        funding_positive = funding > 0
        signals = {
            "adx_trending":   adx_val >= 20,
            "adx_strong":     adx_val >= 25,
            "bull_di":        plus_di > minus_di,
            "bear_di":        minus_di > plus_di,
            "funding_extreme": funding_extreme,
            "funding_long":   funding_positive,
            "funding_short":  not funding_positive,
            "vol_ok":         vol_24h >= 50_000_000,
        }

        result.append({
            "ts":        now_iso,
            "symbol":    sym,
            "regime":    regime,
            "direction": "LONG" if plus_di >= minus_di else "SHORT",
            "score":     round(min(adx_val / 40.0, 1.0), 4),
            "signals":   signals,
            "fire":      False,
            "price":     round(price, 4),
            "chg_pct":   round(chg_pct, 2),
            "funding":   round(funding * 100, 4),
            "adx":       round(adx_val, 1),
            "vol_24h_m": round(vol_24h / 1e6, 1),
        })

    return JSONResponse(result)


# ── Live dashboard ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>confluence_bot dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', monospace; font-size: 14px; }

  /* ── Header ── */
  header { background: #1a1d27; padding: 0 20px; border-bottom: 2px solid #2a2d3a;
           display: flex; align-items: center; gap: 12px; height: 52px; flex-wrap: wrap; }
  .brand { font-size: 1.0rem; font-weight: 700; color: #a78bfa; margin-right: 8px; white-space: nowrap; }
  #status-dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e;
                box-shadow: 0 0 6px #22c55e; flex-shrink: 0; }
  .tabs { display: flex; gap: 4px; }
  .tab  { padding: 6px 16px; border-radius: 6px; font-size: 0.83rem; font-weight: 600;
          color: #9ca3af; background: #12141e; border: 1px solid #2a2d3a; cursor: pointer;
          transition: background .15s, color .15s, border-color .15s; white-space: nowrap; }
  .tab:hover  { color: #e0e0e0; background: #2a2d3a; border-color: #4b5563; }
  .tab.active { color: #fff; background: #4c1d95; border-color: #7c3aed;
                box-shadow: 0 0 8px rgba(124,58,237,0.4); }
  .hdr-right { margin-left: auto; font-size: 0.72rem; color: #4b5563; white-space: nowrap; }

  /* ── Panels ── */
  .panel { display: none; }
  .panel.active { display: block; }

  /* ── Shared ── */
  .green { color: #22c55e; } .red { color: #ef4444; }
  .blue  { color: #60a5fa; } .purple { color: #a78bfa; } .yellow { color: #fbbf24; }
  .gray  { color: #6b7280; }
  .pos { color: #22c55e; } .neg { color: #ef4444; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 0.7rem; font-weight: 600; }
  .badge-TREND    { background: #1d4ed8; color: #bfdbfe; }
  .badge-RANGE    { background: #713f12; color: #fef3c7; }
  .badge-CRASH    { background: #7f1d1d; color: #fecaca; }
  .badge-PUMP     { background: #14532d; color: #bbf7d0; }
  .badge-BREAKOUT { background: #1d4ed8; color: #bfdbfe; }
  .badge-LONG     { background: #14532d; color: #bbf7d0; }
  .badge-SHORT    { background: #7f1d1d; color: #fecaca; }
  .badge-FIRE     { background: #7c3aed; color: #ede9fe; }
  .badge-WIN      { background: #14532d; color: #bbf7d0; }
  .badge-LOSS     { background: #7f1d1d; color: #fecaca; }
  .badge-TIMEOUT  { background: #713f12; color: #fef3c7; }

  /* ── KPI cards (shared by signals + tradelog panels) ── */
  .tl-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px,1fr));
          gap: 16px; padding: 20px; }
  .tl-card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 18px; }
  .tl-card h3 { font-size: 0.75rem; color: #6b7280; text-transform: uppercase;
             letter-spacing: .06em; margin-bottom: 8px; }
  .tl-card .val { font-size: 2rem; font-weight: 700; }
  section { padding: 0 20px 20px; }
  section h2 { font-size: 0.85rem; color: #6b7280; text-transform: uppercase;
               letter-spacing: .06em; margin-bottom: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th { background: #1a1d27; color: #6b7280; padding: 8px 10px; text-align: left;
       border-bottom: 1px solid #2a2d3a; font-weight: 500; }
  td { padding: 8px 10px; border-bottom: 1px solid #1e2130; vertical-align: middle; }
  tr:hover td { background: #1e2130; }

  /* ── Feature pills ── */
  .feat-pill    { display: inline-block; padding: 1px 6px; border-radius: 3px;
                 font-size: 0.63rem; font-weight: 600; margin: 1px 2px 1px 0; white-space: nowrap; }
  .feat-on      { background: #14532d; color: #86efac; }
  .feat-off     { background: #1a1d27; color: #374151; border: 1px solid #2a2d3a; }
  .feat-nodata  { background: #0f1117; color: #1f2937; border: 1px solid #1a1d27;
                  font-style: italic; }

  /* ── Score bar ── */
  .score-wrap { display: flex; align-items: center; gap: 5px; min-width: 80px; }
  .score-pct  { font-size: 0.75rem; font-weight: 700; min-width: 28px; text-align: right; }
  .score-bar  { flex: 1; background: #12141e; border-radius: 3px; height: 6px;
                position: relative; }
  .score-fill { height: 100%; border-radius: 3px; transition: width .3s; }
  .score-thr  { position: absolute; top: -3px; bottom: -3px; width: 2px;
                background: #6b7280; border-radius: 1px; }

  /* ── Backtest panel ── */
  #panel-backtest .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px,1fr));
              gap: 14px; padding: 20px; }
  #panel-backtest .kpi { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 16px; }
  #panel-backtest .kpi label { display: block; font-size: 0.7rem; color: #6b7280;
               text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }
  #panel-backtest .kpi .v { font-size: 1.6rem; font-weight: 700; }
  #panel-backtest .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 0 20px 20px; }
  @media (max-width: 800px) { #panel-backtest .two-col { grid-template-columns: 1fr; } }
  #panel-backtest .bt-panel { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 16px; }
  #panel-backtest .bt-panel h2 { font-size: 0.75rem; color: #6b7280; text-transform: uppercase;
              letter-spacing: .05em; margin-bottom: 14px; }
  #panel-backtest .chart-wrap { position: relative; height: 240px; }
  #panel-backtest .full { padding: 0 20px 20px; }
  #panel-backtest table th { text-align: right; }
  #panel-backtest table th:first-child { text-align: left; }
  #panel-backtest table td { text-align: right; }
  #panel-backtest table td:first-child { text-align: left; font-weight: 500; }

  /* ── Market panel ── */
  #panel-market .mkt-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px,1fr));
           gap: 16px; padding: 20px; }
  .mkt-card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 12px; padding: 20px; }
  .mkt-card.bullish   { border-color: #166534; }
  .mkt-card.bearish   { border-color: #7f1d1d; }
  .mkt-card.pump      { border-color: #22c55e; box-shadow: 0 0 12px rgba(34,197,94,0.2); }
  .mkt-card.crash     { border-color: #f97316; box-shadow: 0 0 12px rgba(249,115,22,0.2); }
  .mkt-card.breakout  { border-color: #60a5fa; box-shadow: 0 0 12px rgba(96,165,250,0.15); }
  .sym-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
  .sym-name { font-size: 1.1rem; font-weight: 700; }
  .sym-price { font-size: 1.5rem; font-weight: 700; }
  .chg { font-size: 0.8rem; font-weight: 600; padding: 2px 7px; border-radius: 4px; }
  .chg.up { background: #14532d; color: #bbf7d0; }
  .chg.dn { background: #7f1d1d; color: #fecaca; }
  .signal-badge { display: inline-block; padding: 5px 14px; border-radius: 6px;
                  font-size: 0.85rem; font-weight: 700; margin-bottom: 14px; }
  .sig-long     { background: #14532d; color: #bbf7d0; }
  .sig-short    { background: #7f1d1d; color: #fecaca; }
  .sig-wait     { background: #1e2130; color: #6b7280; }
  .sig-crash    { background: #451a03; color: #fed7aa; }
  .sig-pump     { background: #14532d; color: #bbf7d0; border: 1px solid #22c55e; }
  .sig-breakout { background: #1e3a5f; color: #bfdbfe; }
  .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; }
  .metric { background: #12141e; border-radius: 6px; padding: 10px; }
  .metric .lbl { font-size: 0.65rem; color: #6b7280; text-transform: uppercase;
                 letter-spacing: .05em; margin-bottom: 3px; }
  .metric .val { font-size: 1.1rem; font-weight: 700; }
  .di-bar { margin-top: 4px; }
  .di-bar-track { background: #12141e; border-radius: 4px; height: 6px; margin-top: 3px; position: relative; }
  .di-plus  { background: #22c55e; height: 6px; border-radius: 4px; position: absolute; left: 0; }
  .di-minus { background: #ef4444; height: 6px; border-radius: 4px; position: absolute; right: 0; }
  .gates { font-size: 0.72rem; color: #6b7280; margin-top: 10px; }
  .gate-fail { color: #ef4444; }
  .gate-ok   { color: #22c55e; }
  .adx-slope { font-size: 0.7rem; margin-top: 6px; }
  .ema-line  { font-size: 0.7rem; color: #6b7280; margin-top: 4px; }
  .swing-section { margin-top: 10px; padding: 10px; background: #12141e;
                   border-radius: 6px; border: 1px solid #1e2130; }
  .swing-section .lbl { font-size: 0.65rem; color: #6b7280; text-transform: uppercase;
                        letter-spacing: .05em; margin-bottom: 6px; }
  .swing-row  { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
  .swing-pill { display: inline-block; padding: 2px 8px; border-radius: 4px;
                font-size: 0.72rem; font-weight: 700; }
  .pill-HH { background: #14532d; color: #86efac; }
  .pill-HL { background: #1c3a1c; color: #6ee37a; }
  .pill-LH { background: #3a1c1c; color: #fca5a5; }
  .pill-LL { background: #7f1d1d; color: #fecaca; }
  .conf-bar-wrap { background: #1a1d27; border-radius: 3px; height: 6px; margin-top: 3px; }
  .conf-bar-fill { height: 100%; border-radius: 3px; transition: width .3s; }
  .buy-zone-txt  { font-size: 0.68rem; color: #6b7280; margin-top: 5px; }
  .buy-zone-txt b { color: #60a5fa; }
  /* ── Coinglass section (inside market card) ── */
  .cg-section { margin-top: 10px; padding: 10px 12px; border-radius: 6px;
                background: #0d1520; border: 1px solid #1e3a5f; }
  .cg-section.cg-none { padding: 8px 10px; background: #0f1117; border-color: #1a1d27; }
  .cg-hdr  { font-size: 0.65rem; color: #60a5fa; text-transform: uppercase;
              letter-spacing: .06em; font-weight: 700; margin-bottom: 7px; }
  .cg-row  { display: flex; justify-content: space-between; align-items: baseline;
              margin: 3px 0; gap: 6px; }
  .cg-lbl  { font-size: 0.68rem; color: #4b5563; white-space: nowrap; flex-shrink: 0; }
  .cg-val  { font-size: 0.78rem; font-weight: 600; text-align: right; }

  .open-pos { margin-top: 10px; padding: 10px 12px; border-radius: 6px;
              border: 1px solid #1e3a1e; background: #0d1f0d; font-size: 0.75rem; }
  .open-pos .op-hdr { font-size: 0.65rem; color: #22c55e; text-transform: uppercase;
                      letter-spacing: .05em; margin-bottom: 6px; font-weight: 700; }
  .open-pos .op-row { display: flex; justify-content: space-between; margin: 2px 0; }
  .open-pos .op-lbl { color: #6b7280; }
  .open-pos .op-val { font-weight: 600; }
  .open-pos.short-pos { border-color: #3a1e1e; background: #1f0d0d; }
  .open-pos.short-pos .op-hdr { color: #ef4444; }
  #panel-market .mkt-footer { padding: 0 20px 30px; }
  .note { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 8px;
          padding: 14px 18px; font-size: 0.75rem; color: #4b5563; line-height: 1.6; }
</style>
</head>
<body>
<header>
  <div id="status-dot"></div>
  <span class="brand">confluence_bot</span>
  <nav class="tabs">
    <button class="tab active" onclick="showTab('signals',this)">&#9889; Signals</button>
    <button class="tab" onclick="showTab('trades',this)">&#9654; Trades</button>
    <button class="tab" onclick="showTab('regimes',this)">&#9685; Regimes</button>
    <button class="tab" onclick="showTab('market',this)">&#127758; Market</button>
    <button class="tab" onclick="showTab('backtest',this)">&#128202; Backtest</button>
    <button class="tab" onclick="showTab('debug',this)">&#128269; Debug</button>
  </nav>
  <span id="cvd-warmup" style="font-size:0.75rem;margin-left:8px">…</span>
  <span class="hdr-right" id="hdr-right">loading…</span>
</header>

<!-- ── SIGNALS ───────────────────────────────────────────── -->
<div id="panel-signals" class="panel active">
  <div class="tl-grid">
    <div class="tl-card"><h3>Total Trades</h3><div class="val blue" id="stat-trades">—</div></div>
    <div class="tl-card"><h3>Win Rate</h3><div class="val green" id="stat-winrate">—</div></div>
    <div class="tl-card"><h3>Total PnL (USDT)</h3><div class="val" id="stat-pnl">—</div></div>
    <div class="tl-card"><h3>Signals Fired Today</h3><div class="val purple" id="stat-fired">—</div></div>
  </div>
  <section style="padding-top:0">
    <h2>Live Signal Snapshot <span style="font-size:0.7rem;color:#4b5563;font-weight:400">— Binance live data</span></h2>
    <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Symbol</th><th>Price</th><th>24h</th><th>Funding %</th><th>ADX</th><th>Vol 24h (M)</th><th>Regime</th><th>Dir</th><th>Live Signals</th></tr></thead>
      <tbody id="signals-body"><tr><td colspan="9" style="color:#4b5563">loading…</td></tr></tbody>
    </table>
    </div>
  </section>
  <section>
    <h2>Recent Fired Signals <span style="font-size:0.7rem;color:#4b5563;font-weight:400">— from DB</span></h2>
    <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Time (IST)</th><th>Symbol</th><th>Regime</th><th>Dir</th><th>Score</th><th>Features</th><th>Fire</th></tr></thead>
      <tbody id="signals-fired-body"><tr><td colspan="7" style="color:#4b5563">loading…</td></tr></tbody>
    </table>
    </div>
  </section>
</div>

<!-- ── TRADES ────────────────────────────────────────────── -->
<div id="panel-trades" class="panel">
  <section style="padding-top:20px">
    <h2>Recent Trades</h2>
    <table>
      <thead><tr><th>Time</th><th>Symbol</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>Size</th><th>PnL</th><th>Status</th></tr></thead>
      <tbody id="trades-body"><tr><td colspan="9" style="color:#4b5563">loading…</td></tr></tbody>
    </table>
  </section>
</div>

<!-- ── REGIMES ───────────────────────────────────────────── -->
<div id="panel-regimes" class="panel">
  <section style="padding-top:20px">
    <h2>Current Regimes</h2>
    <table>
      <thead><tr><th>Symbol</th><th>Regime</th><th>Since</th></tr></thead>
      <tbody id="regime-body"><tr><td colspan="3" style="color:#4b5563">loading…</td></tr></tbody>
    </table>
  </section>
</div>

<!-- ── MARKET ───────────────────────────────────────────── -->
<div id="panel-market" class="panel">
  <div id="mkt-app" class="mkt-cards">
    <div style="padding:40px;color:#4b5563;grid-column:1/-1;text-align:center">Click Market tab to load…</div>
  </div>
  <div class="mkt-footer">
    <div class="note">
      <b>5 Regimes</b> &mdash;
      <span class="green">&#9650; PUMP</span>: price above EMA50(1D) + 7d gain &gt;12% + new highs &bull;
      <span class="red">&#9660; CRASH</span>: below EMA50(1D) + 7d drop &gt;12% + new lows &bull;
      <span style="color:#60a5fa">&#8658; BREAKOUT</span>: ADX 18-30 + price &gt;1% outside 20-bar range + vol spike &bull;
      <span class="yellow">&#8644; TREND</span>: ADX &gt;25, DI confirms direction &bull;
      <span class="blue">&#8651; RANGE</span>: ADX &lt;20.
      Gates (TREND only): EMA200 &bull; ADX rising &bull; Daily bar confirms direction.
    </div>
  </div>
</div>

<!-- ── BACKTEST ──────────────────────────────────────────── -->
<div id="panel-backtest" class="panel">
  <div id="bt-meta" style="padding:14px 20px 0;font-size:0.75rem;color:#4b5563">—</div>
  <div id="bt-app">
    <div style="padding:40px;color:#4b5563;text-align:center">Click Backtest tab to load…</div>
  </div>
</div>

<!-- ── DEBUG ─────────────────────────────────────────────────── -->
<div id="panel-debug" class="panel">
  <div style="padding:16px 20px 0">
    <select id="debug-sym" style="background:#1a1d27;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 12px;font-size:0.85rem">
      <option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option>
      <option>BNBUSDT</option><option>AVAXUSDT</option><option>ADAUSDT</option>
      <option>DOTUSDT</option><option>DOGEUSDT</option><option>SUIUSDT</option>
    </select>
    <button onclick="loadDebug()" style="margin-left:8px;padding:6px 14px;background:#4c1d95;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:0.85rem">Refresh</button>
    <span id="debug-cvd" style="margin-left:16px;font-size:0.8rem;color:#6b7280"></span>
  </div>
  <div id="debug-app" style="padding:16px 20px">
    <div style="color:#4b5563">Select a symbol and click Refresh</div>
  </div>
</div>

<script>
const ALL_SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','AVAXUSDT','ADAUSDT','DOTUSDT','DOGEUSDT','SUIUSDT'];

// ── Tab switching ─────────────────────────────────────────────────────────────
let mktLoaded = false, btLoaded = false, mktTimer = null;
document.getElementById('debug-sym').addEventListener('change', loadDebug);

function showTab(name, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  else {
    const match = document.querySelector(`.tab[onclick*="'${name}'"]`);
    if (match) match.classList.add('active');
  }
  history.replaceState(null, '', '#' + name);
  if (name === 'market') {
    if (!mktLoaded) { mktLoaded = true; loadMarket(); }
    if (!mktTimer) mktTimer = setInterval(loadMarket, 30000);
  } else {
    if (mktTimer) { clearInterval(mktTimer); mktTimer = null; }
  }
  if (name === 'backtest' && !btLoaded) { btLoaded = true; loadBacktest(); }
}

(function initHash() {
  const tab = (location.hash || '#signals').slice(1);
  if (tab !== 'signals') showTab(tab, null);
})();

// ── Shared helpers ────────────────────────────────────────────────────────────
async function fetchJSON(url) { const r = await fetch(url); return r.json(); }
function badge(cls, text) { return `<span class="badge badge-${text}">${text}</span>`; }

// ── SIGNALS / TRADES / REGIMES (shared refresh) ───────────────────────────────
async function refreshTradelog() {
  const [stats, liveSignals, firedSignals, trades] = await Promise.all([
    fetchJSON('/stats/summary'),
    fetchJSON('/signals/live'),
    fetchJSON('/signals/recent?limit=20'),
    fetchJSON('/trades/recent?limit=20'),
  ]);
  document.getElementById('stat-trades').textContent  = stats.total_trades;
  document.getElementById('stat-winrate').textContent = (stats.win_rate * 100).toFixed(1) + '%';
  const pnlEl = document.getElementById('stat-pnl');
  pnlEl.textContent = (stats.total_pnl_usdt >= 0 ? '+' : '') + stats.total_pnl_usdt.toFixed(2);
  pnlEl.className = 'val ' + (stats.total_pnl_usdt >= 0 ? 'green' : 'red');
  document.getElementById('stat-fired').textContent = stats.fired_today;

  const regimes = await Promise.all(ALL_SYMBOLS.map(s => fetchJSON('/regime/' + s)));
  document.getElementById('regime-body').innerHTML = regimes.map(r =>
    `<tr><td>${r.symbol}</td><td>${badge('regime', r.regime)}</td><td>${toIST(r.ts)}</td></tr>`
  ).join('');

  // Regime → fire threshold map (mirrors config.yaml thresholds)
  const THRESHOLDS = {
    TREND_LONG: 0.65, TREND_SHORT: 0.82,
    RANGE_LONG: 0.60, RANGE_SHORT: 0.65,
    CRASH_SHORT: 0.75,
    PUMP_LONG: 0.50,
    BREAKOUT_LONG: 0.60, BREAKOUT_SHORT: 0.75,
  };
  function scoreThr(regime, dir) {
    const key = regime + '_' + dir;
    return THRESHOLDS[key] || 0.65;
  }
  function scoreBar(score, thr) {
    const pct = Math.round(score * 100);
    const fillW = Math.min(pct, 100);
    const thrW  = Math.round(thr * 100);
    const col   = pct >= thrW ? '#22c55e' : pct >= thrW * 0.75 ? '#fbbf24' : '#6b7280';
    return `<div class="score-wrap">
      <span class="score-pct" style="color:${col}">${pct}%</span>
      <div class="score-bar">
        <div class="score-fill" style="width:${fillW}%;background:${col}"></div>
        <div class="score-thr"  style="left:${thrW}%"></div>
      </div>
    </div>`;
  }
  function livePills(signals) {
    // For live snapshot: only show TRUE signals as green pills
    if (!signals || !Object.keys(signals).length) return '<span style="color:#4b5563">—</span>';
    const on = Object.entries(signals).filter(([,v]) => v)
      .map(([k]) => `<span class="feat-pill feat-on">${k.replace(/_/g,' ')}</span>`);
    return on.length ? on.join(' ') : '<span style="color:#4b5563">—</span>';
  }
  function featPills(signalsJson, availJson) {
    let obj = {}, avail = null;
    try { obj   = typeof signalsJson === 'string' ? JSON.parse(signalsJson) : (signalsJson || {}); } catch(e) {}
    try { avail = availJson ? (Array.isArray(availJson) ? availJson : JSON.parse(availJson)) : null; } catch(e) {}
    const entries = Object.entries(obj);
    if (!entries.length) return '<span style="color:#4b5563">—</span>';
    const on      = entries.filter(([,v]) => v)
                           .map(([k]) => `<span class="feat-pill feat-on" title="${k}">${k.replace(/_/g,' ')}</span>`);
    const off     = entries.filter(([k,v]) => !v && (!avail || avail.includes(k)))
                           .map(([k]) => `<span class="feat-pill feat-off" title="${k}">${k.replace(/_/g,' ')}</span>`);
    const nodata  = entries.filter(([k,v]) => !v && avail && !avail.includes(k))
                           .map(([k]) => `<span class="feat-pill feat-nodata" title="no data: ${k}">${k.replace(/_/g,' ')}</span>`);
    return on.join('') + off.join('') + nodata.join('');
  }
  // Live Binance snapshot table
  document.getElementById('signals-body').innerHTML = liveSignals.length
    ? liveSignals.map(s => {
        const chgCls = s.chg_pct >= 0 ? 'green' : 'red';
        const chgStr = (s.chg_pct >= 0 ? '+' : '') + s.chg_pct.toFixed(2) + '%';
        const fundCls = Math.abs(s.funding) >= 0.05 ? (s.funding > 0 ? 'red' : 'green') : 'gray';
        const fundStr = (s.funding >= 0 ? '+' : '') + s.funding.toFixed(4) + '%';
        const adxCls  = s.adx >= 25 ? 'green' : s.adx >= 15 ? 'yellow' : 'gray';
        return `<tr>
          <td><b>${s.symbol}</b></td>
          <td style="font-weight:600">${(+s.price).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:4})}</td>
          <td class="${chgCls}">${chgStr}</td>
          <td class="${fundCls}">${fundStr}</td>
          <td class="${adxCls}">${s.adx}</td>
          <td class="gray">${s.vol_24h_m}M</td>
          <td>${badge('regime', s.regime)}</td>
          <td>${badge('dir', s.direction)}</td>
          <td style="max-width:300px">${livePills(s.signals)}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="9" style="color:#4b5563">loading live data…</td></tr>';

  // Fired signals from DB
  document.getElementById('signals-fired-body').innerHTML = firedSignals.length
    ? firedSignals.map(s => {
        const thr = scoreThr(s.regime, s.direction);
        return `<tr>
          <td style="white-space:nowrap;color:#6b7280">${toIST(s.ts)}</td>
          <td><b>${s.symbol}</b></td>
          <td>${badge('regime', s.regime)}</td>
          <td>${badge('dir', s.direction)}</td>
          <td>${scoreBar(+s.score, thr)}</td>
          <td style="max-width:340px">${featPills(s.signals, s.available)}</td>
          <td>${s.fire ? '<span class="badge badge-FIRE">&#9889; FIRE</span>' : '<span style="color:#374151;font-size:.7rem">—</span>'}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="7" style="color:#4b5563">no signals yet</td></tr>';

  document.getElementById('trades-body').innerHTML = trades.length
    ? trades.map(t => `<tr>
        <td>${toISTTime(t.ts)}</td><td>${t.symbol}</td>
        <td>${badge('dir', t.direction)}</td>
        <td>${(+t.entry).toFixed(2)}</td><td>${(+t.stop_loss).toFixed(2)}</td>
        <td>${(+t.take_profit).toFixed(2)}</td><td>${(+t.size).toFixed(4)}</td>
        <td style="color:${t.pnl_usdt>0?'#22c55e':t.pnl_usdt<0?'#ef4444':'#6b7280'}">${t.pnl_usdt!=null?(+t.pnl_usdt).toFixed(2):'open'}</td>
        <td>${t.status}</td>
      </tr>`).join('')
    : '<tr><td colspan="9" style="color:#4b5563">no trades yet</td></tr>';

  document.getElementById('hdr-right').textContent = 'updated ' + new Date().toLocaleTimeString();

  // CVD warmup status — fetch debug for BTC as representative
  try {
    const dbg = await fetchJSON('/debug/BTCUSDT');
    const warm = dbg.cvd_warmup_remaining || 0;
    const warmEl = document.getElementById('cvd-warmup');
    if (warmEl) {
      if (warm > 0) {
        const mins = Math.ceil(warm / 60);
        warmEl.textContent = `⏳ CVD warmup ${mins}m`;
        warmEl.style.color = '#fbbf24';
      } else {
        warmEl.textContent = '✓ CVD ready';
        warmEl.style.color = '#22c55e';
      }
    }
  } catch(e) {}
}
refreshTradelog();
setInterval(refreshTradelog, 5000);

// ── MARKET ────────────────────────────────────────────────────────────────────
const REGIME_META = {
  TREND:    { icon: '&#8644;', cls: 'yellow' },
  RANGE:    { icon: '&#8651;', cls: 'blue'   },
  CRASH:    { icon: '&#9660;', cls: 'red'    },
  PUMP:     { icon: '&#9650;', cls: 'green'  },
  BREAKOUT: { icon: '&#8658;', cls: 'purple' },
};
function mktFmt(n, dec=2) { return (+n).toLocaleString('en',{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
function mktFmtPct(v) { return (v>=0?'+':'')+mktFmt(v,2)+'%'; }
function regimeBadge(d) {
  const r = d.regime, dir = d.direction, sig = d.signal;
  if (r==='PUMP')     return `<span class="signal-badge sig-pump">&#9650; PUMP — LONG ENTRY (${d.change_7d_pct>0?'+':''}${d.change_7d_pct}% 7d)</span>`;
  if (r==='CRASH')    return `<span class="signal-badge sig-crash">&#9888; CRASH — SHORT (${d.change_7d_pct}% 7d)</span>`;
  if (r==='BREAKOUT') return `<span class="signal-badge sig-breakout">&#8658; BREAKOUT ${dir} — Volume confirmed</span>`;
  if (sig.includes('LONG'))  return `<span class="signal-badge sig-long">&#8679; TREND LONG — all gates passed</span>`;
  if (sig.includes('SHORT')) return `<span class="signal-badge sig-short">&#8681; TREND SHORT — all gates passed</span>`;
  if (r==='RANGE')    return `<span class="signal-badge sig-wait">&#8651; RANGE — watching boundaries</span>`;
  return `<span class="signal-badge sig-wait">&#9711; WAIT — gates not met</span>`;
}
function diBar(pdi, mdi) {
  const total = Math.max(pdi + mdi, 1);
  const pw = Math.round(pdi / total * 100);
  const mw = Math.round(mdi / total * 100);
  return `<div class="di-bar">
    <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:#6b7280">
      <span class="green">+DI ${pdi}</span><span class="red">-DI ${mdi}</span></div>
    <div class="di-bar-track">
      <div class="di-plus" style="width:${pw}%"></div>
      <div class="di-minus" style="width:${mw}%"></div>
    </div></div>`;
}
function cardClass(d) {
  if (d.regime==='PUMP')     return 'pump';
  if (d.regime==='CRASH')    return 'crash';
  if (d.regime==='BREAKOUT') return 'breakout';
  if (d.signal.includes('LONG'))  return 'bullish';
  if (d.signal.includes('SHORT')) return 'bearish';
  return 'neutral';
}
function pumpExtra(d) {
  if (d.regime!=='PUMP') return '';
  return `<div style="background:#0d1f0f;border:1px solid #166534;border-radius:6px;padding:10px;margin-bottom:12px;font-size:0.75rem;">
    <b class="green">&#9650; Parabolic pump detected</b><br>
    7-day gain: <b class="green">+${d.change_7d_pct}%</b> &nbsp;|&nbsp;
    EMA50(1D): <b>$${mktFmt(d.ema50_1d,0)}</b> &nbsp;|&nbsp; Price making new highs &#10003;</div>`;
}
function breakoutExtra(d) {
  if (d.regime!=='BREAKOUT') return '';
  const col=d.direction==='LONG'?'green':'red', arrow=d.direction==='LONG'?'&#8679;':'&#8681;';
  return `<div style="background:#0d1a2f;border:1px solid #1e3a5f;border-radius:6px;padding:10px;margin-bottom:12px;font-size:0.75rem;">
    <b class="${col}">${arrow} Range breakout — ${d.direction}</b><br>
    ADX transitioning: <b>${d.adx}</b> &nbsp;|&nbsp; Volume spike confirmed &#10003;</div>`;
}
function crashExtra(d) {
  if (d.regime!=='CRASH') return '';
  return `<div style="background:#1f0a00;border:1px solid #7f1d1d;border-radius:6px;padding:10px;margin-bottom:12px;font-size:0.75rem;">
    <b class="red">&#9660; Crash regime active</b><br>
    7-day drop: <b class="red">${d.change_7d_pct}%</b> &nbsp;|&nbsp;
    EMA50(1D): <b>$${mktFmt(d.ema50_1d,0)}</b> &nbsp;|&nbsp; Price at new lows &#10003;</div>`;
}
function openPosSection(pos) {
  if (!pos) return '';
  const isLong  = pos.direction === 'LONG';
  const cls     = isLong ? '' : 'short-pos';
  const arrow   = isLong ? '&#8679;' : '&#8681;';
  const col     = isLong ? '#22c55e' : '#ef4444';
  const pd = +pos.entry >= 100 ? 2 : +pos.entry >= 1 ? 4 : 6;
  const fp = n => (+n).toLocaleString('en', {minimumFractionDigits: pd, maximumFractionDigits: pd});
  const since = pos.ts ? new Date(pos.ts).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
  return `<div class="open-pos ${cls}">
    <div class="op-hdr">${arrow} Open ${pos.direction} — ${pos.regime || ''} &nbsp;<span style="color:#4b5563;font-weight:400">${since} IST</span></div>
    <div class="op-row"><span class="op-lbl">Entry</span><span class="op-val" style="color:${col}">${fp(pos.entry)}</span></div>
    <div class="op-row"><span class="op-lbl">Stop</span><span class="op-val red">${fp(pos.stop_loss)}</span></div>
    <div class="op-row"><span class="op-lbl">Target</span><span class="op-val green">${fp(pos.take_profit)}</span></div>
    <div class="op-row"><span class="op-lbl">Size</span><span class="op-val">${(+pos.size).toFixed(4)}</span></div>
  </div>`;
}
function swingSection(d) {
  const s = d.swing;
  if (!s || !s.structure || !s.structure.length) return '';
  const conf    = Math.round(s.buy_confidence * 100);
  const confCol = conf >= 75 ? '#22c55e' : conf >= 50 ? '#fbbf24' : '#ef4444';
  const confLbl = conf >= 75 ? 'Strong bullish' : conf >= 50 ? 'Neutral' : 'Bearish bias';
  const pills   = s.structure.map(lbl =>
    `<span class="swing-pill pill-${lbl}" title="${lbl==='HH'?'Higher High':lbl==='HL'?'Higher Low':lbl==='LH'?'Lower High':'Lower Low'}">${lbl}</span>`
  ).join('');
  const pd = d.price >= 100 ? 0 : d.price >= 1 ? 2 : 4;
  const fp = n => n > 0 ? '$' + mktFmt(n, pd) : '—';
  return `<div class="swing-section">
    <div class="lbl">Swing Structure &amp; Buy Zone (4H)</div>
    <div class="swing-row">${pills}
      <span style="margin-left:auto;font-size:0.7rem;color:${confCol};font-weight:700">${conf}% &mdash; ${confLbl}</span>
    </div>
    <div style="margin-top:7px">
      <div style="display:flex;justify-content:space-between;font-size:0.65rem;color:#6b7280;margin-bottom:2px">
        <span>Buy confidence</span><span style="color:${confCol}">${conf}%</span></div>
      <div class="conf-bar-wrap"><div class="conf-bar-fill" style="width:${conf}%;background:${confCol}"></div></div>
    </div>
    <div class="buy-zone-txt" style="margin-top:6px">
      Buy zone: <b>${fp(s.buy_zone_low)}</b> &mdash; <b>${fp(s.buy_zone_high)}</b>
      &nbsp;&#124;&nbsp; Resistance: <b>${fp(s.last_pivot_high)}</b>
    </div>
  </div>`;
}
function liqClusterRow(c, label) {
  if (!c) return `<div class="cg-row"><span class="cg-lbl">${label}</span><span class="cg-val gray">—</span></div>`;
  const col   = c.side === 'buy' ? '#22c55e' : '#ef4444';
  const arrow = c.side === 'buy' ? '▲' : '▼';
  const prec  = c.price >= 100 ? 0 : c.price >= 1 ? 2 : 4;
  const pFmt  = c.price.toLocaleString('en',{minimumFractionDigits:prec,maximumFractionDigits:prec});
  return `<div class="cg-row">
    <span class="cg-lbl">${label}</span>
    <span class="cg-val" style="color:${col}">${arrow} $${pFmt}
      <span style="color:#6b7280;font-weight:400"> ${c.dist_pct}% away · $${c.size_m}M</span>
    </span>
  </div>`;
}
function lsBiasTag(bias, ratio) {
  if (!bias || ratio === null || ratio === undefined) return '<span style="color:#6b7280">—</span>';
  if (bias === 'crowded_long')  return `<span style="color:#ef4444;font-weight:700">${ratio.toFixed(2)} ▲ crowded LONG</span>`;
  if (bias === 'crowded_short') return `<span style="color:#22c55e;font-weight:700">${ratio.toFixed(2)} ▼ crowded SHORT</span>`;
  return `<span style="color:#9ca3af">${ratio.toFixed(2)} neutral</span>`;
}
function oiTag(pct) {
  if (pct === null || pct === undefined) return '<span style="color:#6b7280">—</span>';
  const col = pct > 2 ? '#22c55e' : pct < -2 ? '#ef4444' : '#9ca3af';
  return `<span style="color:${col};font-weight:700">${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%</span>`;
}
function coinglassSection(d) {
  if (!d.coinglass_live) {
    return `<div class="cg-section cg-none"><span style="color:#374151;font-size:0.68rem">Coinglass — no data yet (API key required)</span></div>`;
  }
  return `<div class="cg-section">
    <div class="cg-hdr">&#128200; Coinglass</div>
    <div class="cg-row"><span class="cg-lbl">OI 24h</span><span class="cg-val">${oiTag(d.oi_change_pct)}</span></div>
    <div class="cg-row"><span class="cg-lbl">L/S Ratio</span><span class="cg-val">${lsBiasTag(d.ls_bias, d.ls_ratio)}</span></div>
    ${liqClusterRow(d.liq_below, 'Liq support ↓')}
    ${liqClusterRow(d.liq_above, 'Liq resist ↑')}
  </div>`;
}
function renderMarket(data, openPos) {
  openPos = openPos || {};
  document.getElementById('mkt-app').innerHTML = ALL_SYMBOLS.map(sym => {
    const d = data[sym]; if (!d) return '';
    const pos = openPos[sym] || null;
    const chgCls = d.change_24h >= 0 ? 'up' : 'dn';
    const aboveEma = d.price > d.ema200 && d.ema200 > 0;
    const emaDec = d.ema200 >= 100 ? 0 : d.ema200 >= 1 ? 2 : 4;
    const emaTxt = d.ema200 > 0
      ? `Price ${aboveEma?'&#8679; above':'&#8681; below'} EMA200 ($${mktFmt(d.ema200, emaDec)})`
      : 'EMA200 (4H) not available';
    const fundCls = Math.abs(d.funding_pct) > 0.05 ? 'red' : 'green';
    const adxCls  = d.adx > 40 ? 'red' : d.adx > 25 ? 'yellow' : 'green';
    const adxSlope = d.adx_rising
      ? '<span class="green">&#8679; rising</span>'
      : '<span class="red">&#8681; declining</span>';
    let gateHtml;
    if (['PUMP','CRASH','BREAKOUT'].includes(d.regime)) {
      gateHtml = d.gate_notes.map(g=>`<span style="color:#6b7280">${g}</span>`).join(' &nbsp;&bull;&nbsp; ');
    } else {
      gateHtml = d.gate_notes.length
        ? d.gate_notes.map(g=>`<span class="gate-fail">&#10005; ${g}</span>`).join(' ')
        : '<span class="gate-ok">&#10003; All gates passed</span>';
    }
    return `<div class="mkt-card ${cardClass(d)}">
      <div class="sym-header">
        <div><div class="sym-name">${sym.replace('USDT','')}</div>
          <div class="sym-price ${d.change_24h>=0?'green':'red'}">$${mktFmt(d.price,d.price>100?0:2)}</div></div>
        <div style="text-align:right">
          <span class="chg ${chgCls}">${mktFmtPct(d.change_24h)}</span>
          <div style="font-size:0.7rem;color:#4b5563;margin-top:4px">Vol $${mktFmt(d.volume_24h_m,0)}M</div>
          <div style="font-size:0.68rem;color:#6b7280;margin-top:2px">7d ${d.change_7d_pct>0?'<span class="green">+':'<span class="red">'}${d.change_7d_pct}%</span></div>
        </div>
      </div>
      ${regimeBadge(d)}${pumpExtra(d)}${crashExtra(d)}${breakoutExtra(d)}
      <div class="metrics">
        <div class="metric"><div class="lbl">Regime</div>
          <div class="val ${(REGIME_META[d.regime]||{cls:'gray'}).cls}">${(REGIME_META[d.regime]||{icon:''}).icon} ${d.regime}</div></div>
        <div class="metric"><div class="lbl">ADX (4H)</div>
          <div class="val ${adxCls}">${d.adx}</div></div>
        <div class="metric"><div class="lbl">Funding Rate</div>
          <div class="val ${fundCls}">${d.funding_pct>0?'+':''}${d.funding_pct}%</div></div>
        <div class="metric"><div class="lbl">Direction</div>
          <div class="val ${d.direction==='LONG'?'green':d.direction==='SHORT'?'red':'gray'}">${d.direction}</div></div>
      </div>
      ${diBar(d.plus_di, d.minus_di)}
      <div class="adx-slope">ADX slope: ${adxSlope}</div>
      <div class="ema-line">${emaTxt}</div>
      <div class="gates">${gateHtml}</div>
      ${coinglassSection(d)}
      ${openPosSection(pos)}
      ${swingSection(d)}
    </div>`;
  }).join('');
}
async function loadMarket() {
  document.getElementById('hdr-right').textContent = 'fetching…';
  try {
    const [r, ro] = await Promise.all([fetch('/market/data'), fetch('/trades/open')]);
    const d = await r.json();
    const openPos = ro.ok ? await ro.json() : {};
    if (d.error) throw new Error(d.error);
    renderMarket(d, openPos);
    document.getElementById('hdr-right').textContent = `updated ${new Date().toLocaleTimeString()} — refreshing every 30s`;
  } catch(e) {
    document.getElementById('mkt-app').innerHTML =
      `<div style="padding:40px;color:#ef4444;grid-column:1/-1;text-align:center">Error: ${e.message}</div>`;
    document.getElementById('hdr-right').textContent = 'error — retrying…';
  }
}

// ── BACKTEST ──────────────────────────────────────────────────────────────────
let eqChart = null, barChart = null;
function btPnlCls(v) { return +v >= 0 ? 'pos' : 'neg'; }
function btPnlFmt(v) { return (+v>=0?'+':'')+'$'+(+v).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function btPctFmt(v) { return (+v>=0?'+':''),(+v).toFixed(1)+'%'; }
function toIST(ts) {
  if (!ts) return '—';
  return new Date(typeof ts==='number'?ts:ts).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}
function toISTDate(ts) {
  if (!ts) return '—';
  return new Date(typeof ts==='number'?ts:ts).toLocaleDateString('en-IN',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'});
}
function toISTTime(ts) {
  if (!ts) return '—';
  return new Date(typeof ts==='number'?ts:ts).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}
function btTsDate(ms) { return toISTDate(ms); }
function btProfitFactor(trades) {
  const w = trades.filter(t=>t.pnl>0).reduce((s,t)=>s+t.pnl,0);
  const l = Math.abs(trades.filter(t=>t.pnl<0).reduce((s,t)=>s+t.pnl,0));
  return l===0 ? 'inf' : (w/l).toFixed(2);
}
function btBucketTable(data) {
  const rows = Object.entries(data).sort();
  if (!rows.length) return '<p style="color:#4b5563;padding:10px">no data</p>';
  return `<table>
    <thead><tr><th>Name</th><th>Trades</th><th>W/L/T</th><th>WR</th><th>PnL</th></tr></thead>
    <tbody>${rows.map(([name,b])=>`<tr>
      <td>${name}</td><td>${b.trades}</td><td>${b.wins}/${b.losses}/${b.timeouts}</td>
      <td class="${b.win_rate>=0.4?'pos':'neg'}">${(b.win_rate*100).toFixed(1)}%</td>
      <td class="${btPnlCls(b.pnl)}">${btPnlFmt(b.pnl)}</td>
    </tr>`).join('')}</tbody></table>`;
}
function btMonthlyTable(monthly, sc) {
  const annuals = {};
  monthly.forEach(m => {
    const y = m.month.slice(0,4);
    if (!annuals[y]) annuals[y] = {pnl:0,trades:0,wins:0,start_eq:m.start_eq};
    annuals[y].pnl+=m.pnl; annuals[y].trades+=m.trades; annuals[y].wins+=m.wins; annuals[y].end_eq=m.end_eq;
  });
  const monthRows = monthly.map(m => {
    const cls = m.pnl>=0?'pos':'neg';
    return `<tr><td>${m.month}</td><td>${m.trades}</td><td>${m.wins}/${m.losses}/${m.timeouts}</td>
      <td class="${m.wins/Math.max(m.trades,1)>=0.4?'pos':'neg'}">${m.trades?Math.round(m.wins/m.trades*100)+'%':'—'}</td>
      <td class="${cls}">${btPnlFmt(m.pnl)}</td>
      <td>$${(+m.end_eq).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
      <td class="${cls}" style="font-weight:700">${btPctFmt(m.pct)}</td></tr>`;
  }).join('');
  const annualRows = Object.entries(annuals).sort().map(([y,a]) => {
    const ret = a.pnl/a.start_eq*100;
    return `<tr style="background:#12141e;font-weight:600"><td>${y} TOTAL</td><td>${a.trades}</td><td>—</td>
      <td class="${a.wins/Math.max(a.trades,1)>=0.4?'pos':'neg'}">${Math.round(a.wins/a.trades*100)}%</td>
      <td class="${btPnlCls(a.pnl)}">${btPnlFmt(a.pnl)}</td>
      <td>$${(+a.end_eq).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
      <td class="${btPnlCls(ret)}" style="font-weight:700">${btPctFmt(ret)}</td></tr>`;
  }).join('');
  return `<table><thead><tr><th>Month</th><th>Trades</th><th>W/L/T</th><th>WR</th>
    <th>PnL</th><th>Equity</th><th>Return%</th></tr></thead>
    <tbody>${monthRows}${annualRows}</tbody></table>`;
}
function btTradeTable(trades) {
  return `<table><thead><tr>
    <th>Date</th><th>Symbol</th><th>Dir</th><th>Regime</th>
    <th>Score</th><th>Risk$</th><th>Outcome</th><th>PnL</th><th>Equity</th>
  </tr></thead><tbody>${trades.map(t=>`<tr>
    <td>${btTsDate(t.exit_ts)}</td><td>${t.symbol}</td>
    <td style="color:${t.direction==='LONG'?'#22c55e':'#ef4444'}">${t.direction}</td>
    <td>${t.regime}</td><td>${(+t.score).toFixed(2)}</td>
    <td>$${(+t.risk_amount).toFixed(1)}</td>
    <td><span class="badge badge-${t.outcome}">${t.outcome}</span></td>
    <td class="${btPnlCls(t.pnl)}">${btPnlFmt(t.pnl)}</td>
    <td>$${t.equity_after?(+t.equity_after).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2}):'—'}</td>
  </tr>`).join('')}</tbody></table>`;
}
function buildEquityCurve(monthly, sc) {
  if (eqChart) { eqChart.destroy(); eqChart = null; }
  let eq = sc; const labels=['Start'], data=[sc];
  monthly.forEach(m => { eq+=m.pnl; labels.push(m.month); data.push(+eq.toFixed(2)); });
  eqChart = new Chart(document.getElementById('eq-chart'), {
    type: 'line',
    data: { labels, datasets: [{ data, fill:true, borderColor:'#a78bfa',
      backgroundColor:'rgba(167,139,250,0.1)', pointRadius:2, tension:0.3 }] },
    options: { responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{display:false} },
      scales: {
        x:{ ticks:{color:'#6b7280',maxTicksLimit:12}, grid:{color:'#1e2130'} },
        y:{ ticks:{color:'#6b7280',callback:v=>'$'+v.toLocaleString()}, grid:{color:'#1e2130'} },
      } },
  });
}
function buildBarChart(monthly) {
  if (barChart) { barChart.destroy(); barChart = null; }
  const labels=monthly.map(m=>m.month.slice(0,7));
  const data=monthly.map(m=>+m.pct.toFixed(2));
  barChart = new Chart(document.getElementById('bar-chart'), {
    type: 'bar',
    data: { labels, datasets: [{ data, backgroundColor:data.map(v=>v>=0?'#22c55e':'#ef4444'), borderRadius:3 }] },
    options: { responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{display:false} },
      scales: {
        x:{ ticks:{color:'#6b7280',maxTicksLimit:14}, grid:{color:'#1e2130'} },
        y:{ ticks:{color:'#6b7280',callback:v=>v+'%'}, grid:{color:'#1e2130'} },
      } },
  });
}
async function loadBacktest() {
  let d;
  try {
    const r = await fetch('/backtest/results');
    if (!r.ok) throw new Error(await r.text());
    d = await r.json();
  } catch(e) {
    document.getElementById('bt-app').innerHTML =
      `<div style="padding:40px;color:#ef4444;text-align:center">${e.message}</div>`;
    return;
  }
  const t=d.stats.total, monthly=d.stats.monthly||[], byReg=d.stats.by_regime||{},
        bySym=d.stats.by_symbol||{}, trades=d.trades||[], sc=d.capital||1000;
  document.getElementById('bt-meta').textContent =
    `${d.symbols?.join(', ')} | Capital $${sc.toLocaleString()} | Risk ${(d.risk_pct*100).toFixed(0)}%/trade`;
  const pf = btProfitFactor(trades);
  const kpis = [
    { label:'Starting Capital', val:'$'+sc.toLocaleString(), cls:'blue' },
    { label:'Final Equity',     val:'$'+(+t.final_equity).toLocaleString('en',{minimumFractionDigits:2}), cls:t.final_equity>=sc?'green':'red' },
    { label:'Total Return',     val:(t.total_return_pct>=0?'+':'')+t.total_return_pct+'%', cls:t.total_return_pct>=0?'green':'red' },
    { label:'Total Trades',     val:t.trades, cls:'blue' },
    { label:'Win Rate',         val:(t.win_rate*100).toFixed(1)+'%', cls:'purple' },
    { label:'Profit Factor',    val:pf, cls:parseFloat(pf)>=1.5?'green':'yellow' },
    { label:'Max Drawdown',     val:'$'+(+t.max_drawdown_usd).toFixed(0)+' ('+t.max_drawdown_pct+'%)', cls:'red' },
    { label:'Avg Win / Loss',   val:btPnlFmt(t.avg_win)+' / '+btPnlFmt(t.avg_loss), cls:'blue' },
  ];
  document.getElementById('bt-app').innerHTML = `
  <div class="kpi-grid">${kpis.map(k=>`
    <div class="kpi"><label>${k.label}</label><div class="v ${k.cls}">${k.val}</div></div>`).join('')}</div>
  <div class="two-col">
    <div class="bt-panel"><h2>Equity Curve</h2>
      <div class="chart-wrap"><canvas id="eq-chart"></canvas></div></div>
    <div class="bt-panel"><h2>Monthly Return %</h2>
      <div class="chart-wrap"><canvas id="bar-chart"></canvas></div></div>
  </div>
  <div class="two-col">
    <div class="bt-panel"><h2>By Regime</h2>${btBucketTable(byReg)}</div>
    <div class="bt-panel"><h2>By Symbol</h2>${btBucketTable(bySym)}</div>
  </div>
  <div class="full">
    <div class="bt-panel"><h2>Monthly Returns</h2>${btMonthlyTable(monthly,sc)}</div>
  </div>
  <div class="full">
    <div class="bt-panel"><h2>Last 20 Trades</h2>${btTradeTable(trades.slice(-20).reverse())}</div>
  </div>`;
  buildEquityCurve(monthly, sc);
  buildBarChart(monthly);
}

// ── DEBUG ──────────────────────────────────────────────────────────────────────
async function loadDebug() {
  const sym = document.getElementById('debug-sym').value;
  const app = document.getElementById('debug-app');
  app.innerHTML = '<div style="color:#4b5563;padding:20px">Loading…</div>';
  let d;
  try {
    d = await fetchJSON('/debug/' + sym);
  } catch(e) {
    app.innerHTML = `<div style="color:#ef4444;padding:20px">Error: ${e}</div>`;
    return;
  }

  // CVD warmup pill
  const cvdEl = document.getElementById('debug-cvd');
  if (cvdEl) {
    if (d.cvd_warmup_remaining > 0) {
      const m = Math.ceil(d.cvd_warmup_remaining / 60);
      cvdEl.innerHTML = `<span style="color:#fbbf24">⏳ CVD warming up — ${m} min remaining</span>`;
    } else {
      cvdEl.innerHTML = `<span style="color:#22c55e">✓ CVD ready</span>`;
    }
  }

  // Live filter gates
  const gates = d.live && d.live.filter_gates ? d.live.filter_gates : {};
  function gateRow(label, pass) {
    if (pass === null || pass === undefined) return `<tr><td>${label}</td><td style="color:#6b7280">N/A</td></tr>`;
    return `<tr><td>${label}</td><td style="color:${pass?'#22c55e':'#ef4444'}">${pass ? '✓ pass' : '✗ fail'}</td></tr>`;
  }
  function gateTable(title, obj) {
    if (!obj) return '';
    const rows = Object.entries(obj).map(([k,v]) => gateRow(k.replace(/_/g,' '), v)).join('');
    return `<div style="margin-bottom:16px">
      <div style="font-size:0.75rem;color:#6b7280;text-transform:uppercase;margin-bottom:6px">${title}</div>
      <table style="width:100%;border-collapse:collapse;font-size:0.82rem">
        <tr style="color:#4b5563;font-size:0.7rem"><th style="text-align:left;padding:2px 8px">Gate</th><th style="text-align:left">Status</th></tr>
        ${rows}
      </table>
    </div>`;
  }

  // Recent signals
  const sigRows = (d.recent_signals || []).map(s => {
    const scoreColor = s.score >= 0.65 ? '#22c55e' : s.score >= 0.45 ? '#fbbf24' : '#ef4444';
    return `<tr>
      <td style="color:#6b7280;font-size:0.75rem">${toIST(s.ts)}</td>
      <td>${s.regime} ${s.direction}</td>
      <td style="color:${scoreColor};font-weight:700">${Math.round(s.score*100)}%</td>
      <td style="color:${s.fire?'#22c55e':'#4b5563'}">${s.fire?'⚡ FIRE':'—'}</td>
      <td style="font-size:0.75rem"><span style="color:#22c55e">${s.fired.join(', ')||'—'}</span></td>
      <td style="font-size:0.75rem"><span style="color:#4b5563">${s.missed.join(', ')||'—'}</span></td>
    </tr>`;
  }).join('');

  const lv = d.live || {};
  app.innerHTML = `
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:0 0 16px">
    <div>
      <div style="font-size:0.75rem;color:#6b7280;text-transform:uppercase;margin-bottom:8px">Live Market</div>
      <table style="width:100%;font-size:0.82rem;border-collapse:collapse">
        <tr><td>Price</td><td style="text-align:right;color:#e0e0e0">${lv.price||'—'}</td></tr>
        <tr><td>ADX</td><td style="text-align:right;color:${(lv.adx||0)>25?'#22c55e':'#fbbf24'}">${lv.adx||'—'}</td></tr>
        <tr><td>+DI / -DI</td><td style="text-align:right"><span style="color:#22c55e">${lv.plus_di||0}</span> / <span style="color:#ef4444">${lv.minus_di||0}</span></td></tr>
        <tr><td>ADX Rising</td><td style="text-align:right;color:${lv.adx_rising?'#22c55e':'#ef4444'}">${lv.adx_rising?'yes':'no'}</td></tr>
        <tr><td>EMA200</td><td style="text-align:right">${lv.ema200||'—'}</td></tr>
        <tr><td>Above EMA200</td><td style="text-align:right;color:${lv.above_ema200?'#22c55e':'#ef4444'}">${lv.above_ema200===null?'N/A':lv.above_ema200?'yes':'no'}</td></tr>
        <tr><td>Daily green</td><td style="text-align:right;color:${lv.daily_green?'#22c55e':'#ef4444'}">${lv.daily_green?'yes':'no'}</td></tr>
        <tr><td>Funding</td><td style="text-align:right;color:${Math.abs(lv.funding||0)<0.0003?'#22c55e':'#fbbf24'}">${((lv.funding||0)*100).toFixed(4)}%</td></tr>
        <tr><td>Vol 24h</td><td style="text-align:right;color:${(lv.vol_24h_m||0)>=50?'#22c55e':'#ef4444'}">${lv.vol_24h_m||0}M</td></tr>
      </table>
    </div>
    <div>
      ${gateTable('TREND LONG filters', gates.trend_long)}
      ${gateTable('TREND SHORT filters', gates.trend_short)}
    </div>
  </div>
  ${d.active_deal ? `<div style="background:#14532d;border:1px solid #166534;border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:0.82rem">
    <b>⚡ Active deal:</b> ${d.active_deal.direction} — Entry ${(+d.active_deal.entry).toFixed(4)}
    SL ${(+d.active_deal.stop_loss).toFixed(4)} TP ${(+d.active_deal.take_profit).toFixed(4)}
  </div>` : ''}
  <div style="font-size:0.75rem;color:#6b7280;text-transform:uppercase;margin-bottom:6px">Last 3 Signal Evaluations</div>
  <table style="width:100%;border-collapse:collapse;font-size:0.8rem">
    <tr style="color:#4b5563;font-size:0.7rem">
      <th style="text-align:left;padding:4px 8px">Time</th>
      <th style="text-align:left">Regime+Dir</th>
      <th style="text-align:left">Score</th>
      <th style="text-align:left">Fire</th>
      <th style="text-align:left">Fired signals</th>
      <th style="text-align:left">Missed signals</th>
    </tr>
    ${sigRows || '<tr><td colspan="6" style="color:#4b5563;padding:8px">No signals in DB yet</td></tr>'}
  </table>`;
}
</script>
</body>
</html>""")


@app.get("/backtest/results")
def backtest_results() -> JSONResponse:
    import json, os
    path = os.path.join(os.path.dirname(__file__), "..", "backtest", "results.json")
    try:
        with open(path) as f:
            return JSONResponse(json.load(f))
    except FileNotFoundError:
        return JSONResponse({"error": "No backtest results yet. Run: python -m backtest.run"}, status_code=404)


@app.get("/backtest", response_class=HTMLResponse)
async def backtest_dashboard() -> HTMLResponse:
    return HTMLResponse("<script>location='/#backtest'</script>")


@app.get("/backtest/_legacy", response_class=HTMLResponse)
async def _backtest_legacy() -> HTMLResponse:
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest Results — confluence_bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', monospace; font-size: 14px; }
  header { background: #1a1d27; padding: 0 24px; border-bottom: 1px solid #2a2d3a;
           display: flex; align-items: center; gap: 16px; height: 48px; }
  .brand { font-size: 1.0rem; font-weight: 700; color: #a78bfa; margin-right: 4px; }
  .tabs { display: flex; gap: 2px; }
  .tab  { padding: 5px 14px; border-radius: 6px; font-size: 0.82rem; font-weight: 500;
          color: #6b7280; text-decoration: none; transition: background .15s, color .15s; }
  .tab:hover  { color: #e0e0e0; background: #2a2d3a; }
  .tab.active { color: #e0e0e0; background: #2a2d3a; }
  #meta { margin-left: auto; font-size: 0.75rem; color: #4b5563; }

  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px,1fr));
              gap: 14px; padding: 20px; }
  .kpi { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 16px; }
  .kpi label { display: block; font-size: 0.7rem; color: #6b7280;
               text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }
  .kpi .v { font-size: 1.6rem; font-weight: 700; }
  .green { color: #22c55e; } .red { color: #ef4444; }
  .blue  { color: #60a5fa; } .purple { color: #a78bfa; } .yellow { color: #fbbf24; }

  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 0 20px 20px; }
  @media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }

  .panel { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 16px; }
  .panel h2 { font-size: 0.75rem; color: #6b7280; text-transform: uppercase;
              letter-spacing: .05em; margin-bottom: 14px; }

  .chart-wrap { position: relative; height: 240px; }

  table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
  th { background: #12141e; color: #6b7280; padding: 7px 10px; text-align: right;
       border-bottom: 1px solid #2a2d3a; font-weight: 500; }
  th:first-child { text-align: left; }
  td { padding: 6px 10px; border-bottom: 1px solid #1e2130; text-align: right; }
  td:first-child { text-align: left; font-weight: 500; }
  tr:hover td { background: #1e2130; }
  .pos { color: #22c55e; } .neg { color: #ef4444; }

  .full { padding: 0 20px 20px; }
  .full .panel { }
  .badge { display:inline-block; padding:2px 7px; border-radius:4px;
           font-size:0.68rem; font-weight:600; }
  .badge-WIN     { background:#14532d; color:#bbf7d0; }
  .badge-LOSS    { background:#7f1d1d; color:#fecaca; }
  .badge-TIMEOUT { background:#713f12; color:#fef3c7; }
</style>
</head>
<body>
<header>
  <span class="brand">confluence_bot</span>
  <nav class="tabs">
    <a href="/" class="tab">Trade Log</a>
    <a href="/market" class="tab">Market</a>
    <a href="/backtest" class="tab active">Backtest</a>
  </nav>
  <span id="meta">loading…</span>
</header>

<div id="app">
  <div style="padding:40px;color:#4b5563;text-align:center">Loading results…</div>
</div>

<script>
async function load() {
  let d;
  try {
    const r = await fetch('/backtest/results');
    if (!r.ok) throw new Error(await r.text());
    d = await r.json();
  } catch(e) {
    document.getElementById('app').innerHTML =
      `<div style="padding:40px;color:#ef4444;text-align:center">${e.message}</div>`;
    return;
  }

  const t      = d.stats.total;
  const monthly = d.stats.monthly || [];
  const byReg  = d.stats.by_regime || {};
  const bySym  = d.stats.by_symbol || {};
  const trades = d.trades || [];
  const sc     = d.capital || 1000;

  document.getElementById('meta').textContent =
    `${d.symbols?.join(', ')} | Capital $${sc.toLocaleString()} | Risk ${(d.risk_pct*100).toFixed(0)}%/trade`;

  function pnlCls(v) { return v >= 0 ? 'pos' : 'neg'; }
  function pnlFmt(v) { return (v>=0?'+':'') + '$' + (+v).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2}); }
  function pctFmt(v) { return (v>=0?'+':'') + (+v).toFixed(1) + '%'; }

  // ── KPIs ────────────────────────────────────────────────────────────────────
  const kpis = [
    { label:'Starting Capital',  val: '$'+sc.toLocaleString(),   cls:'blue'   },
    { label:'Final Equity',      val: '$'+(+t.final_equity).toLocaleString('en',{minimumFractionDigits:2}), cls: t.final_equity>=sc?'green':'red' },
    { label:'Total Return',      val: pctFmt(t.total_return_pct), cls: t.total_return_pct>=0?'green':'red' },
    { label:'Total Trades',      val: t.trades,                   cls:'blue'   },
    { label:'Win Rate',          val: (t.win_rate*100).toFixed(1)+'%', cls:'purple' },
    { label:'Profit Factor',     val: profitFactor(trades),        cls: profitFactor(trades)>='1.50'?'green':'yellow' },
    { label:'Max Drawdown',      val: '$'+(+t.max_drawdown_usd).toFixed(0)+' ('+t.max_drawdown_pct+'%)', cls:'red' },
    { label:'Avg Win / Loss',    val: pnlFmt(t.avg_win)+' / '+pnlFmt(t.avg_loss), cls:'blue' },
  ];

  document.getElementById('app').innerHTML = `
  <div class="kpi-grid">${kpis.map(k=>`
    <div class="kpi"><label>${k.label}</label><div class="v ${k.cls}">${k.val}</div></div>`).join('')}
  </div>

  <div class="two-col">
    <div class="panel">
      <h2>Equity Curve</h2>
      <div class="chart-wrap"><canvas id="eq-chart"></canvas></div>
    </div>
    <div class="panel">
      <h2>Monthly Return %</h2>
      <div class="chart-wrap"><canvas id="bar-chart"></canvas></div>
    </div>
  </div>

  <div class="two-col">
    <div class="panel">
      <h2>By Regime</h2>
      ${bucketTable(byReg)}
    </div>
    <div class="panel">
      <h2>By Symbol</h2>
      ${bucketTable(bySym)}
    </div>
  </div>

  <div class="full">
    <div class="panel">
      <h2>Monthly Returns</h2>
      ${monthlyTable(monthly, sc)}
    </div>
  </div>

  <div class="full">
    <div class="panel">
      <h2>Last 20 Trades</h2>
      ${tradeTable(trades.slice(-20).reverse())}
    </div>
  </div>`;

  buildEquityCurve(monthly, sc);
  buildBarChart(monthly);
}

function profitFactor(trades) {
  const w = trades.filter(t=>t.pnl>0).reduce((s,t)=>s+t.pnl,0);
  const l = Math.abs(trades.filter(t=>t.pnl<0).reduce((s,t)=>s+t.pnl,0));
  return l===0 ? 'inf' : (w/l).toFixed(2);
}

function bucketTable(data) {
  const rows = Object.entries(data).sort();
  if (!rows.length) return '<p style="color:#4b5563;padding:10px">no data</p>';
  return `<table>
    <thead><tr><th>Name</th><th>Trades</th><th>W/L/T</th><th>WR</th><th>PnL</th></tr></thead>
    <tbody>${rows.map(([name,b])=>`<tr>
      <td>${name}</td>
      <td>${b.trades}</td>
      <td>${b.wins}/${b.losses}/${b.timeouts}</td>
      <td class="${b.win_rate>=0.4?'pos':'neg'}">${(b.win_rate*100).toFixed(1)}%</td>
      <td class="${pnlCls(b.pnl)}">${pnlFmt(b.pnl)}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

function monthlyTable(monthly, sc) {
  const annuals = {};
  monthly.forEach(m => {
    const y = m.month.slice(0,4);
    if (!annuals[y]) annuals[y] = {pnl:0, trades:0, wins:0, start_eq: m.start_eq};
    annuals[y].pnl    += m.pnl;
    annuals[y].trades += m.trades;
    annuals[y].wins   += m.wins;
    annuals[y].end_eq  = m.end_eq;
  });

  const monthRows = monthly.map(m => {
    const cls = m.pnl >= 0 ? 'pos' : 'neg';
    return `<tr>
      <td>${m.month}</td>
      <td>${m.trades}</td>
      <td>${m.wins}/${m.losses}/${m.timeouts}</td>
      <td class="${m.wins/Math.max(m.trades,1)>=0.4?'pos':'neg'}">${m.trades?Math.round(m.wins/m.trades*100)+'%':'—'}</td>
      <td class="${cls}">${pnlFmt(m.pnl)}</td>
      <td>$${(+m.end_eq).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
      <td class="${cls}" style="font-weight:700">${pctFmt(m.pct)}</td>
    </tr>`;
  }).join('');

  const annualRows = Object.entries(annuals).sort().map(([y,a]) => {
    const ret = a.pnl / a.start_eq * 100;
    return `<tr style="background:#12141e;font-weight:600">
      <td>${y} TOTAL</td>
      <td>${a.trades}</td>
      <td>—</td>
      <td class="${a.wins/Math.max(a.trades,1)>=0.4?'pos':'neg'}">${Math.round(a.wins/a.trades*100)}%</td>
      <td class="${pnlCls(a.pnl)}">${pnlFmt(a.pnl)}</td>
      <td>$${(+a.end_eq).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
      <td class="${pnlCls(ret)}" style="font-weight:700">${pctFmt(ret)}</td>
    </tr>`;
  }).join('');

  return `<table>
    <thead><tr>
      <th>Month</th><th>Trades</th><th>W/L/T</th><th>WR</th>
      <th>PnL</th><th>Equity</th><th>Return%</th>
    </tr></thead>
    <tbody>${monthRows}${annualRows}</tbody>
  </table>`;
}

function tradeTable(trades) {
  return `<table>
    <thead><tr>
      <th>Date</th><th>Symbol</th><th>Dir</th><th>Regime</th>
      <th>Score</th><th>Risk$</th><th>Outcome</th><th>PnL</th><th>Equity</th>
    </tr></thead>
    <tbody>${trades.map(t=>`<tr>
      <td>${tsDate(t.exit_ts)}</td>
      <td>${t.symbol}</td>
      <td style="color:${t.direction==='LONG'?'#22c55e':'#ef4444'}">${t.direction}</td>
      <td>${t.regime}</td>
      <td>${(+t.score).toFixed(2)}</td>
      <td>$${(+t.risk_amount).toFixed(1)}</td>
      <td><span class="badge badge-${t.outcome}">${t.outcome}</span></td>
      <td class="${pnlCls(t.pnl)}">${pnlFmt(t.pnl)}</td>
      <td>$${t.equity_after ? (+t.equity_after).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—'}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

function pnlCls(v) { return +v >= 0 ? 'pos' : 'neg'; }
function pnlFmt(v) { return (+v>=0?'+':'')+'$'+(+v).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function pctFmt(v) { return (+v>=0?'+':''),(+v).toFixed(1)+'%'; }
function tsDate(ms) { return toISTDate(ms); }

function buildEquityCurve(monthly, sc) {
  let eq = sc;
  const labels = ['Start'];
  const data   = [sc];
  monthly.forEach(m => {
    eq += m.pnl;
    labels.push(m.month);
    data.push(+eq.toFixed(2));
  });
  new Chart(document.getElementById('eq-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data, fill: true,
        borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.1)',
        pointRadius: 2, tension: 0.3,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color:'#6b7280', maxTicksLimit: 12 }, grid: { color:'#1e2130' } },
        y: { ticks: { color:'#6b7280', callback: v=>'$'+v.toLocaleString() }, grid: { color:'#1e2130' } },
      },
    },
  });
}

function buildBarChart(monthly) {
  const labels = monthly.map(m => m.month.slice(0,7));
  const data   = monthly.map(m => +m.pct.toFixed(2));
  const colors = data.map(v => v >= 0 ? '#22c55e' : '#ef4444');
  new Chart(document.getElementById('bar-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{ data, backgroundColor: colors, borderRadius: 3 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color:'#6b7280', maxTicksLimit: 14 }, grid: { color:'#1e2130' } },
        y: { ticks: { color:'#6b7280', callback: v=>v+'%' }, grid: { color:'#1e2130' } },
      },
    },
  });
}

load();
</script>
</body>
</html>""")


# ── Live market data helpers ───────────────────────────────────────────────────

def _fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=8) as r:
        return _json.loads(r.read())


def _calc_adx_live(candles: list, period: int = 14) -> dict:
    h  = [c["h"] for c in candles]
    l  = [c["l"] for c in candles]
    cl = [c["c"] for c in candles]
    n  = len(cl)
    if n < period * 2 + 1:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}

    tr_v, pdm_v, mdm_v = [], [], []
    for i in range(1, n):
        tr  = max(h[i] - l[i], abs(h[i] - cl[i-1]), abs(l[i] - cl[i-1]))
        up  = h[i] - h[i-1]
        dn  = l[i-1] - l[i]
        tr_v.append(tr)
        pdm_v.append(up if up > dn and up > 0 else 0.0)
        mdm_v.append(dn if dn > up and dn > 0 else 0.0)

    def ws(v, p):
        out = [0.0] * len(v)
        if len(v) < p:
            return out
        out[p-1] = sum(v[:p]) / p
        for i in range(p, len(v)):
            out[i] = (out[i-1] * (p-1) + v[i]) / p
        return out

    st = ws(tr_v, period); sp = ws(pdm_v, period); sm = ws(mdm_v, period)
    if st[-1] == 0:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    pdi = 100 * sp[-1] / st[-1]
    mdi = 100 * sm[-1] / st[-1]
    dx_v = []
    for i in range(period - 1, len(st)):
        s = pdi + mdi
        dx_v.append(100 * abs(pdi - mdi) / s if s > 0 else 0.0)
    sadx = ws(dx_v, period)
    return {"adx": round(sadx[-1], 1), "plus_di": round(pdi, 1), "minus_di": round(mdi, 1)}


def _calc_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _calc_swing_structure(candles: list, pivot_n: int = 3) -> dict:
    """Identify HH/HL/LH/LL swing structure from candles using n-bar pivot detection.

    Returns structure labels, buy_confidence (0.0–1.0 where 1.0 = pure HH+HL),
    and buy zone boundaries derived from the last pivot low/high.
    """
    if len(candles) < pivot_n * 2 + 4:
        return {"structure": [], "buy_confidence": 0.0,
                "buy_zone_low": 0.0, "buy_zone_high": 0.0,
                "last_pivot_high": 0.0, "last_pivot_low": 0.0}

    pivot_highs: list[float] = []
    pivot_lows:  list[float] = []
    for i in range(pivot_n, len(candles) - pivot_n):
        if all(candles[i]["h"] >= candles[i - j]["h"] for j in range(1, pivot_n + 1)) and \
           all(candles[i]["h"] >= candles[i + j]["h"] for j in range(1, pivot_n + 1)):
            pivot_highs.append(candles[i]["h"])
        if all(candles[i]["l"] <= candles[i - j]["l"] for j in range(1, pivot_n + 1)) and \
           all(candles[i]["l"] <= candles[i + j]["l"] for j in range(1, pivot_n + 1)):
            pivot_lows.append(candles[i]["l"])

    structure: list[str] = []
    buy_score = 0
    total     = 0

    if len(pivot_highs) >= 2:
        if pivot_highs[-1] > pivot_highs[-2]:
            structure.append("HH"); buy_score += 1
        else:
            structure.append("LH")
        total += 1

    if len(pivot_lows) >= 2:
        if pivot_lows[-1] > pivot_lows[-2]:
            structure.append("HL"); buy_score += 1
        else:
            structure.append("LL")
        total += 1

    buy_confidence = round(buy_score / max(total, 1), 2)
    lph = pivot_highs[-1] if pivot_highs else 0.0
    lpl = pivot_lows[-1]  if pivot_lows  else 0.0
    # Buy zone: from last pivot low (support) to midpoint of the last swing
    bz_high = round((lpl + lph) / 2.0, 4) if lph and lpl else 0.0

    return {
        "structure":       structure,
        "buy_confidence":  buy_confidence,
        "buy_zone_low":    round(lpl, 6),
        "buy_zone_high":   bz_high,
        "last_pivot_high": round(lph, 6),
        "last_pivot_low":  round(lpl, 6),
    }


def _get_klines(symbol: str, tf: str, limit: int) -> list:
    url  = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={tf}&limit={limit}"
    data = _fetch_json(url)
    return [{"ts": x[0], "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4]), "v": float(x[5])} for x in data]


def _detect_regime_live(symbol: str, candles_4h: list, candles_1d: list) -> dict:
    """Return regime, adx_info, ema200, price, adx_slope_rising for all 5 regimes."""
    adx   = _calc_adx_live(candles_4h[-35:])
    swing = _calc_swing_structure(candles_4h)

    closes_4h = [c["c"] for c in candles_4h]
    closes_1d = [c["c"] for c in candles_1d]
    ema200    = _calc_ema(closes_4h, 200) if len(closes_4h) >= 200 else 0.0
    price     = closes_4h[-1]

    # ADX slope (compare current vs 3 bars ago)
    adx_prev   = _calc_adx_live(candles_4h[-38:-3]) if len(candles_4h) >= 38 else adx
    adx_rising = adx["adx"] >= adx_prev["adx"]

    # ── EMA50 (1D) for crash + pump ───────────────────────────────────────────
    ema50_1d   = _calc_ema(closes_1d, 50) if len(closes_1d) >= 50 else 0.0
    change_7d  = (closes_1d[-1] - closes_1d[-8]) / closes_1d[-8] if len(closes_1d) >= 8 else 0.0

    # ── 1. PUMP — price above EMA50(1D) AND 7-day gain > +12% AND new highs ──
    pump = False
    if ema50_1d > 0 and price > ema50_1d and change_7d > 0.12:
        if len(closes_1d) >= 5 and price > max(closes_1d[-5:-1]):
            pump = True

    # ── 2. CRASH — price below EMA50(1D) AND 7-day drop > -12% AND new lows ─
    crash = False
    if not pump and ema50_1d > 0 and price < ema50_1d and change_7d < -0.12:
        if len(closes_1d) >= 5 and closes_1d[-1] < min(closes_1d[-5:-1]):
            crash = True

    # ── 3. BREAKOUT — ADX transitioning (18-28) AND price >1% outside range ─
    breakout = False
    breakout_direction = "NEUTRAL"
    if not pump and not crash and 18 <= adx["adx"] <= 30:
        candles_range = candles_4h[-21:] if len(candles_4h) >= 21 else candles_4h
        if candles_range:
            rng_high = max(c["h"] for c in candles_range[:-1])
            rng_low  = min(c["l"] for c in candles_range[:-1])
            # Check volume on current 4H bar vs 14-bar average
            vols     = [c["v"] for c in candles_4h[-15:]] if len(candles_4h) >= 15 else []
            vol_ok   = (candles_4h[-1]["v"] >= (sum(vols[:-1]) / max(len(vols)-1, 1)) * 1.5) if len(vols) > 1 else False
            if vol_ok:
                if price > rng_high * 1.01:
                    breakout = True; breakout_direction = "LONG"
                elif price < rng_low * 0.99:
                    breakout = True; breakout_direction = "SHORT"

    # ── 4. RANGE — ADX < 20 AND price range is tight (≤12% of mid) ──────────
    range_confirmed = False
    if not pump and not crash and not breakout and adx["adx"] < 20:
        candles_rng = candles_4h[-20:] if len(candles_4h) >= 20 else candles_4h
        if len(candles_rng) >= 10:
            rh = max(c["h"] for c in candles_rng)
            rl = min(c["l"] for c in candles_rng)
            mid = (rh + rl) / 2.0
            if mid > 0 and (rh - rl) / mid <= 0.12:
                range_confirmed = True

    # ── 5. TREND — default ────────────────────────────────────────────────────
    if pump:
        regime    = "PUMP"
        direction = "LONG"
    elif crash:
        regime    = "CRASH"
        direction = "SHORT"
    elif breakout:
        regime    = "BREAKOUT"
        direction = breakout_direction
    elif range_confirmed:
        regime    = "RANGE"
        direction = "NEUTRAL"
    else:
        regime = "TREND"
        if adx["minus_di"] - adx["plus_di"] >= 5 and (ema200 == 0 or price < ema200):
            direction = "SHORT"
        elif adx["plus_di"] - adx["minus_di"] >= 5 and (ema200 == 0 or price > ema200):
            direction = "LONG"
        else:
            direction = "NEUTRAL"

    # ── Signal gates ──────────────────────────────────────────────────────────
    gates_ok   = True
    gate_notes = []

    if regime == "TREND" and direction == "LONG":
        if ema200 > 0 and price < ema200:
            gates_ok = False; gate_notes.append("Below EMA200")
        if not adx_rising:
            gates_ok = False; gate_notes.append("ADX declining")
        if candles_1d and candles_1d[-1]["c"] < candles_1d[-1]["o"]:
            gates_ok = False; gate_notes.append("Daily bar red")
        if ema200 > 0 and price > ema200 * 1.15:
            gates_ok = False; gate_notes.append("Price overextended (>15% EMA200)")

    elif regime == "TREND" and direction == "SHORT":
        if ema200 > 0 and price > ema200:
            gates_ok = False; gate_notes.append("Above EMA200")
        if not adx_rising:
            gates_ok = False; gate_notes.append("ADX declining")
        if candles_1d and candles_1d[-1]["c"] > candles_1d[-1]["o"]:
            gates_ok = False; gate_notes.append("Daily bar green (bounce)")

    elif regime == "PUMP":
        funding_val = 0.0   # checked by caller
        gate_notes.append(f"7d gain: +{change_7d*100:.1f}%")
        gate_notes.append(f"Above EMA50(1D): ${ema50_1d:,.0f}")

    elif regime == "BREAKOUT":
        gate_notes.append(f"ADX transitioning: {adx['adx']:.1f}")
        gate_notes.append(f"Direction: {direction}")

    elif regime == "CRASH":
        gate_notes.append(f"7d drop: {change_7d*100:.1f}%")
        gate_notes.append(f"Below EMA50(1D): ${ema50_1d:,.0f}")

    # PUMP and BREAKOUT are always valid if detected (no extra gates in live view)
    if regime in ("PUMP", "CRASH", "BREAKOUT"):
        gates_ok = True

    signal = "WAIT"
    if regime == "PUMP":
        signal = "PUMP — LONG"
    elif regime == "CRASH":
        signal = "CRASH — SHORT"
    elif regime == "BREAKOUT":
        signal = f"BREAKOUT {direction}"
    elif direction != "NEUTRAL" and gates_ok:
        signal = f"{direction} candidate"

    return {
        "regime":             regime,
        "direction":          direction,
        "signal":             signal,
        "gates_ok":           gates_ok,
        "gate_notes":         gate_notes,
        "adx":                adx["adx"],
        "plus_di":            adx["plus_di"],
        "minus_di":           adx["minus_di"],
        "adx_rising":         adx_rising,
        "ema200":             round(ema200, 2),
        "ema50_1d":           round(ema50_1d, 2),
        "change_7d_pct":      round(change_7d * 100, 2),
        "price":              price,
        "swing":              swing,
    }


def _coinglass_fields(sym: str, price: float) -> dict:
    """Extract Coinglass paid-data fields from the live cache for one symbol.

    Returns a dict with keys: oi_change_pct, ls_ratio, ls_bias,
    liq_below, liq_above, coinglass_live.
    All values are JSON-serialisable; nulls used when data is unavailable.
    """
    empty = {
        "oi_change_pct": None,
        "ls_ratio":      None,
        "ls_bias":       None,
        "liq_below":     None,
        "liq_above":     None,
        "coinglass_live": False,
    }
    if _cache is None or price == 0:
        return empty

    try:
        # OI 24h trend
        oi_hist = _cache.get_oi_history(sym, window=24)
        oi_chg  = None
        if len(oi_hist) >= 2 and oi_hist[0] != 0:
            oi_chg = round((oi_hist[-1] - oi_hist[0]) / oi_hist[0] * 100.0, 2)

        # Long/Short ratio
        ls = _cache.get_long_short_ratio(sym)
        ls_bias = None
        if ls is not None:
            if ls > 1.8:
                ls_bias = "crowded_long"
            elif ls < 0.6:
                ls_bias = "crowded_short"
            else:
                ls_bias = "neutral"

        # Nearest liq clusters above and below current price
        clusters  = _cache.get_liq_clusters(sym)
        liq_below = None
        liq_above = None
        if clusters and price > 0:
            below = [c for c in clusters if c["price"] < price]
            above = [c for c in clusters if c["price"] > price]
            if below:
                nb = max(below, key=lambda c: c["price"])
                dist = round(abs(nb["price"] - price) / price * 100, 2)
                liq_below = {"price": nb["price"], "size_m": round(nb["size_usd"]/1e6, 2),
                             "side": nb["side"], "dist_pct": dist}
            if above:
                na = min(above, key=lambda c: c["price"])
                dist = round(abs(na["price"] - price) / price * 100, 2)
                liq_above = {"price": na["price"], "size_m": round(na["size_usd"]/1e6, 2),
                             "side": na["side"], "dist_pct": dist}

        any_live = any(x is not None for x in (oi_chg, ls, liq_below, liq_above))
        return {
            "oi_change_pct":  oi_chg,
            "ls_ratio":       round(ls, 3) if ls is not None else None,
            "ls_bias":        ls_bias,
            "liq_below":      liq_below,
            "liq_above":      liq_above,
            "coinglass_live": any_live,
        }
    except Exception:
        return empty


@app.get("/market/data")
def market_data() -> JSONResponse:
    """Live market conditions for all 9 configured symbols — prices, regime, ADX, funding,
    plus Coinglass paid data: OI 24h trend, L/S ratio, liquidation heatmap clusters."""
    symbols = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","AVAXUSDT","ADAUSDT","DOTUSDT","DOGEUSDT","SUIUSDT"]
    result  = {}
    try:
        # Fetch tickers (price + 24h change + volume)
        tickers = {d["symbol"]: d for d in _fetch_json(
            "https://fapi.binance.com/fapi/v1/ticker/24hr"
        ) if d["symbol"] in symbols}

        # Funding rates
        funding = {d["symbol"]: float(d["lastFundingRate"])
                   for d in _fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
                   if d["symbol"] in symbols}

        # BTC 4H + 1D (for EMA200 and crash check — shared across symbols)
        btc_4h = _get_klines("BTCUSDT", "4h", 210)
        btc_1d = _get_klines("BTCUSDT", "1d", 60)

        for sym in symbols:
            tk    = tickers.get(sym, {})
            c4h   = btc_4h if sym == "BTCUSDT" else _get_klines(sym, "4h", 210)
            c1d   = btc_1d if sym == "BTCUSDT" else _get_klines(sym, "1d", 60)
            price = float(tk.get("lastPrice", 0))

            info = _detect_regime_live(sym, c4h, c1d)
            result[sym] = {
                **info,
                "price":        price or info["price"],
                "change_24h":   float(tk.get("priceChangePercent", 0)),
                "volume_24h_m": round(float(tk.get("quoteVolume", 0)) / 1e6, 0),
                "funding_pct":  round(funding.get(sym, 0) * 100, 4),
                **_coinglass_fields(sym, price or info["price"]),
            }

    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse(result)


@app.get("/market", response_class=HTMLResponse)
async def market_dashboard() -> HTMLResponse:
    return HTMLResponse("<script>location='/#market'</script>")


@app.get("/market/_legacy", response_class=HTMLResponse)
async def _market_legacy() -> HTMLResponse:
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Market — confluence_bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', monospace; font-size: 14px; }
  header { background: #1a1d27; padding: 0 24px; border-bottom: 1px solid #2a2d3a;
           display: flex; align-items: center; gap: 16px; height: 48px; }
  .brand { font-size: 1.0rem; font-weight: 700; color: #a78bfa; margin-right: 4px; }
  .tabs { display: flex; gap: 2px; }
  .tab  { padding: 5px 14px; border-radius: 6px; font-size: 0.82rem; font-weight: 500;
          color: #6b7280; text-decoration: none; transition: background .15s, color .15s; }
  .tab:hover  { color: #e0e0e0; background: #2a2d3a; }
  .tab.active { color: #e0e0e0; background: #2a2d3a; }
  #refresh-info { margin-left: auto; font-size: 0.75rem; color: #4b5563; }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px,1fr));
           gap: 16px; padding: 20px; }

  .card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 12px;
          padding: 20px; position: relative; }
  .card.bullish   { border-color: #166534; }
  .card.bearish   { border-color: #7f1d1d; }
  .card.neutral   { border-color: #2a2d3a; }
  .card.pump      { border-color: #22c55e; box-shadow: 0 0 12px rgba(34,197,94,0.2); }
  .card.crash     { border-color: #f97316; box-shadow: 0 0 12px rgba(249,115,22,0.2); }
  .card.breakout  { border-color: #60a5fa; box-shadow: 0 0 12px rgba(96,165,250,0.15); }

  .sym-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
  .sym-name { font-size: 1.1rem; font-weight: 700; color: #e0e0e0; }
  .sym-price { font-size: 1.5rem; font-weight: 700; }
  .chg { font-size: 0.8rem; font-weight: 600; padding: 2px 7px; border-radius: 4px; }
  .chg.up { background: #14532d; color: #bbf7d0; }
  .chg.dn { background: #7f1d1d; color: #fecaca; }

  .signal-badge { display: inline-block; padding: 5px 14px; border-radius: 6px;
                  font-size: 0.85rem; font-weight: 700; margin-bottom: 14px; }
  .sig-long     { background: #14532d; color: #bbf7d0; }
  .sig-short    { background: #7f1d1d; color: #fecaca; }
  .sig-wait     { background: #1e2130; color: #6b7280; }
  .sig-crash    { background: #451a03; color: #fed7aa; }
  .sig-pump     { background: #14532d; color: #bbf7d0; border: 1px solid #22c55e; }
  .sig-breakout { background: #1e3a5f; color: #bfdbfe; }

  .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; }
  .metric { background: #12141e; border-radius: 6px; padding: 10px; }
  .metric .lbl { font-size: 0.65rem; color: #6b7280; text-transform: uppercase;
                 letter-spacing: .05em; margin-bottom: 3px; }
  .metric .val { font-size: 1.1rem; font-weight: 700; }
  .green { color: #22c55e; } .red { color: #ef4444; }
  .blue  { color: #60a5fa; } .purple { color: #a78bfa; } .yellow { color: #fbbf24; }
  .gray  { color: #6b7280; }

  .di-bar { margin-top: 4px; }
  .di-bar-track { background: #12141e; border-radius: 4px; height: 6px; margin-top: 3px; position: relative; }
  .di-plus  { background: #22c55e; height: 6px; border-radius: 4px; position: absolute; left: 0; }
  .di-minus { background: #ef4444; height: 6px; border-radius: 4px; position: absolute; right: 0; }

  .gates { font-size: 0.72rem; color: #6b7280; margin-top: 10px; }
  .gates span { margin-right: 8px; }
  .gate-fail { color: #ef4444; }
  .gate-ok   { color: #22c55e; }

  .adx-slope { font-size: 0.7rem; margin-top: 6px; }
  .ema-line  { font-size: 0.7rem; color: #6b7280; margin-top: 4px; }

  .footer { padding: 0 20px 30px; }
  .note { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 8px; padding: 14px 18px;
          font-size: 0.75rem; color: #4b5563; line-height: 1.6; }
</style>
</head>
<body>
<header>
  <span class="brand">confluence_bot</span>
  <nav class="tabs">
    <a href="/" class="tab">Trade Log</a>
    <a href="/market" class="tab active">Market</a>
    <a href="/backtest" class="tab">Backtest</a>
  </nav>
  <span id="refresh-info">refreshing every 30s</span>
</header>

<div id="app" class="cards">
  <div style="padding:40px;color:#4b5563;grid-column:1/-1;text-align:center">Loading live data…</div>
</div>

<div class="footer">
  <div class="note">
    <b>5 Regimes</b> &mdash;
    <span class="green">&#9650; PUMP</span>: price above EMA50(1D) + 7d gain &gt;12% + new highs &bull;
    <span class="red">&#9660; CRASH</span>: below EMA50(1D) + 7d drop &gt;12% + new lows &bull;
    <span style="color:#60a5fa">&#8658; BREAKOUT</span>: ADX 18-30 + price &gt;1% outside 20-bar range + vol spike &bull;
    <span class="yellow">&#8644; TREND</span>: ADX &gt;25, DI confirms direction &bull;
    <span class="blue">&#8651; RANGE</span>: ADX &lt;20.
    Gates (TREND only): EMA200 &bull; ADX rising &bull; Daily bar confirms direction.
  </div>
</div>

<script>
const SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','AVAXUSDT','ADAUSDT','DOTUSDT','DOGEUSDT','SUIUSDT'];

function fmt(n, dec=2) { return (+n).toLocaleString('en',{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
function fmtPct(v) { return (v>=0?'+':'')+fmt(v,2)+'%'; }

const REGIME_META = {
  TREND:    { icon: '&#8644;', label: 'TREND',    cls: 'yellow' },
  RANGE:    { icon: '&#8651;', label: 'RANGE',    cls: 'blue'   },
  CRASH:    { icon: '&#9660;', label: 'CRASH',    cls: 'red'    },
  PUMP:     { icon: '&#9650;', label: 'PUMP',     cls: 'green'  },
  BREAKOUT: { icon: '&#8658;', label: 'BREAKOUT', cls: 'purple' },
};

function regimeBadge(d) {
  const r = d.regime, dir = d.direction, sig = d.signal;
  if (r === 'PUMP')
    return `<span class="signal-badge sig-pump">&#9650; PUMP — LONG ENTRY (${d.change_7d_pct > 0 ? '+' : ''}${d.change_7d_pct}% 7d)</span>`;
  if (r === 'CRASH')
    return `<span class="signal-badge sig-crash">&#9888; CRASH — SHORT (${d.change_7d_pct}% 7d)</span>`;
  if (r === 'BREAKOUT')
    return `<span class="signal-badge sig-breakout">&#8658; BREAKOUT ${dir} — Volume confirmed</span>`;
  if (sig.includes('LONG'))
    return `<span class="signal-badge sig-long">&#8679; TREND LONG — all gates passed</span>`;
  if (sig.includes('SHORT'))
    return `<span class="signal-badge sig-short">&#8681; TREND SHORT — all gates passed</span>`;
  if (r === 'RANGE')
    return `<span class="signal-badge sig-wait">&#8651; RANGE — watching boundaries</span>`;
  return `<span class="signal-badge sig-wait">&#9711; WAIT — gates not met</span>`;
}

function diBar(pdi, mdi) {
  const total = Math.max(pdi + mdi, 1);
  const pw = Math.round(pdi / total * 100);
  const mw = Math.round(mdi / total * 100);
  return `
  <div class="di-bar">
    <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:#6b7280">
      <span class="green">+DI ${pdi}</span><span class="red">-DI ${mdi}</span>
    </div>
    <div class="di-bar-track">
      <div class="di-plus"  style="width:${pw}%"></div>
      <div class="di-minus" style="width:${mw}%"></div>
    </div>
  </div>`;
}

function cardClass(d) {
  if (d.regime === 'PUMP')     return 'pump';
  if (d.regime === 'CRASH')    return 'crash';
  if (d.regime === 'BREAKOUT') return 'breakout';
  if (d.signal.includes('LONG'))  return 'bullish';
  if (d.signal.includes('SHORT')) return 'bearish';
  return 'neutral';
}

function regimeColor(r) {
  return (REGIME_META[r] || {cls:'gray'}).cls;
}

function pumpExtra(d) {
  if (d.regime !== 'PUMP') return '';
  return `<div style="background:#0d1f0f;border:1px solid #166534;border-radius:6px;padding:10px;margin-bottom:12px;font-size:0.75rem;">
    <b class="green">&#9650; Parabolic pump detected</b><br>
    7-day gain: <b class="green">+${d.change_7d_pct}%</b> &nbsp;|&nbsp;
    EMA50(1D): <b>$${fmt(d.ema50_1d,0)}</b> &nbsp;|&nbsp;
    Price making new highs &#10003;
  </div>`;
}

function breakoutExtra(d) {
  if (d.regime !== 'BREAKOUT') return '';
  const col = d.direction === 'LONG' ? 'green' : 'red';
  const arrow = d.direction === 'LONG' ? '&#8679;' : '&#8681;';
  return `<div style="background:#0d1a2f;border:1px solid #1e3a5f;border-radius:6px;padding:10px;margin-bottom:12px;font-size:0.75rem;">
    <b class="${col}">${arrow} Range breakout — ${d.direction}</b><br>
    ADX transitioning: <b>${d.adx}</b> &nbsp;|&nbsp;
    Volume spike confirmed &#10003;
  </div>`;
}

function crashExtra(d) {
  if (d.regime !== 'CRASH') return '';
  return `<div style="background:#1f0a00;border:1px solid #7f1d1d;border-radius:6px;padding:10px;margin-bottom:12px;font-size:0.75rem;">
    <b class="red">&#9660; Crash regime active</b><br>
    7-day drop: <b class="red">${d.change_7d_pct}%</b> &nbsp;|&nbsp;
    EMA50(1D): <b>$${fmt(d.ema50_1d,0)}</b> &nbsp;|&nbsp;
    Price at new lows &#10003;
  </div>`;
}

function render(data) {
  const html = SYMBOLS.map(sym => {
    const d = data[sym];
    if (!d) return '';
    const chgCls = d.change_24h >= 0 ? 'up' : 'dn';
    const aboveEma = d.price > d.ema200 && d.ema200 > 0;
    const emaDec = d.ema200 >= 100 ? 0 : d.ema200 >= 1 ? 2 : 4;
    const emaTxt = d.ema200 > 0
      ? `Price ${aboveEma ? '&#8679; above' : '&#8681; below'} EMA200 ($${fmt(d.ema200, emaDec)})`
      : 'EMA200 (4H) not available';

    const fundCls = d.funding_pct > 0.05 ? 'red' : d.funding_pct < -0.05 ? 'red' : 'green';
    const adxCls  = d.adx > 40 ? 'red' : d.adx > 25 ? 'yellow' : 'green';
    const adxSlope = d.adx_rising
      ? '<span class="green">&#8679; rising</span>'
      : '<span class="red">&#8681; declining</span>';

    // Gate notes: for pump/crash/breakout show info pills; for trend show fail/pass
    let gateHtml;
    if (['PUMP','CRASH','BREAKOUT'].includes(d.regime)) {
      gateHtml = d.gate_notes.map(g => `<span style="color:#6b7280">${g}</span>`).join(' &nbsp;&bull;&nbsp; ');
    } else {
      gateHtml = d.gate_notes.length
        ? d.gate_notes.map(g => `<span class="gate-fail">&#10005; ${g}</span>`).join(' ')
        : '<span class="gate-ok">&#10003; All gates passed</span>';
    }

    return `
    <div class="card ${cardClass(d)}">
      <div class="sym-header">
        <div>
          <div class="sym-name">${sym.replace('USDT','')}</div>
          <div class="sym-price ${d.change_24h>=0?'green':'red'}">$${fmt(d.price, d.price>100?0:2)}</div>
        </div>
        <div style="text-align:right">
          <span class="chg ${chgCls}">${fmtPct(d.change_24h)}</span>
          <div style="font-size:0.7rem;color:#4b5563;margin-top:4px">Vol $${fmt(d.volume_24h_m,0)}M</div>
          <div style="font-size:0.68rem;color:#6b7280;margin-top:2px">7d ${d.change_7d_pct>0?'<span class="green">+':'<span class="red">'}${d.change_7d_pct}%</span></div>
        </div>
      </div>

      ${regimeBadge(d)}
      ${pumpExtra(d)}${crashExtra(d)}${breakoutExtra(d)}

      <div class="metrics">
        <div class="metric">
          <div class="lbl">Regime</div>
          <div class="val ${regimeColor(d.regime)}">${(REGIME_META[d.regime]||{icon:''}).icon} ${d.regime}</div>
        </div>
        <div class="metric">
          <div class="lbl">ADX (4H)</div>
          <div class="val ${adxCls}">${d.adx}</div>
        </div>
        <div class="metric">
          <div class="lbl">Funding Rate</div>
          <div class="val ${fundCls}">${d.funding_pct > 0 ? '+' : ''}${d.funding_pct}%</div>
        </div>
        <div class="metric">
          <div class="lbl">Direction</div>
          <div class="val ${d.direction==='LONG'?'green':d.direction==='SHORT'?'red':'gray'}">${d.direction}</div>
        </div>
      </div>

      ${diBar(d.plus_di, d.minus_di)}
      <div class="adx-slope">ADX slope: ${adxSlope}</div>
      <div class="ema-line">${emaTxt}</div>
      <div class="gates" style="margin-top:10px">${gateHtml}</div>
    </div>`;
  }).join('');
  document.getElementById('app').innerHTML = html;
}

async function load() {
  document.getElementById('refresh-info').textContent = 'fetching…';
  try {
    const r = await fetch('/market/data');
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    render(d);
    const now = new Date().toLocaleTimeString();
    document.getElementById('refresh-info').textContent = `updated ${now} — refreshing every 30s`;
  } catch(e) {
    document.getElementById('app').innerHTML =
      `<div style="padding:40px;color:#ef4444;grid-column:1/-1;text-align:center">Error: ${e.message}</div>`;
    document.getElementById('refresh-info').textContent = 'error — retrying…';
  }
}

load();
setInterval(load, 30000);
</script>
</body>
</html>""")


def start(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port)
