"""FastAPI metrics server — exposes trade stats and signal history via HTTP."""
import os
import sqlite3
import json as _json
import urllib.request
from fastapi import FastAPI, Request
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


@app.get("/api/circuit-breaker/status")
async def cb_status() -> JSONResponse:
    try:
        from core.circuit_breaker import status as cb_status_fn
        return JSONResponse(cb_status_fn())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/circuit-breaker/reset")
async def cb_reset() -> JSONResponse:
    try:
        from core.circuit_breaker import reset as cb_reset_fn
        result = cb_reset_fn()
        return JSONResponse({"ok": True, **result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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
            total = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status IN ('CLOSED','FILLED')"
            ).fetchone()[0]
            # A win must exceed 10% of risked amount (min $1) — matches
            # circuit_breaker logic so dashboard WR is consistent.
            wins  = conn.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE status IN ('CLOSED','FILLED') AND pnl_usdt > 0 "
                "AND pnl_usdt >= MAX(COALESCE(risk_usdt * 0.10, 1.0), 1.0)"
            ).fetchone()[0]
            pnl   = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdt),0) FROM trades "
                "WHERE status IN ('CLOSED','FILLED')"
            ).fetchone()[0]
            by_regime_rows = conn.execute(
                """SELECT regime, direction, COUNT(*) as cnt,
                          COALESCE(SUM(pnl_usdt),0) as pnl
                   FROM trades WHERE status IN ('CLOSED','FILLED')
                   GROUP BY regime, direction"""
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

    # ── BTC Dominance ─────────────────────────────────────────────────────────
    btc_dominance_info: dict = {}
    if _cache is not None:
        try:
            dom_val   = _cache.get_btc_dominance()
            dom_trend = _cache.get_btc_dominance_trend()
            btc_dominance_info = {
                "dominance_pct": round(dom_val * 100, 2) if dom_val > 0 else None,
                "trend":         dom_trend,
                "alt_long_ok":   not (dom_trend == "rising" and dom_val > 0.55),
            }
        except Exception:
            pass

    return JSONResponse({
        "symbol":               sym,
        "cvd_warmup_remaining": cvd_warmup_remaining,
        "cvd_ready":            cvd_warmup_remaining == 0.0,
        "active_deal":          active_deal,
        "recent_signals":       recent_signals,
        "live":                 live,
        "btc_dominance":        btc_dominance_info,
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
    """Live Binance snapshot for all 8 symbols — price, 24h change, funding, ADX, regime."""
    import datetime
    symbols = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT",
               "XRPUSDT","LINKUSDT","DOGEUSDT","SUIUSDT"]
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
  .badge-TREND      { background: #1d4ed8; color: #bfdbfe; }
  .badge-RANGE      { background: #713f12; color: #fef3c7; }
  .badge-CRASH      { background: #7f1d1d; color: #fecaca; }
  .badge-PUMP       { background: #14532d; color: #bbf7d0; }
  .badge-BREAKOUT   { background: #1d4ed8; color: #bfdbfe; }
  .badge-LONG       { background: #14532d; color: #bbf7d0; }
  .badge-SHORT      { background: #7f1d1d; color: #fecaca; }
  .badge-FIRE       { background: #7c3aed; color: #ede9fe; }
  .badge-WIN        { background: #14532d; color: #bbf7d0; }
  .badge-LOSS       { background: #7f1d1d; color: #fecaca; }
  .badge-TIMEOUT    { background: #713f12; color: #fef3c7; }
  /* Strategy-specific regime badges */
  .badge-LEADLAG    { background: #0f3460; color: #93c5fd; }
  .badge-MICRORANGE { background: #3b0764; color: #e9d5ff; }
  .badge-SESSION    { background: #164e63; color: #a5f3fc; }
  .badge-EMA_PULLBACK { background: #052e16; color: #86efac; }
  .badge-ZONE       { background: #1e1b4b; color: #c7d2fe; }
  .badge-FVG        { background: #14532d; color: #6ee7b7; }
  .badge-VWAPBAND   { background: #134e4a; color: #5eead4; }
  .badge-OISPIKE    { background: #4a044e; color: #f5d0fe; }

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

  /* ── Strategies panel ── */
  #panel-strategies { padding: 20px; max-width: 1400px; margin: 0 auto; }
  #panel-strategies h1 { font-size: 1rem; font-weight: 700; color: #a78bfa;
    margin-bottom: 4px; letter-spacing: .04em; }
  #panel-strategies .intro { font-size: 0.78rem; color: #6b7280; margin-bottom: 20px; line-height: 1.6; }
  .strat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px,1fr)); gap: 18px; }
  .strat-card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 12px;
    padding: 20px; display: flex; flex-direction: column; gap: 12px; }
  .strat-card.indep  { border-left: 3px solid #a78bfa; }
  .strat-card.trend  { border-left: 3px solid #60a5fa; }
  .strat-card.range  { border-left: 3px solid #fbbf24; }
  .strat-card.meta   { border-left: 3px solid #6b7280; }
  .strat-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
  .strat-name { font-size: 0.95rem; font-weight: 700; color: #e0e0e0; }
  .strat-badges { display: flex; gap: 5px; flex-wrap: wrap; justify-content: flex-end; }
  .sb { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.65rem;
    font-weight: 700; letter-spacing: .04em; white-space: nowrap; }
  .sb-indep  { background: #2e1065; color: #c4b5fd; }
  .sb-trend  { background: #1e3a5f; color: #93c5fd; }
  .sb-range  { background: #422006; color: #fde68a; }
  .sb-meta   { background: #1a1d27; color: #6b7280; border: 1px solid #2a2d3a; }
  .sb-any    { background: #1a2e1a; color: #86efac; }
  .sb-tf     { background: #12141e; color: #9ca3af; border: 1px solid #2a2d3a; }
  .strat-desc { font-size: 0.8rem; color: #9ca3af; line-height: 1.6; }
  .strat-section { font-size: 0.68rem; font-weight: 700; color: #4b5563; text-transform: uppercase;
    letter-spacing: .08em; margin-bottom: 4px; }
  .sig-list { display: flex; flex-direction: column; gap: 3px; }
  .sig-row { display: flex; align-items: flex-start; gap: 8px; font-size: 0.77rem; }
  .sig-name { color: #60a5fa; font-family: monospace; font-size: 0.73rem; min-width: 140px;
    flex-shrink: 0; padding-top: 1px; }
  .sig-desc { color: #9ca3af; line-height: 1.45; }
  .sig-hard { color: #fbbf24; font-size: 0.65rem; font-weight: 700;
    background: #2d1f00; border: 1px solid #78350f; border-radius: 3px;
    padding: 1px 5px; margin-left: 4px; white-space: nowrap; flex-shrink: 0; }
  .param-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .param-row { display: flex; flex-direction: column; background: #12141e;
    border-radius: 6px; padding: 8px 10px; }
  .param-label { font-size: 0.62rem; color: #4b5563; text-transform: uppercase;
    letter-spacing: .06em; margin-bottom: 2px; }
  .param-val { font-size: 0.82rem; font-weight: 600; color: #e0e0e0; }
  .param-val.green { color: #22c55e; } .param-val.yellow { color: #fbbf24; }
  .param-val.purple { color: #a78bfa; } .param-val.red { color: #ef4444; }
  .strat-divider { border: none; border-top: 1px solid #1e2130; margin: 0; }
  .why-box { background: #12141e; border-radius: 6px; padding: 10px 12px;
    font-size: 0.77rem; color: #9ca3af; line-height: 1.55; }
  .why-box strong { color: #a78bfa; }
  .sltp-box { font-size: 0.77rem; color: #9ca3af; line-height: 1.6; }
  .sltp-box code { background: #12141e; border-radius: 3px; padding: 1px 5px;
    font-size: 0.72rem; color: #34d399; font-family: monospace; }
  /* Backtest data requirements block */
  .bt-req { background: #0c0e17; border: 1px solid #1e2130; border-radius: 6px;
    padding: 8px 12px; margin-top: 4px; font-size: 0.76rem; }
  .bt-req-row { display: flex; justify-content: space-between; align-items: baseline;
    padding: 4px 0; border-bottom: 1px solid #1e2130; gap: 8px; }
  .bt-req-row:last-of-type { border-bottom: none; }
  .bt-req-lbl { color: #4b5563; font-size: 0.68rem; text-transform: uppercase;
    letter-spacing: .05em; white-space: nowrap; flex-shrink: 0; }
  .bt-req-val { color: #d1d5db; font-weight: 500; text-align: right; }
  .bt-req-val.ok  { color: #22c55e; }
  .bt-req-val.opt { color: #fbbf24; }
  .bt-req-val.warn{ color: #f87171; }
  .bt-cmd { display: block; background: #12141e; border-radius: 4px;
    padding: 6px 10px; margin-top: 8px; color: #86efac;
    font-family: monospace; font-size: 0.70rem; word-break: break-all; }
  @media (max-width: 600px) { .strat-grid { grid-template-columns: 1fr; }
    .param-grid { grid-template-columns: 1fr; } .strat-name { font-size: 0.85rem; } }
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
    <button class="tab" onclick="showTab('strategies',this)">&#128218; Strategies</button>
    <button class="tab" onclick="showTab('gates',this)">&#128683; Gates</button>
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
    <div class="tl-card" id="cb-card" style="border-left:3px solid #22c55e">
      <h3>Circuit Breaker</h3>
      <div class="val green" id="cb-status-val">—</div>
      <div id="cb-reason" style="font-size:0.7rem;color:#6b7280;margin-top:4px;min-height:16px"></div>
      <button id="cb-reset-btn" onclick="cbReset()" style="display:none;margin-top:8px;padding:4px 12px;
        background:#7f1d1d;color:#fca5a5;border:1px solid #dc2626;border-radius:4px;
        font-size:0.72rem;cursor:pointer;font-weight:600">&#x21BA; Reset Breaker</button>
    </div>
  </div>
  <section style="padding-top:0">
    <h2>Signal Readiness <span style="font-size:0.7rem;color:#4b5563;font-weight:400">— how close each coin is to firing</span></h2>
    <div id="readiness-body">
      <div style="color:#4b5563;padding:12px">loading…</div>
    </div>
  </section>
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
      <thead><tr><th>Time</th><th>Symbol</th><th>Strategy</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>Size</th><th>PnL</th><th>Status</th></tr></thead>
      <tbody id="trades-body"><tr><td colspan="10" style="color:#4b5563">loading…</td></tr></tbody>
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
  <div id="bt-form-wrap" style="padding:16px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;background:#1a1d27;border-bottom:1px solid #2a2d3a">
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:0.68rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Symbol</label>
      <select id="bt-sym" style="background:#12141e;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 10px;font-size:0.83rem;min-width:130px">
        <option value="ALL">ALL (8 coins)</option>
        <option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option>
        <option>BNBUSDT</option><option>XRPUSDT</option><option>LINKUSDT</option>
        <option>DOGEUSDT</option><option>SUIUSDT</option>
      </select>
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:0.68rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Strategy</label>
      <select id="bt-strat" style="background:#12141e;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 10px;font-size:0.83rem;min-width:145px">
        <option value="auto_regime">Auto — regime switching (matches live bot)</option>
        <option value="auto_regime_compound">Auto — regime switching + compound</option>
        <option value="">Loading strategies...</option>
      </select>
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:0.68rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">From Date</label>
      <input id="bt-from" type="date" value="2023-01-01" style="background:#12141e;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 10px;font-size:0.83rem">
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:0.68rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">To Date</label>
      <input id="bt-to" type="date" style="background:#12141e;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 10px;font-size:0.83rem">
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:0.68rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Capital ($)</label>
      <input id="bt-capital" type="number" value="4744" min="100" step="100" style="background:#12141e;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 10px;font-size:0.83rem;width:100px">
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:0.68rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Risk %</label>
      <input id="bt-risk" type="number" value="1" min="0.5" max="10" step="0.5" style="background:#12141e;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 10px;font-size:0.83rem;width:72px">
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:0.68rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Sizing mode</label>
      <select id="bt-sizing" style="background:#12141e;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 10px;font-size:0.83rem;min-width:130px">
        <option value="compound">Compound (1% of current equity)</option>
        <option value="fixed">Fixed (1% of starting capital only)</option>
      </select>
    </div>
    <button id="bt-run-btn" onclick="runBacktest()" style="padding:7px 20px;background:#4c1d95;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:0.85rem;font-weight:600;white-space:nowrap;align-self:flex-end">&#9654; Run</button>
    <span id="bt-status" style="font-size:0.75rem;color:#6b7280;align-self:center"></span>
  </div>
  <div id="cache-status" style="padding:6px 20px;font-size:12px;color:#6b7280;background:#12141e;border-bottom:1px solid #2a2d3a;display:flex;justify-content:space-between;align-items:center">
    <span id="cache-info-text">Checking local cache...</span>
    <button id="dl-btn" onclick="downloadData()" style="font-size:11px;padding:3px 10px;background:#1e3a5f;color:#93c5fd;border:1px solid #2563eb;border-radius:4px;cursor:pointer">Download / Update Data</button>
  </div>
  <div id="bt-meta" style="padding:10px 20px 0;font-size:0.75rem;color:#4b5563"></div>
  <div id="bt-app">
    <div style="padding:60px;color:#4b5563;text-align:center">Configure a backtest above and click <b style="color:#a78bfa">Run</b>.</div>
  </div>
</div>

<!-- ── DEBUG ─────────────────────────────────────────────────── -->
<div id="panel-debug" class="panel">
  <div style="padding:16px 20px 0">
    <select id="debug-sym" style="background:#1a1d27;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:6px 12px;font-size:0.85rem">
      <option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option>
      <option>BNBUSDT</option><option>XRPUSDT</option><option>LINKUSDT</option>
      <option>DOGEUSDT</option><option>SUIUSDT</option>
    </select>
    <button onclick="loadDebug()" style="margin-left:8px;padding:6px 14px;background:#4c1d95;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:0.85rem">Refresh</button>
    <span id="debug-cvd" style="margin-left:16px;font-size:0.8rem;color:#6b7280"></span>
  </div>
  <div id="debug-app" style="padding:16px 20px">
    <div style="color:#4b5563">Select a symbol and click Refresh</div>
  </div>
</div>

<!-- ── STRATEGIES ─────────────────────────────────────────── -->
<div id="panel-gates" class="panel">
  <div style="padding:20px">
    <h2 style="margin:0 0 8px 0">&#128683; Trade Gate Status</h2>
    <p style="color:#9ca3af;font-size:0.82rem;margin:0 0 16px 0">
      Every gate that can block a new trade. Green = clear, Red = blocking, Yellow = partial/warning.
      <button onclick="loadGates()" style="margin-left:12px;padding:4px 14px;background:#374151;color:#e0e0e0;border:1px solid #4b5563;border-radius:4px;cursor:pointer;font-size:0.78rem">&#8635; Refresh</button>
      <span id="gates-ts" style="margin-left:8px;color:#6b7280;font-size:0.72rem"></span>
    </p>
    <div id="gates-list" style="display:flex;flex-direction:column;gap:6px">
      <div style="color:#6b7280;padding:40px;text-align:center">Loading gates...</div>
    </div>
  </div>
</div>

<div id="panel-strategies" class="panel">
  <h1>&#128218; Strategy Reference</h1>
  <p class="intro">
    All strategies running in confluence_bot &mdash; what each one does, when it fires, which signals it uses,
    and how stops &amp; targets are calculated. Strategies run independently as asyncio tasks; the executor deduplicates
    entries via <code style="background:#12141e;padding:1px 5px;border-radius:3px;font-size:0.72rem;color:#34d399">_pending_deals</code> and DB guard so only one position per symbol+direction can be open at a time.
    <br><span style="color:#f59e0b;font-size:0.77rem">&#9888; HARD gate = blocks fire regardless of score; no-data defaults to block (conservative). Soft signals contribute to score.</span>
  </p>
  <div class="strat-grid">

    <!-- ── REGIME DETECTOR ─────────────────────── -->
    <div class="strat-card meta">
      <div class="strat-head">
        <div class="strat-name">&#9685; Regime Detector</div>
        <div class="strat-badges">
          <span class="sb sb-meta">META</span>
          <span class="sb sb-tf">1D · 4H · 1H</span>
        </div>
      </div>
      <div class="strat-desc">
        Classifies each symbol's market structure every loop tick. All strategies read the current regime before deciding whether to fire.
        Five regimes are possible &mdash; each enables/disables different strategies and directions.
      </div>
      <div class="strat-section">Regimes &amp; conditions</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">TREND</span><span class="sig-desc">ADX &gt; 25 (4H), exits range with confirmation. Default fallback. Enables EMA Pullback, LeadLag, main scorer.</span></div>
        <div class="sig-row"><span class="sig-name">RANGE</span><span class="sig-desc">All 3 recent ADX readings &lt; 20 (hysteresis lock). Range size ≤ 12% of mid. Enables MicroRange, InsideBar, Range scorer.</span></div>
        <div class="sig-row"><span class="sig-name">BREAKOUT</span><span class="sig-desc">Opens for 3 bars after exiting RANGE. Price must breach ≥ 0.3% beyond range bounds. Direction: LONG above, SHORT below.</span></div>
        <div class="sig-row"><span class="sig-name">PUMP</span><span class="sig-desc">Price &gt; EMA50(1D) + 7-day gain &gt; +12% + new 4-bar high. Blocks SHORT entries. Mirror of CRASH.</span></div>
        <div class="sig-row"><span class="sig-name">CRASH</span><span class="sig-desc">Price &lt; EMA50(1D) + 7-day drop &gt; −12% + no 4-bar recovery. Blocks LONG entries.</span></div>
      </div>
      <div class="why-box">
        <strong>Why ADX hysteresis?</strong> A single ADX threshold causes regime flapping on choppy bars. By requiring 3 consecutive readings below 20 to enter RANGE, and 2 of 3 above 25 to exit, the detector stays in regime long enough for strategies to work.
      </div>
    </div>

    <!-- ── MICRORANGE ──────────────────────────── -->
    <div class="strat-card range">
      <div class="strat-head">
        <div class="strat-name">&#8651; MicroRange Flip</div>
        <div class="strat-badges">
          <span class="sb sb-any">ANY REGIME</span>
          <span class="sb sb-tf">5M</span>
        </div>
      </div>
      <div class="strat-desc">
        Mean-reversion inside a tight 5-minute consolidation box. Detects when price compresses into a low-volatility range
        on the last N completed 5m bars, then fades the move back to the opposite boundary.
        Tightened heavily to reduce overtrading: all 4 signals required (threshold = 1.0).
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        Finds a box where (high − low) / mid ≤ <strong>0.5%</strong> over the last 10 bars (tightened from 1.0%).
        Entry zone within 0.1% of boundary. SL just outside the boundary.
        Blocked in CRASH (no longs) and PUMP (no shorts). In TREND regime, only trades with the 4H DI+ bias.
        <strong>Invariant:</strong> stop_pct × 2 ≤ range_max_pct must hold or live scorer rejects all boxes.
      </div>
      <div class="strat-section">Signals (equal weight 0.25 each — ALL 4 required)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">box_detected</span><span class="sig-desc">Tight range box confirmed on last 10 bars (range_max_pct ≤ 0.5%). Always True when scored.</span></div>
        <div class="sig-row"><span class="sig-name">entry_zone</span><span class="sig-desc">Price within 0.1% of range_low (LONG) or range_high (SHORT). Always True when scored.</span></div>
        <div class="sig-row"><span class="sig-name">volume_ok</span><span class="sig-desc">Current bar volume ≤ 1.3× 20-bar average. High-volume bars signal potential breakout, not mean reversion.</span></div>
        <div class="sig-row"><span class="sig-name">rsi_aligned</span><span class="sig-desc">RSI ≤ 35 for LONG (tightened from 40), RSI ≥ 65 for SHORT (tightened from 60). Stronger confirmation needed.</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        LONG: <code>SL = range_low × (1 − 0.2%)</code> &nbsp; <code>TP = range_low + range_width × 0.75</code><br>
        SHORT: <code>SL = range_high × (1 + 0.2%)</code> &nbsp; <code>TP = range_high − range_width × 0.75</code>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">1.0 (ALL 4)</div></div>
        <div class="param-row"><div class="param-label">Box max width</div><div class="param-val yellow">0.5% of mid</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">20 min / symbol</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">5m OHLCV &mdash; min 400 bars (32 warmup + eval period)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val opt">None required</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">32 &times; 5m (vol MA-20 + window + buffer)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:5m"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-07 &rarr; 2024-01 (sideways + range-bound)</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy microrange --from-date 2023-07-01 --to-date 2024-01-31</code>
      </div>
    </div>

    <!-- ── EMA PULLBACK ────────────────────────── -->
    <div class="strat-card trend">
      <div class="strat-head">
        <div class="strat-name">&#8599; EMA Pullback (15m)</div>
        <div class="strat-badges">
          <span class="sb sb-trend">TREND</span>
          <span class="sb sb-tf">15M · 4H</span>
        </div>
      </div>
      <div class="strat-desc">
        Trend-continuation entries at EMA21 on the 15m chart, filtered by 4H macro direction.
        Catches the "healthy pullback in a trend" setup &mdash; price dips to the fast EMA, volume quiets,
        then closes back in trend direction with a confirmed bounce candle.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        4H macro filter first: bullish = close &gt; 4H EMA50 OR 4H EMA21 &gt; EMA50 (for LONG).
        On 15m: EMA21 &gt; EMA50 (uptrend intact). Prior bar <em>touched</em> EMA21 (within <strong>0.2%</strong>). Current bar
        must close back in trend direction with body ≥ 0.2% AND be at least 0.2% above EMA21 (LONG) —
        these ensure a real reversal candle, not a scratch. <strong>Volume hard gate:</strong> bounce bar volume must exceed
        pullback bar volume (buyers stepping in). RSI in healthy zone 35–60. SL placed below pullback bar low (LONG) or above pullback bar high (SHORT).
      </div>
      <div class="strat-section">Scored signals (equal weight 0.33 each — max 1.0)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">htf_aligned</span><span class="sig-desc">4H macro direction agrees: close &gt; 4H EMA50 or 4H EMA21 &gt; EMA50 (LONG). Always True when scored.</span></div>
        <div class="sig-row"><span class="sig-name">ema_structure</span><span class="sig-desc">15m EMA21 &gt; EMA50 (LONG) or &lt; EMA50 (SHORT). Confirms trend on entry timeframe.</span></div>
        <div class="sig-row"><span class="sig-name">pullback_touch</span><span class="sig-desc">Previous bar within 0.2% of EMA21 (tightened from 0.4%). Always True when scored.</span></div>
      </div>
      <div class="strat-section">Hard gates (block fire regardless of score)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">bounce_confirm</span><span class="sig-desc">Close back in trend direction, candle body ≥ 0.2%, close ≥ 0.2% away from EMA21. No marginal wicks.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">vol_confirm</span><span class="sig-desc">Bounce bar volume &gt; pullback bar volume. Buyers/sellers stepping in confirms the reversal is real.</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        LONG: <code>SL = min(EMA21 × 0.998,  pullback_bar_low × 0.999)</code><br>
        SHORT: <code>SL = max(EMA21 × 1.002,  pullback_bar_high × 1.001)</code><br>
        <code>TP = entry ± dist × 1.5</code> &nbsp; (reduced from 2.5× to cut timeouts)
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.75 (all 3 scored)</div></div>
        <div class="param-row"><div class="param-label">RR ratio</div><div class="param-val yellow">1.5×</div></div>
        <div class="param-row"><div class="param-label">Pullback touch</div><div class="param-val">0.2% of EMA21</div></div>
        <div class="param-row"><div class="param-label">Min bounce body</div><div class="param-val">0.2%</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">45 min / symbol</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">15m OHLCV &mdash; min 500 bars (45 warmup + eval period)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val">4H OHLCV &mdash; macro EMA50/EMA21 alignment gate</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">45 &times; 15m (EMA50 + vol MA-20 + buffer)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:15m"] &nbsp; ohlcv["SYM:4h"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-01 &rarr; 2024-12 (trending market, mixed regimes)</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy ema_pullback --from-date 2023-01-01 --to-date 2024-12-31</code>
      </div>
    </div>

    <!-- ── LEADLAG ─────────────────────────────── -->
    <div class="strat-card indep">
      <div class="strat-head">
        <div class="strat-name">&#9889; Lead-Lag (BTC → Alt)</div>
        <div class="strat-badges">
          <span class="sb sb-any">ANY REGIME</span>
          <span class="sb sb-tf">5M (BTC + Alt)</span>
        </div>
      </div>
      <div class="strat-desc">
        Exploits the structural lag between BTC and correlated alts. When BTC breaks its rolling
        <strong>5m VWAP</strong> with a confirmed volume spike, alts that have not yet moved are entered in the
        same direction before the correlated move propagates.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        BTC is the dominant price-discovery asset. The loop monitors BTC's 5m bars for a VWAP break on
        elevated volume. Alt positions are taken immediately, with a score bonus proportional to BTC's
        distance above/below VWAP.
        <strong>Hard gate:</strong> the alt must NOT have already moved ≥ max_alt_premove_pct (lag window closed).
        Very tight SL (0.2%) because the entry should be near the alt's last price before propagation.
      </div>
      <div class="strat-section">Signals (equal weight 0.25 each)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">btc_vwap_break</span><span class="sig-desc">BTC crossed its rolling 5m VWAP. Always True when scored (pre-filtered before calling scorer).</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">vol_spike</span><span class="sig-desc">BTC 5m bar volume ≥ 1.5× 20-bar average. Institutional participation confirms the break.</span></div>
        <div class="sig-row"><span class="sig-name">alt_not_premoved</span><span class="sig-desc">Alt price has NOT already moved ≥ max_alt_premove_pct. Entry window still open.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">cooldown_ok</span><span class="sig-desc">Symbol not in post-trade cooldown window (30 min).</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">Bonus scoring</div>
      <div class="why-box">BTC breakout strength adds up to <strong>+0.10</strong> bonus to push borderline setups over threshold without relaxing the hard gates.</div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        Fixed % offsets: <code>SL = entry × (1 ∓ 0.2%)</code> &nbsp; <code>TP = entry × (1 ± 0.5%)</code>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.60 (3 of 4)</div></div>
        <div class="param-row"><div class="param-label">Min RR</div><div class="param-val yellow">2.5×</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">30 min / symbol</div></div>
        <div class="param-row"><div class="param-label">Check interval</div><div class="param-val">30 s</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">5m OHLCV &mdash; BTCUSDT <em>and</em> all alt symbols</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val opt">None required</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">50 &times; 5m (VWAP window + vol MA)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["BTCUSDT:5m"] &nbsp; ohlcv["SYM:5m"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Special requirement</span><span class="bt-req-val warn">BTCUSDT must be in the symbols list — it is the lead indicator</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-01 &rarr; 2024-12</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy leadlag --from-date 2023-01-01 --to-date 2024-12-31</code>
      </div>
    </div>

    <!-- ── SESSION ────────────────────────────── -->
    <div class="strat-card range">
      <div class="strat-head">
        <div class="strat-name">&#9200; Session Open Trap</div>
        <div class="strat-badges">
          <span class="sb sb-range">RANGE / BREAKOUT</span>
          <span class="sb sb-tf">5M</span>
        </div>
      </div>
      <div class="strat-desc">
        Captures the classic session open fake-out reversal. At Asia (01:00 UTC), London (08:00 UTC), and New York (13:00 UTC)
        opens, market makers frequently run stops in one direction before reversing. This strategy enters the reversal
        exactly 15 minutes into the session when direction is clear. Only fires in RANGE or BREAKOUT regime — trend environments
        are less likely to reverse cleanly at session opens.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        At T+15 min the 5m bars show whether the session "fake move" occurred: price spiked one way but is now
        reversing back. <strong>Logic:</strong> if first 15 min moved ≥ 0.6% in one direction but is now pulling back,
        enter the reversal. Range gate: session range ≤ 0.7% of open — confirms a clean sweep, not a gap or
        trend continuation. SL = session extreme + 0.2% buffer.
        The entry is available for only ~15 minutes &mdash; the loop fires once per session window, not on a polling interval.
      </div>
      <div class="strat-section">Signals (equal weight 0.25 each)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">fake_move_ok</span><span class="sig-desc">|session_move| ≥ 0.6% — sufficient price displacement to confirm a stop-hunt occurred.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">move_strong</span><span class="sig-desc">|session_move| ≥ 0.9% — even stronger displacement = higher reversal conviction.</span></div>
        <div class="sig-row"><span class="sig-name">reversal_bar</span><span class="sig-desc">Last 5m candle body ≥ 0.1% in the opposite direction. Momentum already turning.</span></div>
        <div class="sig-row"><span class="sig-name">range_compact</span><span class="sig-desc">Session range (high − low) / open ≤ 0.7% (tightened from 0.9%). Excludes gaps and trend bursts.</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        <code>SL = session_extreme + 0.2% buffer</code> &nbsp; <code>TP = entry + |entry − SL| × 1.5</code>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.75 (3 of 4)</div></div>
        <div class="param-row"><div class="param-label">Min move</div><div class="param-val">0.6% (was 0.3%)</div></div>
        <div class="param-row"><div class="param-label">Range compact</div><div class="param-val">≤ 0.7% (was 0.9%)</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">60 min / symbol / session</div></div>
        <div class="param-row"><div class="param-label">Sessions (UTC)</div><div class="param-val">01:00 · 08:00 · 13:00</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">5m OHLCV &mdash; min 300 bars per symbol</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val opt">None required</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">5 &times; 5m (minimal — session opens are time-anchored)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:5m"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Note</span><span class="bt-req-val">Low-frequency: ~3 entry windows per day &mdash; use long date ranges for statistical validity</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-01 &rarr; 2024-12 (&ge;500 session windows)</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy session --from-date 2023-01-01 --to-date 2024-12-31</code>
      </div>
    </div>

    <!-- ── INSIDEBAR ───────────────────────────── -->
    <div class="strat-card range">
      <div class="strat-head">
        <div class="strat-name">&#9634; Inside Bar Flip (1H)</div>
        <div class="strat-badges">
          <span class="sb sb-range">RANGE / TREND</span>
          <span class="sb sb-tf">1H</span>
        </div>
      </div>
      <div class="strat-desc">
        Detects multi-bar compression zones on the 1H chart (inside bars where each bar fits within the previous).
        Enters at the compression zone boundary when price touches it from inside, targeting a move to the opposite boundary.
        Requires a genuine "coiled spring" with at least 3 inside bars, declining volume, and entry near the zone POC.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        Scans last 16 × 1H bars for ≥ <strong>3</strong> consecutive inside bars (tightened from 2). Each bar's high &lt; prior high
        AND low &gt; prior low forms the zone = (zone_low, zone_high). <strong>Zone width cap: ≤ 1.0%</strong> of mid price — excludes
        wide, low-quality compressions. Volume must decline through the inside bar run (sellers exhausted).
        Entry: price touches zone_low (LONG) or zone_high (SHORT) within 0.2%, AND is within 0.5% of the zone POC.
        SL placed just outside the zone boundary + 0.2% buffer.
      </div>
      <div class="strat-section">Scored signals (equal weight 0.5 each — max 1.0)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">entry_zone</span><span class="sig-desc">Price within 0.2% of zone_low (LONG) or zone_high (SHORT). Always True when scored.</span></div>
        <div class="sig-row"><span class="sig-name">near_poc</span><span class="sig-desc">Price within 0.5% of zone POC (tightened from 1.0%). Entering near volume gravity = higher hold probability.</span></div>
      </div>
      <div class="strat-section">Hard gates (block fire regardless of score)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">strong_compression</span><span class="sig-desc">≥ 3 consecutive inside bars required (raised from 2). 2-bar compressions are too common and unreliable.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">volume_declining</span><span class="sig-desc">Avg inside-bar volume &lt; volume of the bar before the run. Quiet compression only — rising volume = distribution.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">zone_ok</span><span class="sig-desc">Zone width ≤ 1.0% of mid price (tightened from 1.5%). Wide zones have too much internal noise.</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        LONG: <code>SL = zone_low × (1 − 0.2%)</code> &nbsp; <code>TP = zone_low + zone_range × 1.5</code><br>
        SHORT: <code>SL = zone_high × (1 + 0.2%)</code> &nbsp; <code>TP = zone_high − zone_range × 1.5</code>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.75 (both scored — near_poc required)</div></div>
        <div class="param-row"><div class="param-label">RR ratio</div><div class="param-val yellow">1.5×</div></div>
        <div class="param-row"><div class="param-label">Min inside bars</div><div class="param-val">3 (was 2)</div></div>
        <div class="param-row"><div class="param-label">Max zone width</div><div class="param-val">1.0% of mid (was 1.5%)</div></div>
        <div class="param-row"><div class="param-label">Near POC</div><div class="param-val">0.5% (was 1.0%)</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">60 min / symbol</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">1H OHLCV &mdash; min 200 bars (16 warmup + eval period)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val opt">None required</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">20 &times; 1H (16 lookback + vol MA + buffer)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:1h"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-01 &rarr; 2024-12 (range-heavy months preferred)</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy insidebar --from-date 2023-01-01 --to-date 2024-12-31</code>
      </div>
    </div>

    <!-- ── FUNDING ────────────────────────────── -->
    <div class="strat-card indep">
      <div class="strat-head">
        <div class="strat-name">&#128176; Funding Rate Harvest</div>
        <div class="strat-badges">
          <span class="sb sb-any">ANY REGIME</span>
          <span class="sb sb-tf">5M + 4H + Funding</span>
        </div>
      </div>
      <div class="strat-desc">
        Systematic income from extreme perpetual funding rates. When funding is extreme (e.g. +0.08% per 8h),
        longs are paying shorts heavily. By entering SHORT before settlement and exiting after, the bot earns
        the funding payment plus a potential price mean-reversion bonus.
        <strong>Trend filter prevents counter-trend fades in strong directional markets.</strong>
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        Binance settles funding every 8h at 00:00, 08:00, 16:00 UTC. The strategy wakes 30 minutes before each window.
        <strong>Direction logic:</strong> positive funding (&gt;0.08%) → SHORT (collect from longs).
        Negative funding (&lt;−0.08%) → LONG (collect from shorts).
        <strong>Trend filter (hard gate):</strong> if 4H EMA21 signals a trend, only take trades <em>aligned</em> with the macro bias
        (e.g., no shorting into a bull trend just because positive funding is high — market can stay funded for weeks).
        <strong>Break-even WR at 0.1% funding with 0.5% SL / 0.8% TP = 38.5%.</strong>
      </div>
      <div class="strat-section">Scored signals (equal weight 0.25 each)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">rate_extreme</span><span class="sig-desc">|funding_rate| ≥ 0.08% per 8h (raised from 0.05%). Minimum threshold to justify the trade.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">rate_very_high</span><span class="sig-desc">|funding_rate| ≥ 0.15% per 8h. Elevated extreme = higher expected mean-reversion bonus.</span></div>
        <div class="sig-row"><span class="sig-name">in_window</span><span class="sig-desc">Within 30 min before OR 15 min after a settlement window. Outside this window = no trade.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">cooldown_ok</span><span class="sig-desc">No recent harvest on this symbol (8h cooldown = full funding cycle).</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">Hard gates (block fire regardless of score)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">trend_ok</span><span class="sig-desc">In TREND regime: trade direction must match 4H EMA21 bias (LONG or NEUTRAL). Blocks shorting into a bull run or longing into a bear run.</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        Fixed: <code>SL = entry × (1 ∓ 0.5%)</code> &nbsp; <code>TP = entry × (1 ± 0.8%)</code> &nbsp; RR = 1.6×
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.50 (2 of 4 scored)</div></div>
        <div class="param-row"><div class="param-label">Min rate</div><div class="param-val">0.08% / 8h (was 0.05%)</div></div>
        <div class="param-row"><div class="param-label">Trend filter</div><div class="param-val">4H EMA21 bias check</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">8h / symbol</div></div>
        <div class="param-row"><div class="param-label">Windows (UTC)</div><div class="param-val">00:00 · 08:00 · 16:00</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">5m OHLCV &mdash; for entry/exit timing around settlement windows</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val">4H OHLCV &mdash; trend filter (EMA21 macro bias)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val warn">Funding rate history &mdash; required (fetched from Binance /fundingRate endpoint)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">50 &times; 5m + 26 &times; 4H (EMA21 warmup)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:5m"] &nbsp; ohlcv["SYM:4h"] &nbsp; funding["SYM"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Note</span><span class="bt-req-val">Only 3 entry windows/day — need 6+ months for &ge;50 trades</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-01 &rarr; 2024-12 (volatile funding periods)</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy funding --from-date 2023-01-01 --to-date 2024-12-31</code>
      </div>
    </div>

    <!-- ── SWEEP ──────────────────────────────── -->
    <div class="strat-card indep">
      <div class="strat-head">
        <div class="strat-name">&#9875; Liquidity Sweep Reversal</div>
        <div class="strat-badges">
          <span class="sb sb-any">ANY REGIME</span>
          <span class="sb sb-tf">15M · 4H</span>
        </div>
      </div>
      <div class="strat-desc">
        Detects institutional stop-hunts: a 15m candle whose wick pierces a prior swing high/low by at least 0.5%
        but whose body closes back inside the range. This "sweep and reverse" pattern indicates smart money absorbed
        retail stops and is now driving price the opposite direction.
        Works in all regimes — specifically fills the bear-market gap where main TREND scorer rarely fires.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        Looks for a 15m bar where: <strong>wick</strong> extends ≥ 0.5% beyond the recent swing high/low (stop-hunt), but the
        <strong>close</strong> is back inside (rejection). Volume on the sweep bar must be ≥ 2.0× average — raised from
        1.4× to ensure only high-conviction institutional moves qualify, filtering out normal volatility sweeps.
        4H structure check is a <strong>hard gate</strong> — if 4H trend is strongly opposed, the trade is blocked.
      </div>
      <div class="strat-section">Scored signals (equal weight 0.33 each — max 1.0)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">sweep_detected</span><span class="sig-desc">Wick ≥ 0.5% through swing level, close back inside. Core pattern — always True when scored.</span></div>
        <div class="sig-row"><span class="sig-name">volume_spike</span><span class="sig-desc">Sweep candle volume ≥ 2.0× 20-bar average (raised from 1.4×). High volume confirms institutional participation.</span></div>
        <div class="sig-row"><span class="sig-name">rsi_zone</span><span class="sig-desc">RSI ≤ 50 for LONG (dipped, not overbought), RSI ≥ 50 for SHORT (spiked, not oversold).</span></div>
      </div>
      <div class="strat-section">Hard gates (block fire regardless of score)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">htf_no_block</span><span class="sig-desc">4H trend not strongly opposing. If 4H is in a strong trend against the trade direction, the gate blocks — previously a soft filter.</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        <code>SL = sweep_extreme + small_buffer</code> &nbsp; (just beyond the wick that caused the sweep)<br>
        <code>TP = entry ± dist × 2.0</code>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.75 (all 3 scored)</div></div>
        <div class="param-row"><div class="param-label">Min wick</div><div class="param-val">0.5% beyond swing</div></div>
        <div class="param-row"><div class="param-label">Volume</div><div class="param-val">≥ 2.0× avg (was 1.4×)</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">30 min / symbol</div></div>
        <div class="param-row"><div class="param-label">Check interval</div><div class="param-val">30 s</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">15m OHLCV &mdash; min 600 bars (55 warmup + eval period)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val">4H OHLCV &mdash; HTF macro bias hard gate (EMA21)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">55 &times; 15m (swing lookback 50 + pivot_n 5 + buffer)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:15m"] &nbsp; ohlcv["SYM:4h"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-05 &rarr; 2024-11 (volatile + trending — sweeps frequent)</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy sweep --from-date 2023-05-01 --to-date 2024-11-30</code>
      </div>
    </div>

    <!-- ── ZONE ───────────────────────────────── -->
    <div class="strat-card trend">
      <div class="strat-head">
        <div class="strat-name">&#9744; HTF Zone Retest</div>
        <div class="strat-badges">
          <span class="sb sb-trend">TREND / RANGE</span>
          <span class="sb sb-tf">4H · 1H</span>
        </div>
      </div>
      <div class="strat-desc">
        Highest-quality, lowest-frequency signal. Identifies 4H demand (bullish origin) and supply (bearish origin) zones —
        where price previously left impulsively (≥ 3% move) — and enters on the <em>first retest only</em> of that zone.
        First retests historically have the highest reversal probability before the zone degrades.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        A demand zone = 4H base candles just before a strong bullish impulse (≥ 3%). A supply zone = 4H base before a strong
        bearish impulse (≥ 3%). <strong>Zone width cap: ≤ 1.5% of mid price</strong> — wide consolidations are excluded.
        <strong>First retest only:</strong> any prior wick that entered the zone from the wrong side invalidates it — the
        zone's virgin status is the key edge. When price returns for the <em>first time</em>, enter in the origin direction.
        OI is confirmed as a hard gate — if OI data is unavailable, the trade is blocked.
        1H confirmation adds a lower-timeframe entry signal.
      </div>
      <div class="strat-section">Scored signals (equal weight 0.33 each — max 1.0)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">zone_active</span><span class="sig-desc">Valid 4H demand/supply zone detected and price is currently retesting it. Always True when scored.</span></div>
        <div class="sig-row"><span class="sig-name">htf_1h_confirm</span><span class="sig-desc">1H close confirms direction at the zone (bullish close in demand, bearish close in supply).</span></div>
        <div class="sig-row"><span class="sig-name">rsi_not_extreme</span><span class="sig-desc">Demand LONG: RSI &lt; 65 (not yet overbought). Supply SHORT: RSI &gt; 35 (not yet oversold).</span></div>
      </div>
      <div class="strat-section">Hard gates (block fire regardless of score)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">oi_supporting</span><span class="sig-desc">LONG: OI rising over last 3 snapshots (new longs building). SHORT: OI falling (longs liquidating). No data = blocked.</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        <code>SL = beyond zone boundary + buffer</code> &nbsp; (zone invalidation = thesis is wrong)<br>
        <code>TP = entry ± dist × 1.5</code> &nbsp; (reduced from 2.0× to cut timeouts)
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.75 (all 3 scored)</div></div>
        <div class="param-row"><div class="param-label">RR ratio</div><div class="param-val yellow">1.5× (was 2.0×)</div></div>
        <div class="param-row"><div class="param-label">Min impulse</div><div class="param-val">3% (was 2%)</div></div>
        <div class="param-row"><div class="param-label">Zone width max</div><div class="param-val">1.5% of mid</div></div>
        <div class="param-row"><div class="param-label">Retests</div><div class="param-val">1st retest only</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">120 min / symbol</div></div>
        <div class="param-row"><div class="param-label">Check interval</div><div class="param-val">60 s</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">4H OHLCV &mdash; zone origin detection (impulse candles)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val">1H OHLCV &mdash; 1H confirmation close at zone</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val warn">OI snapshots &mdash; hard gate; oi["SYM"] from fetcher</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">50 &times; 4H (zone detection lookback + impulse scan)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:4h"] &nbsp; ohlcv["SYM:1h"] &nbsp; oi["SYM"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Note</span><span class="bt-req-val">Extremely low frequency — favour wide date ranges for significance</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-01 &rarr; 2024-12 + stress: 2022-05 &rarr; 2022-06</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy zone --from-date 2023-01-01 --to-date 2024-12-31</code>
      </div>
    </div>

    <!-- ── FVG FILL ──────────────────────────────── -->
    <div class="strat-card trend">
      <div class="strat-head">
        <div class="strat-name">&#9643; FVG Fill</div>
        <div class="strat-badges">
          <span class="sb sb-trend">TREND / RANGE</span>
          <span class="sb sb-tf">1H · 4H</span>
        </div>
      </div>
      <div class="strat-desc">
        Trades price returning into unfilled 1H Fair Value Gaps (3-bar price imbalances). When price moves so fast
        that one candle's high is lower than a candle two bars later's low, an inefficiency zone is created.
        Price frequently returns to fill these gaps before continuing. Only virgin gaps (not yet retested) are traded.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        Bullish FVG: <code>bar[k].low &gt; bar[k-2].high</code> — gap between two candles. Price re-entering from above
        signals institutional support in the imbalance zone. 4H EMA21 must confirm direction. Entry = first bar that
        closes inside the gap. Gap is marked touched and never re-traded once the signal fires.
      </div>
      <div class="strat-section">Scored signals (0.33 each — threshold 0.67)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">fvg_detected</span><span class="sig-desc">Virgin 1H FVG found and price is currently inside it. Hard gate — always True when scored.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">htf_aligned</span><span class="sig-desc">4H EMA21 agrees: price above EMA21 for LONG, below for SHORT.</span></div>
        <div class="sig-row"><span class="sig-name">rsi_confirm</span><span class="sig-desc">RSI ≤ 45 for LONG (not overbought), ≥ 55 for SHORT (not oversold).</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        <code>LONG:  SL = gap_low × (1 − 0.2%)</code> &nbsp; (just below the gap bottom)<br>
        <code>SHORT: SL = gap_high × (1 + 0.2%)</code><br>
        <code>TP = entry ± dist × 2.0</code>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.67 (fvg + one confirm)</div></div>
        <div class="param-row"><div class="param-label">Min gap size</div><div class="param-val">0.3% of price</div></div>
        <div class="param-row"><div class="param-label">RR ratio</div><div class="param-val yellow">2.0×</div></div>
        <div class="param-row"><div class="param-label">Lookback</div><div class="param-val">50 × 1H bars</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">45 min / symbol</div></div>
        <div class="param-row"><div class="param-label">Check interval</div><div class="param-val">60 s</div></div>
        <div class="param-row"><div class="param-label">Max positions</div><div class="param-val">3</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">1H OHLCV &mdash; min 400 bars (55 warmup + eval period)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val">4H OHLCV &mdash; HTF EMA21 alignment (LONG above, SHORT below)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None required</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">55 &times; 1H (lookback 50 + EMA21 + buffer)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:1h"] &nbsp; ohlcv["SYM:4h"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Backtest engine</span><span class="bt-req-val ok">backtest/fvg_engine.py</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-01 &rarr; 2023-12 (target: WR &gt; 50%, PF &gt; 1.5)</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy fvg --from-date 2023-01-01 --to-date 2023-12-31</code>
      </div>
    </div>

    <!-- ── BOS / CHoCH ──────────────────────────── -->
    <div class="strat-card trend">
      <div class="strat-head">
        <div class="strat-name">&#9654; BOS / CHoCH</div>
        <div class="strat-badges">
          <span class="sb sb-trend">TREND / RANGE</span>
          <span class="sb sb-tf">1H · 4H</span>
        </div>
      </div>
      <div class="strat-desc">
        Trades confirmed 1H market structure breaks. A Break of Structure (BOS) = price closes beyond the last
        confirmed swing high/low with volume — trend continuation. A Change of Character (CHoCH) = structure break
        opposing the prevailing trend — early reversal entry. Low frequency (15–40/year), high quality.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        Swing points confirmed by <strong>pivot_n = 3 bars</strong> each side. BOS LONG: prior close &lt; swing_high,
        current close &gt; swing_high + volume ≥ 1.3× average. 4H HTF alignment is a hard requirement —
        HH+HL for LONG, LH+LL for SHORT. Entry blocked if price already 2% extended past the break level.
        SL anchored to the prior swing low (the last structure point that would invalidate the thesis).
      </div>
      <div class="strat-section">Scored signals (threshold 0.75)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">bos_confirmed</span><span class="sig-desc">Close beyond swing point with prior close inside. Weight 0.50. Hard gate.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">htf_4h</span><span class="sig-desc">4H structure aligned (HH+HL or LH+LL). Weight 0.25. Hard gate.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">choch</span><span class="sig-desc">Change of Character — structural reversal opposing the prior trend. Weight 0.15.</span></div>
        <div class="sig-row"><span class="sig-name">volume_spike</span><span class="sig-desc">Break bar volume ≥ 1.3× 20-bar average. Weight 0.10.</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        <code>SL = prior_swing_point × (1 ∓ 0.1%)</code> &nbsp; (just beyond the last confirmed structure)<br>
        <code>TP = entry ± dist × 2.5</code>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.75 (BOS + HTF = exact)</div></div>
        <div class="param-row"><div class="param-label">RR ratio</div><div class="param-val yellow">2.5×</div></div>
        <div class="param-row"><div class="param-label">Pivot N</div><div class="param-val">3 bars each side</div></div>
        <div class="param-row"><div class="param-label">Max extension</div><div class="param-val">2% beyond break</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">60 min / symbol</div></div>
        <div class="param-row"><div class="param-label">Check interval</div><div class="param-val">60 s</div></div>
        <div class="param-row"><div class="param-label">Max positions</div><div class="param-val">2</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">1H OHLCV &mdash; min 400 bars (55 warmup + eval period)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val">4H OHLCV &mdash; HTF structure (HH+HL or LH+LL) hard gate</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None required</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">55 &times; 1H (pivot lookback 50 + pivot_n 3 + buffer)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:1h"] &nbsp; ohlcv["SYM:4h"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Backtest engine</span><span class="bt-req-val ok">backtest/bos_engine.py</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Note</span><span class="bt-req-val">Low frequency (15&ndash;40/year/symbol) &mdash; use 12+ month window</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2023-01 &rarr; 2024-12 + breakout stress: Oct&ndash;Dec 2024</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy bos --from-date 2023-01-01 --to-date 2024-12-31</code>
      </div>
    </div>

    <!-- ── VWAP BAND REVERSION ───────────────────── -->
    <div class="strat-card range">
      <div class="strat-head">
        <div class="strat-name">&#8776; VWAP Band Reversion</div>
        <div class="strat-badges">
          <span class="sb sb-range">RANGE / TREND</span>
          <span class="sb sb-tf">15M · 4H</span>
        </div>
      </div>
      <div class="strat-desc">
        Mean-reversion from the ±2σ rolling VWAP bands on 15m. When price touches the outer band and closes
        back inside (rejection, not breakdown), it typically reverts to the VWAP midline. Blocked in strong trends
        (4H ADX ≥ 30) where bands keep expanding and reversion fails.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        Rolling 20-bar VWAP (5H window). Variance = <code>Σ(vol × (tp − vwap)²) / Σvol</code>. ±2σ bands.
        Entry on the bar that <em>closes back inside</em> the band after a touch — rejection, not breakout.
        TP is the <strong>dynamic VWAP midline</strong> (recalculated each bar), not a fixed target. Inline RR check
        requires |vwap − entry| / |entry − SL| ≥ 1.5 to block entries where the midline is too close.
      </div>
      <div class="strat-section">Scored signals (0.33 each — threshold 0.67)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">band_touch</span><span class="sig-desc">Previous bar touched ±2σ band, current bar closed back inside. Hard gate.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">rsi_confirm</span><span class="sig-desc">RSI ≤ 35 for LONG (oversold), ≥ 65 for SHORT (overbought).</span></div>
        <div class="sig-row"><span class="sig-name">regime_aligned</span><span class="sig-desc">RANGE or TREND regime — mean reversion valid. PUMP/CRASH blocked.</span></div>
      </div>
      <div class="strat-section">Hard gates (block fire regardless of score)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">not_trending</span><span class="sig-desc">4H ADX &lt; 30. Strong trends cause bands to expand continuously — reversion fails.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">rr_inline</span><span class="sig-desc">|VWAP − entry| / |entry − SL| ≥ 1.5. VWAP midline must be far enough away to be worth it.</span><span class="sig-hard">HARD</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        <code>SL = band_level ± 0.2%</code> &nbsp; (just beyond the 2σ band edge)<br>
        <code>TP = VWAP midline (dynamic)</code> &nbsp; — recalculated each bar, not fixed at entry
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.67 (band + one confirm)</div></div>
        <div class="param-row"><div class="param-label">VWAP window</div><div class="param-val">20 × 15m (5H rolling)</div></div>
        <div class="param-row"><div class="param-label">Band mult</div><div class="param-val">±2σ standard deviations</div></div>
        <div class="param-row"><div class="param-label">ADX block</div><div class="param-val">≥ 30 on 4H</div></div>
        <div class="param-row"><div class="param-label">Min RR</div><div class="param-val yellow">1.5× (dynamic target)</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">30 min / symbol</div></div>
        <div class="param-row"><div class="param-label">Check interval</div><div class="param-val">30 s</div></div>
        <div class="param-row"><div class="param-label">Max positions</div><div class="param-val">3</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">15m OHLCV &mdash; min 400 bars (26 warmup + eval period)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val">4H OHLCV &mdash; ADX gate (blocks when 4H ADX &ge; 30)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">None required</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">26 &times; 15m (VWAP window 20 + RSI 14 + buffer)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:15m"] &nbsp; ohlcv["SYM:4h"]</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Backtest engine</span><span class="bt-req-val ok">backtest/vwap_band_engine.py</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2024-04 &rarr; 2024-09 (ranging) + stress: 2021 H1</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy vwap_band --from-date 2024-04-01 --to-date 2024-09-30</code>
      </div>
    </div>

    <!-- ── OI SPIKE FADE ──────────────────────────── -->
    <div class="strat-card indep">
      <div class="strat-head">
        <div class="strat-name">&#9651; OI Spike Fade</div>
        <div class="strat-badges">
          <span class="sb sb-any">ANY REGIME</span>
          <span class="sb sb-tf">15M</span>
        </div>
      </div>
      <div class="strat-desc">
        Fades liquidation cascades triggered by sudden OI surges. When open interest spikes ≥ 15% in 2 hours,
        retail FOMO has piled in. A simultaneous wick rejection on the 15m candle confirms those positions are
        being immediately liquidated — creating a high-probability reversal entry.
      </div>
      <div class="strat-section">How it works</div>
      <div class="why-box">
        Aggregates Binance + Bybit OI for spike detection (cross-exchange confirmation reduces false signals).
        OI spike alone is not enough — the 15m candle must show a <strong>wick rejection</strong> (wick ≥ 0.5%
        of price AND wick &gt; body size × 0.5). Volume must also spike ≥ 1.5× average (confirms cascade, not drift).
        EMA21 gates direction: bounces above EMA = LONG; fades below EMA = SHORT.
        When OI history is unavailable in backtests, a volume proxy (≥ 2.5× avg) is used instead.
      </div>
      <div class="strat-section">Scored signals (threshold 0.75)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">oi_spike</span><span class="sig-desc">OI increased ≥ 15% in 2H (Binance + Bybit aggregated). Weight 0.40. Hard gate.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">price_rejection</span><span class="sig-desc">15m wick ≥ 0.5% and wick &gt; body × 0.5 — candle rejected at extremes. Weight 0.35. Hard gate.</span><span class="sig-hard">HARD</span></div>
        <div class="sig-row"><span class="sig-name">ema_aligned</span><span class="sig-desc">Price above EMA21 for LONG bounce, below for SHORT fade. Weight 0.15.</span></div>
        <div class="sig-row"><span class="sig-name">rsi_zone</span><span class="sig-desc">RSI 35–55 for LONG (not overbought), 45–65 for SHORT (not oversold). Weight 0.10.</span></div>
      </div>
      <div class="strat-section">SL / TP</div>
      <div class="sltp-box">
        <code>LONG:  SL = candle.low  × (1 − 0.2%)</code> &nbsp; (just below the wick extreme)<br>
        <code>SHORT: SL = candle.high × (1 + 0.2%)</code><br>
        <code>TP = entry ± ATR(14, 15m) × 2.0</code>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Threshold</div><div class="param-val green">0.75 (oi_spike 0.40 + rejection 0.35)</div></div>
        <div class="param-row"><div class="param-label">OI spike min</div><div class="param-val">≥ 15% in 2H</div></div>
        <div class="param-row"><div class="param-label">Wick min</div><div class="param-val">0.5% of price</div></div>
        <div class="param-row"><div class="param-label">RR ratio</div><div class="param-val yellow">2.0× (ATR-based)</div></div>
        <div class="param-row"><div class="param-label">Volume</div><div class="param-val">≥ 1.5× avg</div></div>
        <div class="param-row"><div class="param-label">Cooldown</div><div class="param-val purple">60 min / symbol</div></div>
        <div class="param-row"><div class="param-label">Check interval</div><div class="param-val">60 s</div></div>
        <div class="param-row"><div class="param-label">Max positions</div><div class="param-val">2</div></div>
      </div>
      <div class="strat-section">Backtest Data Requirements</div>
      <div class="bt-req">
        <div class="bt-req-row"><span class="bt-req-lbl">Primary bars</span><span class="bt-req-val">15m OHLCV &mdash; min 300 bars (20 warmup + eval period)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Secondary bars</span><span class="bt-req-val opt">None required</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">External data</span><span class="bt-req-val opt">OI history optional &mdash; oi["SYM:binance"] &nbsp; (volume proxy used when absent)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Warmup bars</span><span class="bt-req-val">20 &times; 15m (ATR-14 + EMA-21 + buffer)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Fetcher keys</span><span class="bt-req-val">ohlcv["SYM:15m"] &nbsp; oi["SYM"] (optional)</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Backtest engine</span><span class="bt-req-val ok">backtest/oi_spike_engine.py</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">OI proxy note</span><span class="bt-req-val warn">Without OI history, volume &ge; 2.5&times; avg is used — results will over-signal vs live</span></div>
        <div class="bt-req-row"><span class="bt-req-lbl">Recommended period</span><span class="bt-req-val">2022-05 &rarr; 2022-11 (LUNA + FTX liq cascades)</span></div>
        <code class="bt-cmd">python -m backtest.run --strategy oi_spike --from-date 2022-05-01 --to-date 2022-11-30</code>
      </div>
    </div>

    <!-- ── TREND SCORER ────────────────────────── -->
    <div class="strat-card trend">
      <div class="strat-head">
        <div class="strat-name">&#9650; Main Trend Scorer</div>
        <div class="strat-badges">
          <span class="sb sb-trend">TREND ONLY</span>
          <span class="sb sb-tf">Mixed TFs</span>
        </div>
      </div>
      <div class="strat-desc">
        Multi-confluence trend entry. 14 signals from diverse data sources (on-chain, options, CVD, OI, order blocks, FVG).
        Uses <em>normalised scoring</em> — signals from unavailable data sources are excluded from the denominator,
        so the score reflects confluence of what's actually available, not diluted by structural gaps.
      </div>
      <div class="strat-section">Signals (normalised weighted)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">cvd_bullish</span><span class="sig-desc">Cumulative Volume Delta bullish — buy volume exceeds sell volume (WebSocket aggTrades).</span></div>
        <div class="sig-row"><span class="sig-name">liq_sweep</span><span class="sig-desc">Liquidity cluster swept — CoinGlass cluster OR synthetic pivot low wick.</span></div>
        <div class="sig-row"><span class="sig-name">oi_funding</span><span class="sig-desc">OI rising + funding rate alignment (positive OI = new longs; funding negative = short squeeze).</span></div>
        <div class="sig-row"><span class="sig-name">vpvr_support</span><span class="sig-desc">Price at/above VPVR support node — high-volume price reclaim.</span></div>
        <div class="sig-row"><span class="sig-name">htf_structure</span><span class="sig-desc">4H structure bullish: higher highs, order blocks, pivot support.</span></div>
        <div class="sig-row"><span class="sig-name">order_block</span><span class="sig-desc">Price at a bullish order block (last down-candle before a large up-move).</span></div>
        <div class="sig-row"><span class="sig-name">options_flow</span><span class="sig-desc">Deribit options skew bullish (call premium &gt; put premium). Optional.</span></div>
        <div class="sig-row"><span class="sig-name">whale_flow</span><span class="sig-desc">CryptoQuant: exchange inflow anomaly — large deposits to exchanges (buying pressure).</span></div>
        <div class="sig-row"><span class="sig-name">rsi_divergence</span><span class="sig-desc">1H RSI bullish divergence — price makes lower low but RSI makes higher low.</span></div>
        <div class="sig-row"><span class="sig-name">ema_pullback</span><span class="sig-desc">1H EMA pullback setup — price touched EMA21, bounced, closed above.</span></div>
        <div class="sig-row"><span class="sig-name">ls_crowded_short</span><span class="sig-desc">CoinGlass L/S ratio: crowd is heavily short = contrarian LONG signal.</span></div>
        <div class="sig-row"><span class="sig-name">funding_ramp_bull</span><span class="sig-desc">Extreme negative funding — shorts pay longs, squeeze potential.</span></div>
        <div class="sig-row"><span class="sig-name">fvg_bullish</span><span class="sig-desc">Fair Value Gap bullish — unfilled imbalance on 1H/4H acting as magnet.</span></div>
        <div class="sig-row"><span class="sig-name">bb_squeeze_bull</span><span class="sig-desc">Bollinger Band squeeze resolving upward — low-volatility breakout setup.</span></div>
      </div>
      <div class="strat-section">Hard filters (all must pass)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">EMA200</span><span class="sig-desc">Price above 1D EMA200 (macro bull structure).</span></div>
        <div class="sig-row"><span class="sig-name">ADX rising</span><span class="sig-desc">4H ADX trending up (momentum building).</span></div>
        <div class="sig-row"><span class="sig-name">Daily bar</span><span class="sig-desc">Daily candle direction confirms trade direction.</span></div>
      </div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Score method</div><div class="param-val purple">Normalised</div></div>
        <div class="param-row"><div class="param-label">Min RR</div><div class="param-val yellow">2.5×</div></div>
        <div class="param-row"><div class="param-label">Signal sources</div><div class="param-val">14 signals</div></div>
        <div class="param-row"><div class="param-label">Loop interval</div><div class="param-val">60 s</div></div>
      </div>
    </div>

    <!-- ── RANGE SCORER ────────────────────────── -->
    <div class="strat-card range">
      <div class="strat-head">
        <div class="strat-name">&#8651; Main Range Scorer</div>
        <div class="strat-badges">
          <span class="sb sb-range">RANGE ONLY</span>
          <span class="sb sb-tf">Mixed TFs</span>
        </div>
      </div>
      <div class="strat-desc">
        Multi-confluence range-boundary entry. 10 signals weighted toward absorption, Wyckoff structure, and VWAP.
        Requires at least one "anchor signal" (absorption, Wyckoff spring, or RSI oversold) so the setup always
        has a structural basis — prevents pure indicator confluence firing on noise.
      </div>
      <div class="strat-section">Signals (weighted)</div>
      <div class="sig-list">
        <div class="sig-row"><span class="sig-name">absorption</span><span class="sig-desc">High-volume candle near support with small net displacement — buyers absorbed sellers without large price move.</span></div>
        <div class="sig-row"><span class="sig-name">wyckoff_spring</span><span class="sig-desc">Wyckoff spring: wick below range support on low volume, closes back inside — classic accumulation tell.</span></div>
        <div class="sig-row"><span class="sig-name">perp_basis</span><span class="sig-desc">Perpetual funding positive (contango) — spot demand exceeds futures, bullish structural bias.</span></div>
        <div class="sig-row"><span class="sig-name">options_skew</span><span class="sig-desc">Deribit options skew favors calls — put/call ratio low, market pays up for upside exposure.</span></div>
        <div class="sig-row"><span class="sig-name">anchored_vwap</span><span class="sig-desc">Price at or above range-start VWAP anchor — institutional cost basis respected.</span></div>
        <div class="sig-row"><span class="sig-name">time_distribution</span><span class="sig-desc">Volume-time distribution: price spent majority of time near current level (POC proximity).</span></div>
        <div class="sig-row"><span class="sig-name">call_skew_roc</span><span class="sig-desc">Call skew rate of change increasing — options market pricing in upside asymmetry.</span></div>
        <div class="sig-row"><span class="sig-name">rsi_oversold</span><span class="sig-desc">1H RSI &lt; 30 — statistically oversold at range support.</span></div>
        <div class="sig-row"><span class="sig-name">vwap_oversold</span><span class="sig-desc">Price below session VWAP — statistical edge for mean reversion.</span></div>
        <div class="sig-row"><span class="sig-name">fvg_bullish</span><span class="sig-desc">Fair Value Gap on 1H/4H acting as bullish magnet.</span></div>
      </div>
      <div class="strat-section">Mandatory anchor (≥1 required)</div>
      <div class="why-box"><code style="font-size:0.72rem;background:#12141e;padding:1px 5px;border-radius:3px;color:#34d399">absorption</code> OR <code style="font-size:0.72rem;background:#12141e;padding:1px 5px;border-radius:3px;color:#34d399">wyckoff_spring</code> OR <code style="font-size:0.72rem;background:#12141e;padding:1px 5px;border-radius:3px;color:#34d399">rsi_oversold</code> — prevents pure indicator confluence firing on noise. Ensures structural evidence exists.</div>
      <div class="param-grid">
        <div class="param-row"><div class="param-label">Score method</div><div class="param-val purple">Weighted sum</div></div>
        <div class="param-row"><div class="param-label">Min RR</div><div class="param-val yellow">2.5×</div></div>
        <div class="param-row"><div class="param-label">Signal sources</div><div class="param-val">10 signals</div></div>
        <div class="param-row"><div class="param-label">Loop interval</div><div class="param-val">60 s</div></div>
      </div>
    </div>

  </div><!-- /strat-grid -->

  <!-- ── BACKTEST TIMEFRAMES REFERENCE ─────────────────────────────────────── -->
  <div style="margin-top:32px; padding:20px 24px; background:#0d1117; border:1px solid #1e2535; border-radius:10px;">
    <div style="font-size:0.82rem; font-weight:600; color:#94a3b8; letter-spacing:.08em; text-transform:uppercase; margin-bottom:14px;">
      Backtest Data Requirements — per Strategy Engine
    </div>
    <table style="width:100%; border-collapse:collapse; font-size:0.78rem; color:#c9d1d9;">
      <thead>
        <tr style="border-bottom:1px solid #1e2535; color:#64748b; font-size:0.70rem; text-transform:uppercase; letter-spacing:.06em;">
          <th style="text-align:left; padding:6px 10px; font-weight:500;">Strategy</th>
          <th style="text-align:left; padding:6px 10px; font-weight:500;">Engine file</th>
          <th style="text-align:left; padding:6px 10px; font-weight:500;">OHLCV timeframes</th>
          <th style="text-align:left; padding:6px 10px; font-weight:500;">Other data</th>
          <th style="text-align:left; padding:6px 10px; font-weight:500;">Eval bar</th>
        </tr>
      </thead>
      <tbody>
        <tr style="border-bottom:1px solid #161b22;">
          <td style="padding:7px 10px; color:#e2e8f0;">MicroRange</td>
          <td style="padding:7px 10px; font-family:monospace; color:#7dd3fc;">backtest/run.py</td>
          <td style="padding:7px 10px;">5m</td>
          <td style="padding:7px 10px; color:#64748b;">—</td>
          <td style="padding:7px 10px;">5m</td>
        </tr>
        <tr style="border-bottom:1px solid #161b22; background:#0a0f1a;">
          <td style="padding:7px 10px; color:#e2e8f0;">EMA Pullback</td>
          <td style="padding:7px 10px; font-family:monospace; color:#7dd3fc;">backtest/ema_pullback_engine.py</td>
          <td style="padding:7px 10px;">15m <span style="color:#64748b;">+</span> 4h</td>
          <td style="padding:7px 10px; color:#64748b;">—</td>
          <td style="padding:7px 10px;">15m</td>
        </tr>
        <tr style="border-bottom:1px solid #161b22;">
          <td style="padding:7px 10px; color:#e2e8f0;">Lead-Lag</td>
          <td style="padding:7px 10px; font-family:monospace; color:#7dd3fc;">tests/backtest_signals.py</td>
          <td style="padding:7px 10px;">BTC 5m <span style="color:#64748b;">+</span> Alt 5m</td>
          <td style="padding:7px 10px; color:#64748b;">—</td>
          <td style="padding:7px 10px;">5m</td>
        </tr>
        <tr style="border-bottom:1px solid #161b22; background:#0a0f1a;">
          <td style="padding:7px 10px; color:#e2e8f0;">Session Trap</td>
          <td style="padding:7px 10px; font-family:monospace; color:#7dd3fc;">backtest/run.py (session)</td>
          <td style="padding:7px 10px;">5m</td>
          <td style="padding:7px 10px; color:#64748b;">—</td>
          <td style="padding:7px 10px;">5m (session windows)</td>
        </tr>
        <tr style="border-bottom:1px solid #161b22;">
          <td style="padding:7px 10px; color:#e2e8f0;">Inside Bar Flip</td>
          <td style="padding:7px 10px; font-family:monospace; color:#7dd3fc;">backtest/insidebar_engine.py</td>
          <td style="padding:7px 10px;">1h</td>
          <td style="padding:7px 10px; color:#64748b;">—</td>
          <td style="padding:7px 10px;">1h</td>
        </tr>
        <tr style="border-bottom:1px solid #161b22; background:#0a0f1a;">
          <td style="padding:7px 10px; color:#e2e8f0;">Funding Harvest</td>
          <td style="padding:7px 10px; font-family:monospace; color:#7dd3fc;">backtest/funding_harvest_engine.py</td>
          <td style="padding:7px 10px;">5m <span style="color:#64748b;">+</span> 4h</td>
          <td style="padding:7px 10px;">Funding rate (8h)</td>
          <td style="padding:7px 10px;">Settlement windows</td>
        </tr>
        <tr style="border-bottom:1px solid #161b22;">
          <td style="padding:7px 10px; color:#e2e8f0;">Sweep Reversal</td>
          <td style="padding:7px 10px; font-family:monospace; color:#7dd3fc;">backtest/sweep_engine.py</td>
          <td style="padding:7px 10px;">15m <span style="color:#64748b;">+</span> 4h</td>
          <td style="padding:7px 10px; color:#64748b;">—</td>
          <td style="padding:7px 10px;">15m</td>
        </tr>
        <tr style="background:#0a0f1a;">
          <td style="padding:7px 10px; color:#e2e8f0;">HTF Zone Retest</td>
          <td style="padding:7px 10px; font-family:monospace; color:#7dd3fc;">backtest/zone_engine.py</td>
          <td style="padding:7px 10px;">4h <span style="color:#64748b;">+</span> 1h</td>
          <td style="padding:7px 10px;">OI snapshots</td>
          <td style="padding:7px 10px;">1h (at zone touch)</td>
        </tr>
      </tbody>
    </table>
    <div style="margin-top:10px; font-size:0.70rem; color:#374151;">
      All engines replay bar-by-bar with no look-ahead. Entry is taken at bar close. OI/funding data is time-sliced to the current bar cursor.
    </div>
  </div>

  <div style="padding: 24px 0 8px; font-size: 0.72rem; color: #374151; text-align: center;">
    All strategies share a common executor — duplicate entries blocked via in-memory <code style="background:#12141e;padding:1px 4px;border-radius:3px;color:#34d399">_pending_deals</code>
    + DB cross-process guard. Trade monitor polls every 30 s to detect TP/SL hits on the exchange.
  </div>
</div>

<script>
const ALL_SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','LINKUSDT','DOGEUSDT','SUIUSDT'];

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
  if (name === 'gates') loadGates();
}

(function initHash() {
  const tab = (location.hash || '#signals').slice(1);
  if (tab !== 'signals') showTab(tab, null);
})();

// ── Shared helpers ────────────────────────────────────────────────────────────
async function fetchJSON(url) { const r = await fetch(url); return r.json(); }
function badge(cls, text) { return `<span class="badge badge-${text}">${text}</span>`; }

// ── Gates panel ──────────────────────────────────────────────────────────────
async function loadGates() {
  try {
    const gates = await fetchJSON('/api/gates');
    const el = document.getElementById('gates-list');
    const ts = document.getElementById('gates-ts');
    ts.textContent = 'Updated ' + new Date().toLocaleTimeString();
    el.innerHTML = gates.map(g => {
      const s = g.status;
      const bg = s === 'BLOCKING' ? '#7f1d1d' : s === 'WARNING' ? '#78350f' : s === 'PARTIAL' ? '#78350f'
               : s === 'ERROR' ? '#4c1d1d' : s === 'INFO' ? '#1e293b' : '#14271a';
      const border = s === 'BLOCKING' ? '#dc2626' : s === 'WARNING' ? '#f59e0b' : s === 'PARTIAL' ? '#f59e0b'
                   : s === 'ERROR' ? '#ef4444' : s === 'INFO' ? '#3b82f6' : '#22c55e';
      const icon = s === 'BLOCKING' ? '&#128308;' : s === 'WARNING' ? '&#128992;' : s === 'PARTIAL' ? '&#128992;'
                 : s === 'ERROR' ? '&#9888;' : s === 'INFO' ? '&#128309;' : '&#128994;';
      return `<div style="display:flex;align-items:center;gap:12px;padding:10px 16px;background:${bg};border-left:3px solid ${border};border-radius:4px">
        <span style="font-size:1.1rem;min-width:20px">${icon}</span>
        <span style="font-weight:600;min-width:180px;color:#e0e0e0;font-size:0.85rem">${g.gate}</span>
        <span style="font-weight:700;min-width:80px;color:${border};font-size:0.78rem;text-transform:uppercase">${s}</span>
        <span style="color:#9ca3af;font-size:0.8rem;flex:1">${g.detail}</span>
      </div>`;
    }).join('');
  } catch(e) {
    document.getElementById('gates-list').innerHTML =
      '<div style="color:#ef4444;padding:20px">Failed to load gates: ' + e.message + '</div>';
  }
}

// ── SIGNALS / TRADES / REGIMES (shared refresh) ───────────────────────────────
async function refreshTradelog() {
  const [stats, liveSignals, firedSignals, trades, readiness] = await Promise.all([
    fetchJSON('/stats/summary'),
    fetchJSON('/signals/live'),
    fetchJSON('/signals/recent?limit=20'),
    fetchJSON('/trades/recent?limit=20'),
    fetchJSON('/signals/readiness'),
  ]);
  document.getElementById('stat-trades').textContent  = stats.total_trades;
  document.getElementById('stat-winrate').textContent = (stats.win_rate * 100).toFixed(1) + '%';
  const pnlEl = document.getElementById('stat-pnl');
  pnlEl.textContent = (stats.total_pnl_usdt >= 0 ? '+' : '') + stats.total_pnl_usdt.toFixed(2);
  pnlEl.className = 'val ' + (stats.total_pnl_usdt >= 0 ? 'green' : 'red');
  document.getElementById('stat-fired').textContent = stats.fired_today;

  // Signal Readiness panel
  if (readiness && readiness.length) {
    document.getElementById('readiness-body').innerHTML =
      readiness.map(r => {
        const pct   = r.readiness_pct || 0;
        const color = pct >= 80 ? '#22c55e'
                    : pct >= 55 ? '#fbbf24'
                    : '#6b7280';
        const bar   = `<div style="background:#12141e;border-radius:3px;
                         height:8px;width:100%;margin:4px 0">
          <div style="width:${pct}%;background:${color};
                      height:8px;border-radius:3px;
                      transition:width 0.5s"></div></div>`;
        return `<div style="display:grid;
                  grid-template-columns:90px 60px 1fr 200px;
                  gap:8px;align-items:center;padding:6px 0;
                  border-bottom:1px solid #1e2130;font-size:0.82rem">
          <span style="font-weight:600">${r.symbol}</span>
          <span style="color:${color};font-weight:700">${pct}%</span>
          <div>${bar}</div>
          <span style="color:#4b5563;font-size:0.75rem">${r.reason}</span>
        </div>`;
      }).join('');
  }

  // Circuit Breaker status
  fetchCB();

  const regimes = await Promise.all(ALL_SYMBOLS.map(s => fetchJSON('/regime/' + s)));
  document.getElementById('regime-body').innerHTML = regimes.map(r =>
    `<tr><td>${r.symbol}</td><td>${badge('regime', r.regime)}</td><td>${toIST(r.ts)}</td></tr>`
  ).join('');

  // Regime → fire threshold map (mirrors config.yaml thresholds)
  const THRESHOLDS = {
    // Main regime strategies (direction-specific)
    TREND_LONG: 0.65, TREND_SHORT: 0.82,
    RANGE_LONG: 0.60, RANGE_SHORT: 0.65,
    CRASH_SHORT: 0.75,
    PUMP_LONG: 0.50,
    BREAKOUT_LONG: 0.60, BREAKOUT_SHORT: 0.75,
    // Independent strategy loops (same threshold for both directions)
    LEADLAG_LONG: 0.65,    LEADLAG_SHORT: 0.65,
    MICRORANGE_LONG: 0.67, MICRORANGE_SHORT: 0.67,
    SESSION_LONG: 0.60,    SESSION_SHORT: 0.60,
    INSIDEBAR_LONG: 0.67,  INSIDEBAR_SHORT: 0.67,
    FUNDING_LONG: 0.70,    FUNDING_SHORT: 0.70,
    SWEEP_LONG: 0.75,      SWEEP_SHORT: 0.75,
    EMA_PULLBACK_LONG: 0.67, EMA_PULLBACK_SHORT: 0.67,
    ZONE_LONG: 0.75,       ZONE_SHORT: 0.75,
    FVG_LONG: 0.67,        FVG_SHORT: 0.67,
    BOS_LONG: 0.75,        BOS_SHORT: 0.75,
    VWAPBAND_LONG: 0.67,   VWAPBAND_SHORT: 0.67,
    OISPIKE_LONG: 0.75,    OISPIKE_SHORT: 0.75,
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
          <td>${(+s.score >= 0.30) ?
            badge('dir', s.direction)
            : '<span style="color:#4b5563">—</span>'}</td>
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
        <td>${badge('regime', t.regime || '')}</td>
        <td>${badge('dir', t.direction)}</td>
        <td>${(+t.entry).toFixed(2)}</td><td>${(+t.stop_loss).toFixed(2)}</td>
        <td>${(+t.take_profit).toFixed(2)}</td><td>${(+t.size).toFixed(4)}</td>
        <td style="color:${t.pnl_usdt>0?'#22c55e':t.pnl_usdt<0?'#ef4444':'#6b7280'}">${t.pnl_usdt!=null?(+t.pnl_usdt).toFixed(2):'open'}</td>
        <td>${t.status}</td>
      </tr>`).join('')
    : '<tr><td colspan="10" style="color:#4b5563">no trades yet</td></tr>';

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

// ── CIRCUIT BREAKER ────────────────────────────────────────────────────────────
async function fetchCB() {
  try {
    const cb = await fetchJSON('/api/circuit-breaker/status');
    const card   = document.getElementById('cb-card');
    const valEl  = document.getElementById('cb-status-val');
    const rsn    = document.getElementById('cb-reason');
    const btn    = document.getElementById('cb-reset-btn');
    if (cb.tripped) {
      valEl.textContent = 'TRIPPED';
      valEl.className   = 'val red';
      card.style.borderLeftColor = '#dc2626';
      rsn.textContent  = cb.reason || '';
      btn.style.display = 'inline-block';
    } else {
      valEl.textContent = 'OK';
      valEl.className   = 'val green';
      card.style.borderLeftColor = '#22c55e';
      rsn.textContent  = `${cb.consecutive_losses} consec losses  |  daily PnL $${cb.daily_pnl.toFixed(2)}`;
      btn.style.display = 'none';
    }
  } catch(e) {}
}
async function cbReset() {
  const btn = document.getElementById('cb-reset-btn');
  btn.disabled = true;
  btn.textContent = 'Resetting…';
  try {
    await fetch('/api/circuit-breaker/reset', { method: 'POST' });
    await fetchCB();
  } catch(e) {}
  btn.disabled = false;
  btn.textContent = '↺ Reset Breaker';
}
setInterval(fetchCB, 10000);

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
function buildEquityCurve(monthly, sc, sizingMode, trades) {
  if (eqChart) { eqChart.destroy(); eqChart = null; }
  let eq = sc; const labels=['Start'], data=[sc];
  monthly.forEach(m => { eq+=m.pnl; labels.push(m.month); data.push(+eq.toFixed(2)); });

  // Build fixed-sizing reference line from trade-level data
  const fixedData = [sc];
  if (trades && trades.length) {
    const riskPct = parseFloat(document.getElementById('bt-risk').value) / 100 || 0.01;
    let fixedEq = sc;
    // Group trades by month to align with monthly labels
    const tradesByMonth = {};
    trades.forEach(t => {
      const ts = t.exit_ts;
      if (!ts) return;
      const dt = new Date(ts);
      const mk = dt.getFullYear() + '-' + String(dt.getMonth()+1).padStart(2,'0');
      if (!tradesByMonth[mk]) tradesByMonth[mk] = [];
      tradesByMonth[mk].push(t);
    });
    monthly.forEach(m => {
      const mTrades = tradesByMonth[m.month] || [];
      mTrades.forEach(t => {
        const fixedRisk = sc * riskPct;
        const pnlR = t.pnl_r || 0;
        fixedEq += pnlR * fixedRisk;
        if (fixedEq < 0.01) fixedEq = 0.01;
      });
      fixedData.push(+fixedEq.toFixed(2));
    });
  }

  const datasets = [
    { label: sizingMode === 'fixed' ? 'Fixed Sizing' : 'Compound Sizing',
      data, fill:true, borderColor:'#a78bfa',
      backgroundColor:'rgba(167,139,250,0.1)', pointRadius:2, tension:0.3 }
  ];
  // Add reference line showing the alternative sizing mode
  if (fixedData.length === data.length && sizingMode !== 'fixed') {
    datasets.push({
      label: 'Fixed Sizing (reference)',
      data: fixedData, fill:false, borderColor:'#6b7280',
      borderDash:[6,3], pointRadius:0, tension:0.3
    });
  } else if (sizingMode === 'fixed' && fixedData.length === data.length) {
    // When in fixed mode, show what compound would look like
    // (actual curve IS fixed; we don't have compound data from server,
    //  so just show the single line)
  }

  const annotation = sizingMode === 'compound'
    ? 'Compounding: each win increases next position size'
    : 'Fixed: constant risk per trade regardless of equity';

  eqChart = new Chart(document.getElementById('eq-chart'), {
    type: 'line',
    data: { labels, datasets },
    options: { responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{ display: datasets.length > 1, labels:{color:'#9ca3af',font:{size:11}} },
        subtitle:{ display:true, text:annotation, color:'#6b7280', font:{size:11}, padding:{bottom:8} }
      },
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
  // Set default "to" date to today
  const toEl = document.getElementById('bt-to');
  if (toEl && !toEl.value) {
    toEl.value = new Date().toISOString().slice(0,10);
  }
  // Populate strategy dropdown from config.yaml via API
  try {
    const r = await fetch('/api/strategies');
    const strategies = await r.json();
    const sel = document.getElementById('bt-strat');
    const autoHtml = `<option value="auto_regime">Auto \u2014 regime switching (matches live bot)</option>
<option value="auto_regime_compound">Auto \u2014 regime switching + compound</option>`;
    const rest = strategies
      .filter(s => s.value !== 'auto_regime' && s.value !== 'auto_regime_compound')
      .map(s => `<option value="${s.value}">${s.label}</option>`)
      .join('');
    sel.innerHTML = autoHtml + rest;
  } catch(e) {
    console.error('Failed to load strategies:', e);
  }
  loadCacheStatus();
}

async function loadCacheStatus() {
  try {
    const r = await fetch('/api/backtest/cache');
    const d = await r.json();
    const syms = Object.keys(d.symbols || {}).length;
    const bars = (d.total_bars || 0).toLocaleString();
    const mb   = d.total_size_mb || 0;
    document.getElementById('cache-info-text').textContent =
      syms > 0
        ? `Local cache: ${bars} bars \u00b7 ${mb} MB \u00b7 ${syms} symbols \u2014 fast mode active`
        : 'No local cache \u2014 first run will download from Binance (~60s)';
  } catch(e) {
    document.getElementById('cache-info-text').textContent =
      'Cache status unavailable';
  }
}

async function downloadData() {
  const btn = document.getElementById('dl-btn');
  const txt = document.getElementById('cache-info-text');
  const fromDate = document.getElementById('bt-from').value || '2022-01-01';
  const toDate   = document.getElementById('bt-to').value   || '';

  btn.disabled = true;
  btn.textContent = 'Downloading...';
  txt.textContent = 'Downloading all symbols all timeframes \u2014 this runs once, ~2 min...';

  try {
    const r = await fetch('/api/backtest/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ from_date: fromDate, to_date: toDate }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    txt.textContent = d.message;
    btn.textContent = 'Update Data';
  } catch(e) {
    txt.textContent = 'Download failed: ' + e.message;
    btn.textContent = 'Retry Download';
  }
  btn.disabled = false;
  loadCacheStatus();
}

async function runBacktest() {
  const btn    = document.getElementById('bt-run-btn');
  const status = document.getElementById('bt-status');
  const app    = document.getElementById('bt-app');
  const meta   = document.getElementById('bt-meta');

  const symbol   = document.getElementById('bt-sym').value;
  const strategy = document.getElementById('bt-strat').value;
  const fromDate = document.getElementById('bt-from').value;
  const toDate   = document.getElementById('bt-to').value;
  const capital  = parseFloat(document.getElementById('bt-capital').value) || 1000;
  const riskPct  = parseFloat(document.getElementById('bt-risk').value) / 100 || 0.02;

  if (!fromDate || !toDate) { status.textContent = 'Set both dates.'; return; }
  if (fromDate >= toDate)   { status.textContent = 'From date must be before To date.'; return; }

  btn.disabled = true;
  btn.textContent = '⏳ Running…';
  status.textContent = 'Fetching data & running backtest — this may take 30–90 s…';
  app.innerHTML = '<div style="padding:60px;color:#4b5563;text-align:center">Running backtest, please wait…</div>';
  meta.textContent = '';

  let d;
  try {
    const r = await fetch('/api/backtest/run', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ symbol, strategy, from_date: fromDate, to_date: toDate, capital, risk_pct: riskPct, sizing: document.getElementById('bt-sizing').value }),
    });
    d = await r.json();
    if (d.error) throw new Error(d.error);
  } catch(e) {
    app.innerHTML = `<div style="padding:40px;color:#ef4444;text-align:center">Error: ${e.message}</div>`;
    status.textContent = '';
    btn.disabled = false; btn.textContent = '▶ Run';
    return;
  }

  btn.disabled = false; btn.textContent = '▶ Run';
  status.textContent = `Done in ${new Date().toLocaleTimeString()}`;

  // Guard against missing stats structure
  if (!d.stats || !d.stats.total) {
    app.innerHTML = `<div style="padding:40px;color:#ef4444;text-align:center">
      Backtest completed but returned no results.<br>
      Strategy: ${d.strategy}<br>
      Trades found: ${(d.trades||[]).length}<br>
      <small style="color:#6b7280">Check that strategy is in config routing table.</small>
    </div>`;
    status.textContent = '';
    return;
  }

  const sc = d.capital || 1000;
  const t  = d.stats.total;

  if (!t.trades || t.trades === 0) {
    app.innerHTML = `<div style="padding:40px;color:#fbbf24;text-align:center">
      No trades found for ${d.strategy} on ${(d.symbols||[]).join(', ')}<br>
      Period: ${fromDate} to ${toDate}<br>
      <small style="color:#6b7280">
        Try a longer date range (2023-01-01 to today) or check strategy routing.
      </small>
    </div>`;
    status.textContent = '';
    return;
  }

  const monthly = d.stats.monthly || [];
  const byReg   = d.stats.by_regime || {};
  const bySym   = d.stats.by_symbol || {};
  const trades  = d.trades || [];
  const pf      = btProfitFactor(trades);

  meta.textContent = `${d.symbols?.join(', ')} | ${d.strategy?.toUpperCase()} | Capital $${sc.toLocaleString()} | Risk ${(d.risk_pct*100).toFixed(0)}%/trade | ${fromDate} → ${toDate} | Sizing: ${d.sizing_mode || 'compound'}`;

  const kpis = [
    { label:'Starting Capital', val:'$'+sc.toLocaleString(), cls:'blue' },
    { label:'Final Equity',     val:'$'+(+t.final_equity).toLocaleString('en',{minimumFractionDigits:2}), cls:t.final_equity>=sc?'green':'red' },
    { label:'Total Return',     val:(t.total_return_pct>=0?'+':'')+t.total_return_pct+'%', cls:t.total_return_pct>=0?'green':'red' },
    { label:'Total Trades',     val:t.trades, cls:'blue' },
    { label:'Win Rate',         val:(t.win_rate*100).toFixed(1)+'%', cls:t.win_rate>=0.4?'green':'yellow' },
    { label:'Profit Factor',    val:pf, cls:parseFloat(pf)>=1.5?'green':'yellow' },
    { label:'Max Drawdown',     val:'$'+(+t.max_drawdown_usd).toFixed(0)+' ('+t.max_drawdown_pct+'%)', cls:'red' },
    { label:'Avg Win / Loss',   val:btPnlFmt(t.avg_win)+' / '+btPnlFmt(t.avg_loss), cls:'blue' },
    { label:'Win / Loss / T-O', val:`${t.wins} / ${t.losses} / ${t.timeouts}`, cls:'gray' },
    { label:'Win Streak',       val:`${t.longest_win_streak} W  /  ${t.longest_loss_streak} L`, cls:'gray' },
    { label:'Sizing Mode',     val: d.sizing_mode === 'compound' ? 'Compound' : 'Fixed', cls:'blue' },
  ];

  app.innerHTML = `
  <div class="kpi-grid">${kpis.map(k=>`
    <div class="kpi"><label>${k.label}</label><div class="v ${k.cls}">${k.val}</div></div>`).join('')}</div>
  ${monthly.length ? `
  <div class="two-col">
    <div class="bt-panel"><h2>Equity Curve</h2>
      <div class="chart-wrap"><canvas id="eq-chart"></canvas></div></div>
    <div class="bt-panel"><h2>Monthly Return %</h2>
      <div class="chart-wrap"><canvas id="bar-chart"></canvas></div></div>
  </div>` : ''}
  <div class="two-col">
    ${Object.keys(byReg).length ? `<div class="bt-panel"><h2>By Regime</h2>${btBucketTable(byReg)}</div>` : ''}
    ${Object.keys(bySym).length ? `<div class="bt-panel"><h2>By Symbol</h2>${btBucketTable(bySym)}</div>` : ''}
  </div>
  ${monthly.length ? `
  <div class="full">
    <div class="bt-panel"><h2>Monthly Returns</h2>${btMonthlyTable(monthly,sc)}</div>
  </div>` : ''}
  <div class="full">
    <div class="bt-panel"><h2>${trades.length ? 'All ' + trades.length + ' Trades' : 'No Trades'}</h2>
      ${trades.length ? btTradeTable(trades.slice(-50).reverse()) : '<p style="color:#4b5563;padding:10px">No trades were generated for this period and symbol.</p>'}
    </div>
  </div>`;

  if (monthly.length) {
    buildEquityCurve(monthly, sc, d.sizing_mode || 'compound', trades);
    buildBarChart(monthly);
  }
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


@app.get("/api/gates")
def get_gates() -> JSONResponse:
    """Return live status of every trade-blocking gate — real-time diagnostics."""
    import yaml as _yaml, time as _t
    from datetime import datetime, timezone

    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(cfg_path) as f:
        cfg = _yaml.safe_load(f)

    symbols = cfg.get("symbols", [])
    now_utc = datetime.now(timezone.utc)
    gates = []

    # 1. Circuit breaker
    try:
        from core.circuit_breaker import is_tripped, status as cb_status
        tripped = is_tripped()
        cb = cb_status()
        gates.append({
            "gate": "Circuit Breaker",
            "status": "BLOCKING" if tripped else "OK",
            "detail": cb.get("reason", "") if tripped else
                      f"PnL ${cb['daily_pnl']:+.2f}  losses={cb['consecutive_losses']}/{cb['limits']['max_consecutive']}",
        })
    except Exception as exc:
        gates.append({"gate": "Circuit Breaker", "status": "ERROR", "detail": str(exc)})

    # 2. Active positions / max open
    try:
        from core.executor import _active_deals, _pending_deals, _MAX_OPEN, _MAX_SAME_DIRECTION
        active = len(_active_deals)
        pending = len(_pending_deals)
        total = active + pending
        longs = sum(1 for _, d in _active_deals if d == "LONG")
        shorts = sum(1 for _, d in _active_deals if d == "SHORT")
        full = total >= _MAX_OPEN
        gates.append({
            "gate": "Max Positions",
            "status": "BLOCKING" if full else "OK",
            "detail": f"{total}/{_MAX_OPEN} open ({longs}L {shorts}S)  pending={pending}",
        })
        long_full = longs >= _MAX_SAME_DIRECTION
        short_full = shorts >= _MAX_SAME_DIRECTION
        gates.append({
            "gate": "Max Same Direction",
            "status": "BLOCKING" if (long_full and short_full) else
                      "PARTIAL" if (long_full or short_full) else "OK",
            "detail": f"LONG {longs}/{_MAX_SAME_DIRECTION}{'  BLOCKED' if long_full else ''}  "
                      f"SHORT {shorts}/{_MAX_SAME_DIRECTION}{'  BLOCKED' if short_full else ''}",
        })
        # List active deals
        if _active_deals:
            deal_list = [f"{s} {d}" for s, d in sorted(_active_deals)]
            gates.append({
                "gate": "Active Deals",
                "status": "INFO",
                "detail": ", ".join(deal_list),
            })
    except Exception as exc:
        gates.append({"gate": "Max Positions", "status": "ERROR", "detail": str(exc)})

    # 3. Post-trade cooldowns
    try:
        from core.executor import _post_trade_until, _symbol_direction_until
        now_mono = _t.monotonic()
        cooling = []
        for sym, until in _post_trade_until.items():
            remaining = until - now_mono
            if remaining > 0:
                cooling.append(f"{sym} {remaining/60:.0f}min")
        gates.append({
            "gate": "Post-Trade Cooldown",
            "status": "BLOCKING" if cooling else "OK",
            "detail": ", ".join(cooling) if cooling else "No symbols in cooldown",
        })
        dir_cooling = []
        for (sym, d), until in _symbol_direction_until.items():
            remaining = until - now_mono
            if remaining > 0:
                dir_cooling.append(f"{sym} {d} {remaining/60:.0f}min")
        if dir_cooling:
            gates.append({
                "gate": "Direction Cooldown",
                "status": "BLOCKING",
                "detail": ", ".join(dir_cooling),
            })
    except Exception as exc:
        gates.append({"gate": "Post-Trade Cooldown", "status": "ERROR", "detail": str(exc)})

    # 4. Weekly trend gate
    try:
        from core.weekly_trend_gate import weekly_allows_long, weekly_allows_short
        long_ok = weekly_allows_long("fvg", _cache) if _cache else None
        short_ok = weekly_allows_short("fvg", _cache) if _cache else None
        if long_ok is None:
            detail = "No cache — cannot check"
        elif long_ok and short_ok:
            detail = "LONG + SHORT both allowed"
        elif not long_ok:
            detail = "LONGs BLOCKED (BTC below 10W EMA)"
        else:
            detail = "SHORTs BLOCKED (BTC above 10W EMA)"
        blocking = (long_ok is False or short_ok is False)
        gates.append({
            "gate": "Weekly Trend Gate",
            "status": "BLOCKING" if blocking else "OK",
            "detail": detail,
        })
    except Exception as exc:
        gates.append({"gate": "Weekly Trend Gate", "status": "ERROR", "detail": str(exc)})

    # 5. ATR spike gate
    try:
        from core.filter import atr_spike_ok
        if _cache:
            blocked_syms = [s for s in symbols if not atr_spike_ok(s, _cache, tf="1h")]
            gates.append({
                "gate": "ATR Spike Gate",
                "status": "BLOCKING" if blocked_syms else "OK",
                "detail": f"Blocked: {', '.join(blocked_syms)}" if blocked_syms else "All symbols clear",
            })
        else:
            gates.append({"gate": "ATR Spike Gate", "status": "UNKNOWN", "detail": "No cache"})
    except Exception as exc:
        gates.append({"gate": "ATR Spike Gate", "status": "ERROR", "detail": str(exc)})

    # 6. Session filter
    try:
        sf = cfg.get("session_filter", {})
        wd = now_utc.weekday()
        hr = now_utc.hour
        sat_blocked = sf.get("block_saturday", True) and wd == 5
        dz_start = sf.get("dead_zone_start_utc", 22)
        dz_end = sf.get("dead_zone_end_utc", 24) % 24
        in_dead = (dz_start <= hr < 24) if dz_end == 0 else (dz_start <= hr < dz_end)
        blocked = sat_blocked or in_dead
        reason = "Saturday blocked" if sat_blocked else f"Dead zone {dz_start}:00-{dz_end or 24}:00 UTC" if in_dead else "Clear"
        gates.append({
            "gate": "Session Filter",
            "status": "BLOCKING" if blocked else "OK",
            "detail": f"{reason}  (now={now_utc.strftime('%a %H:%M')} UTC)",
        })
    except Exception as exc:
        gates.append({"gate": "Session Filter", "status": "ERROR", "detail": str(exc)})

    # 7. Account balance
    try:
        bal = _cache.get_account_balance() if _cache else 0.0
        gates.append({
            "gate": "Account Balance",
            "status": "OK" if bal > 0 else "BLOCKING",
            "detail": f"${bal:,.2f} USDT" if bal > 0 else "Balance = 0 — position_size() returns 0",
        })
    except Exception as exc:
        gates.append({"gate": "Account Balance", "status": "ERROR", "detail": str(exc)})

    # 8. Committed risk vs available equity
    try:
        from core.rr_calculator import _committed_risk
        bal = _cache.get_account_balance() if _cache else 0.0
        committed = _committed_risk()
        available = bal - committed
        pct_used = (committed / bal * 100) if bal > 0 else 0
        gates.append({
            "gate": "Committed Risk",
            "status": "BLOCKING" if available <= 0 else "WARNING" if pct_used > 80 else "OK",
            "detail": f"Committed ${committed:,.2f} / ${bal:,.2f} ({pct_used:.0f}%)  "
                      f"Available ${max(available, 0):,.2f}",
        })
    except Exception as exc:
        gates.append({"gate": "Committed Risk", "status": "ERROR", "detail": str(exc)})

    # 9. Cache warmup
    try:
        if _cache:
            warmup_issues = []
            for s in symbols:
                bars_1h = _cache.get_ohlcv(s, window=50, tf="1h")
                bars_5m = _cache.get_ohlcv(s, window=32, tf="5m")
                if not bars_1h or len(bars_1h) < 50:
                    warmup_issues.append(f"{s} 1h={len(bars_1h) if bars_1h else 0}/50")
                if not bars_5m or len(bars_5m) < 32:
                    warmup_issues.append(f"{s} 5m={len(bars_5m) if bars_5m else 0}/32")
            gates.append({
                "gate": "Cache Warmup",
                "status": "BLOCKING" if warmup_issues else "OK",
                "detail": ", ".join(warmup_issues) if warmup_issues else "All symbols warmed up",
            })
        else:
            gates.append({"gate": "Cache Warmup", "status": "BLOCKING", "detail": "No cache initialized"})
    except Exception as exc:
        gates.append({"gate": "Cache Warmup", "status": "ERROR", "detail": str(exc)})

    # 10. Disabled strategies
    try:
        disabled = []
        for s in ["leadlag", "session_trap", "zone", "vwap_band", "oi_spike",
                   "fvg", "microrange", "ema_pullback", "liq_sweep", "wyckoff_spring",
                   "cme_gap", "breakout_retest"]:
            scfg = cfg.get(s, {})
            if isinstance(scfg, dict) and not scfg.get("enabled", True):
                disabled.append(s)
        gates.append({
            "gate": "Disabled Strategies",
            "status": "INFO" if disabled else "OK",
            "detail": ", ".join(disabled) if disabled else "All strategies enabled",
        })
    except Exception as exc:
        gates.append({"gate": "Disabled Strategies", "status": "ERROR", "detail": str(exc)})

    return JSONResponse(gates)


@app.get("/api/strategies")
def get_strategies() -> JSONResponse:
    """Return all strategies currently in config.yaml routing + enabled blocks."""
    import yaml as _yaml
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(cfg_path) as f:
        cfg = _yaml.safe_load(f)

    routing = cfg.get("strategy_routing", {})
    found = set()
    for sym_key, sym_routes in routing.items():
        if sym_key == "_default" or not isinstance(sym_routes, dict):
            continue
        for regime_strats in sym_routes.values():
            for s in (regime_strats or []):
                found.add(s)

    # Also include strategies with enabled config blocks (loop-based, may not be in routing)
    _KNOWN = [
        "fvg", "microrange", "breakout_retest", "liq_sweep",
        "wyckoff_spring", "ema_pullback", "leadlag", "vwap_band",
        "cme_gap", "wyckoff_upthrust", "ema_pullback_short_v2",
        "zone", "oi_spike", "session",
    ]
    for s in _KNOWN:
        if cfg.get(s, {}).get("enabled", False):
            found.add(s)

    # Always include these base options
    found.add("main")
    found.add("breakout_retest_tp1")
    found.add("breakout_retest_tp2")

    _LABELS = {
        "fvg":                    "FVG Fill",
        "microrange":             "Micro Range",
        "breakout_retest":        "Breakout Retest (2.2R)",
        "breakout_retest_tp1":    "Breakout Retest TP1 (1.5R)",
        "breakout_retest_tp2":    "Breakout Retest TP2 (3.0R)",
        "liq_sweep":              "Liquidity Sweep",
        "wyckoff_spring":         "Wyckoff Spring",
        "wyckoff_spring_v2":      "Wyckoff Spring v2",
        "ema_pullback":           "EMA Pullback",
        "ema_pullback_short":     "EMA Pullback Short",
        "ema_pullback_short_v2":  "EMA Pullback Short v2",
        "leadlag":                "Lead Lag",
        "vwap_band":              "VWAP Band",
        "cme_gap":                "CME Gap (BTC)",
        "wyckoff_upthrust":       "Wyckoff Upthrust",
        "wyckoff_upthrust_v2":    "Wyckoff Upthrust v2",
        "zone":                   "HTF Zone",
        "oi_spike":               "OI Spike Fade",
        "session":                "Session Trap",
        "liq_sweep_short":        "Liquidity Sweep Short",
        "main":                   "Main (all signals)",
    }

    strategies = sorted(found)
    result_list = [
        {"value": "auto_regime",
         "label": "Auto \u2014 regime switching (matches live bot)"},
        {"value": "auto_regime_compound",
         "label": "Auto \u2014 regime switching + compound"},
    ]
    result_list.extend(
        {"value": s, "label": _LABELS.get(s, s.replace("_", " ").title())}
        for s in strategies
    )
    return JSONResponse(result_list)


@app.get("/api/routing")
def get_routing() -> JSONResponse:
    """Return live routing table from config.yaml — what runs on what symbol."""
    import yaml as _yaml
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(cfg_path) as f:
        cfg = _yaml.safe_load(f)

    routing  = cfg.get("strategy_routing", {})
    symbols  = cfg.get("symbols", [])

    result = {}
    for sym in symbols:
        sym_routes = routing.get(sym.upper(), routing.get("_default", {}))
        result[sym] = {}
        for regime in ["TREND", "RANGE", "BREAKOUT", "CRASH", "PUMP"]:
            strats = sym_routes.get(regime, [])
            annotated = []
            for s in strats:
                scorer_file = os.path.join(
                    os.path.dirname(__file__), "..", "core", f"{s}_scorer.py"
                )
                has_scorer = os.path.exists(scorer_file)
                enabled = cfg.get(s, {}).get("enabled", True)
                annotated.append({
                    "name":       s,
                    "has_scorer": has_scorer,
                    "enabled":    enabled,
                    "status":     "live" if (has_scorer and enabled) else
                                  "stub" if has_scorer else "missing",
                })
            result[sym][regime] = annotated

    return JSONResponse(result)


@app.get("/api/backtest/cache")
def backtest_cache_status() -> JSONResponse:
    """Return what's in the local data cache."""
    try:
        from backtest.data_store import cache_info
        return JSONResponse(cache_info())
    except Exception as exc:
        return JSONResponse({"error": str(exc), "symbols": {},
                             "total_bars": 0, "total_files": 0,
                             "total_size_mb": 0.0})


@app.post("/api/backtest/download")
async def backtest_download(request: Request) -> JSONResponse:
    """Pre-download historical data to local cache.
    Body: { from_date, to_date, symbols }
    """
    import asyncio as _asyncio

    body      = await request.json()
    from_date = str(body.get("from_date", "2022-01-01"))
    to_date   = str(body.get("to_date", ""))
    symbols   = body.get("symbols", None)  # None = all 8

    if not to_date:
        from datetime import datetime as _dt
        to_date = _dt.utcnow().strftime("%Y-%m-%d")

    def _dl():
        from backtest.fetcher import download_all_history
        return download_all_history(
            symbols=symbols,
            from_date=from_date,
            to_date=to_date,
        )

    try:
        result = await _asyncio.to_thread(_dl)
        return JSONResponse({
            "status": "ok",
            "downloaded": result,
            "message": (
                f"Cached {result['total_bars']:,} bars "
                f"({result['total_size_mb']} MB) \u2014 "
                "backtests will now run instantly"
            ),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/backtest/run")
async def api_backtest_run(request: Request) -> JSONResponse:
    """Run a backtest on demand from the web UI.

    Body JSON: { symbol, strategy, from_date, to_date, capital, risk_pct }
    Returns:   { stats, trades, symbols, capital, risk_pct, strategy }
    """
    import asyncio as _asyncio

    body      = await request.json()
    symbol    = str(body.get("symbol", "BTCUSDT")).upper()
    strategy  = str(body.get("strategy", "main"))
    from_date = str(body.get("from_date", ""))
    to_date   = str(body.get("to_date",   ""))
    capital     = float(body.get("capital",  1000))
    risk_pct    = float(body.get("risk_pct", 0.02))
    sizing_mode = str(body.get("sizing", "compound"))

    if not from_date or not to_date:
        return JSONResponse({"error": "from_date and to_date are required (YYYY-MM-DD)"}, status_code=400)

    def _run_sync():
        nonlocal sizing_mode
        import sys, os as _os
        _root = _os.path.join(_os.path.dirname(__file__), "..")
        if _root not in sys.path:
            sys.path.insert(0, _root)

        from backtest.fetcher  import fetch_period_sync
        from backtest.reporter import compute_stats
        from datetime import datetime, timezone

        def _date_ms(d):
            return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

        from_ms = _date_ms(from_date)
        to_ms   = _date_ms(to_date) + 86_400_000

        _ALL_SYMS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","LINKUSDT","DOGEUSDT","SUIUSDT"]
        symbols   = _ALL_SYMS if symbol == "ALL" else [symbol]

        # Always fetch BTCUSDT for strategies that need it as a reference
        # (weekly trend gate, lead-lag, breakout_retest BTC:1w)
        fetch_symbols = list(set(symbols + ["BTCUSDT"]))
        data    = fetch_period_sync(fetch_symbols, from_ms, to_ms, warmup_days=45)
        ohlcv   = data["ohlcv"]
        oi      = data["oi"]
        funding = data["funding"]

        # Re-use _run_strategy dispatcher from backtest.run
        import asyncio as _aio

        def _run_async(coro):
            loop = _aio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        # ── Dynamic engine loader ─────────────────────────────────────────
        # Convention: strategy "X" has backtest/X_engine.py with run().
        # Falls back to backtest/engine.py run_strategy() for strategies
        # registered in the generic vectorised engine (RUNNERS dict).
        import numpy as _np

        # Strategies handled by backtest/engine.py run_strategy() with
        # numpy arrays (need dict→numpy conversion + per-symbol dispatch)
        _GENERIC_ENGINE = {
            "breakout_retest", "breakout_retest_tp1", "breakout_retest_tp2",
        }

        trades = []

        if strategy == "main":
            from backtest.engine import run as _run
            trades = _run_async(_run(
                symbols=symbols, ohlcv=ohlcv, oi=oi, funding=funding,
                warmup_bars=210, starting_capital=capital, risk_pct=risk_pct
            ))

        elif strategy in _GENERIC_ENGINE:
            from backtest.engine import run_strategy as _bt_run

            # Convert fetched ohlcv (list[dict]) to numpy arrays for engine
            np_data: dict = {}
            for ohlcv_key, bars_list in ohlcv.items():
                if bars_list and isinstance(bars_list, list) and isinstance(bars_list[0], dict):
                    np_data[ohlcv_key] = _np.array(
                        [[b["o"], b["h"], b["l"], b["c"], b["v"], b["ts"]]
                         for b in bars_list], dtype=_np.float64)
                elif bars_list is not None and hasattr(bars_list, '__len__') and len(bars_list) > 0:
                    np_data[ohlcv_key] = _np.asarray(bars_list, dtype=_np.float64)

            all_trades = []
            btc_keys = {k for k in np_data if k.startswith("BTCUSDT:")}
            btc_data = {k: np_data[k] for k in btc_keys} if btc_keys else None
            for sym in symbols:
                sym_data = {k: v for k, v in np_data.items() if k.startswith(f"{sym}:")}
                if not sym_data:
                    continue
                sym_trades = _bt_run(sym, strategy, sym_data, btc_data,
                                     from_ms, to_ms)
                all_trades.extend(sym_trades)
            trades = all_trades

        elif strategy in ("auto_regime", "auto_regime_compound"):
            # ── Regime-switching backtest ────────────────────────────────
            import yaml as _yaml_bt
            cfg_bt_path = _os.path.join(_os.path.dirname(__file__), "..", "config.yaml")
            with open(cfg_bt_path) as _cf:
                _cfg_bt = _yaml_bt.safe_load(_cf)

            routing = _cfg_bt.get("strategy_routing", {})
            weekly_gate_cfg = _cfg_bt.get("weekly_trend_gate", {})
            weekly_enabled  = weekly_gate_cfg.get("enabled", True)
            ema_period      = int(weekly_gate_cfg.get("ema_period", 10))

            if strategy == "auto_regime_compound":
                sizing_mode = "compound"

            def _detect_regime_from_bars(sym_dr: str, bars_4h: list, bars_1d: list) -> str:
                if not bars_4h or len(bars_4h) < 15:
                    return "TREND"
                highs  = [b["h"] for b in bars_4h[-15:]]
                lows   = [b["l"] for b in bars_4h[-15:]]
                closes = [b["c"] for b in bars_4h[-15:]]
                if bars_1d and len(bars_1d) >= 8:
                    change_7d = (bars_1d[-1]["c"] - bars_1d[-8]["c"]) / bars_1d[-8]["c"]
                    ema50_val = sum(b["c"] for b in bars_1d[-50:]) / min(50, len(bars_1d))
                    price_now = bars_1d[-1]["c"]
                    if change_7d < -0.12 and price_now < ema50_val:
                        return "CRASH"
                    if change_7d >  0.12 and price_now > ema50_val:
                        return "PUMP"
                recent_range = (max(highs[-5:]) - min(lows[-5:])) / closes[-1]
                long_range   = (max(highs) - min(lows)) / closes[-1]
                if recent_range < 0.012 and long_range < 0.06:
                    return "RANGE"
                ema_fast = sum(closes[-5:])  / 5
                ema_slow = sum(closes[-15:]) / 15
                if ema_fast > ema_slow * 1.005:
                    return "TREND"
                if ema_fast < ema_slow * 0.995:
                    return "TREND"
                return "RANGE"

            # Import all available engines
            _ENGINE_MAP = {}
            for _strat_name in [
                "breakout_retest", "breakout_retest_tp1",
                "fvg", "liq_sweep", "liq_sweep_short",
                "wyckoff_spring", "wyckoff_upthrust",
                "ema_pullback", "ema_pullback_short",
                "microrange", "cme_gap",
            ]:
                try:
                    import importlib as _imp
                    _eng = _imp.import_module(f"backtest.{_strat_name}_engine")
                    if hasattr(_eng, "run"):
                        _ENGINE_MAP[_strat_name] = _eng
                except (ImportError, ModuleNotFoundError):
                    pass

            # Also try generic engine for breakout_retest variants
            try:
                from backtest.engine import run_strategy as _bt_generic
                for _gs in ("breakout_retest", "breakout_retest_tp1",
                            "breakout_retest_tp2"):
                    if _gs not in _ENGINE_MAP:
                        _ENGINE_MAP[_gs] = None  # use generic
            except ImportError:
                pass

            all_regime_trades = []
            _already_run = set()

            for sym in symbols:
                sym_routing = routing.get(sym.upper(), routing.get("_default", {}))

                # Get 4H and 1D bars for regime detection
                key_4h = f"{sym}:4h"
                key_1d = f"{sym}:1d"
                bars_4h_raw = ohlcv.get(key_4h, [])
                bars_1d_raw = ohlcv.get(key_1d, [])

                if bars_4h_raw and isinstance(bars_4h_raw[0], dict):
                    bars_4h_list = bars_4h_raw
                    bars_1d_list = bars_1d_raw if bars_1d_raw else []
                else:
                    bars_4h_list = []
                    bars_1d_list = []

                mid_idx = len(bars_4h_list) // 2
                regime_detected = _detect_regime_from_bars(
                    sym,
                    bars_4h_list[max(0, mid_idx-15): mid_idx+1],
                    bars_1d_list[max(0, mid_idx//6 - 8): mid_idx//6 + 1]
                    if bars_1d_list else []
                )

                active_strats = sym_routing.get(regime_detected, [])
                if not active_strats:
                    active_strats = sym_routing.get("TREND", [])

                for strat in active_strats:
                    run_key = (sym, strat)
                    if run_key in _already_run:
                        continue
                    _already_run.add(run_key)

                    if strat not in _ENGINE_MAP:
                        continue

                    eng = _ENGINE_MAP[strat]
                    try:
                        if eng is None:
                            import numpy as _np2
                            np_d = {}
                            for ok2, bl2 in ohlcv.items():
                                if ok2.startswith(f"{sym}:") or ok2.startswith("BTCUSDT:"):
                                    if bl2 and isinstance(bl2, list) and isinstance(bl2[0], dict):
                                        np_d[ok2] = _np.array([[b["o"],b["h"],b["l"],b["c"],b["v"],b["ts"]] for b in bl2], dtype=_np.float64)
                                    elif bl2 is not None:
                                        np_d[ok2] = _np.asarray(bl2, dtype=_np.float64)
                            btck = {k: np_d[k] for k in np_d if k.startswith("BTCUSDT:")}
                            sk   = {k: np_d[k] for k in np_d if k.startswith(f"{sym}:")}
                            if sk:
                                from backtest.engine import run_strategy as _btr
                                sym_trades = _btr(sym, strat, sk, btck or None, from_ms, to_ms)
                                all_regime_trades.extend(sym_trades)
                        else:
                            import inspect as _ins
                            sig = _ins.signature(eng.run)
                            kwargs = dict(symbols=[sym], ohlcv=ohlcv,
                                          starting_capital=capital,
                                          risk_pct=risk_pct)
                            if "oi" in sig.parameters:
                                kwargs["oi"] = oi
                            if "funding" in sig.parameters:
                                kwargs["funding"] = funding
                            sym_trades = eng.run(**kwargs)
                            all_regime_trades.extend(sym_trades)
                    except Exception as _e:
                        import logging as _log2
                        _log2.getLogger(__name__).debug(
                            "auto_regime engine %s/%s failed: %s", sym, strat, _e)

            trades = all_regime_trades

        else:
            # Try dedicated engine file: backtest/{strategy}_engine.py
            import importlib, inspect
            engine_module = f"backtest.{strategy}_engine"
            try:
                eng = importlib.import_module(engine_module)
                if not hasattr(eng, 'run'):
                    raise ImportError(f"No run() in {engine_module}")
                sig = inspect.signature(eng.run)
                if 'oi' in sig.parameters:
                    trades = eng.run(
                        symbols=symbols, ohlcv=ohlcv, oi=oi,
                        starting_capital=capital, risk_pct=risk_pct
                    )
                else:
                    trades = eng.run(
                        symbols=symbols, ohlcv=ohlcv,
                        starting_capital=capital, risk_pct=risk_pct
                    )
            except (ImportError, ModuleNotFoundError):
                # Fall back to generic engine run_strategy()
                try:
                    from backtest.engine import run_strategy as _bt_run2
                    np_data2: dict = {}
                    for ok, bl in ohlcv.items():
                        if bl and isinstance(bl, list) and isinstance(bl[0], dict):
                            np_data2[ok] = _np.array(
                                [[b["o"], b["h"], b["l"], b["c"], b["v"], b["ts"]]
                                 for b in bl], dtype=_np.float64)
                        elif bl is not None and hasattr(bl, '__len__') and len(bl) > 0:
                            np_data2[ok] = _np.asarray(bl, dtype=_np.float64)
                    btc_keys2 = {k for k in np_data2 if k.startswith("BTCUSDT:")}
                    btc_data2 = {k: np_data2[k] for k in btc_keys2} if btc_keys2 else None
                    all_trades2 = []
                    for sym in symbols:
                        sd = {k: v for k, v in np_data2.items() if k.startswith(f"{sym}:")}
                        if sd:
                            all_trades2.extend(_bt_run2(sym, strategy, sd, btc_data2,
                                                        from_ms, to_ms))
                    trades = all_trades2
                except Exception as eng_exc:
                    return {
                        "error": f"No engine for '{strategy}'. "
                                 f"Create backtest/{strategy}_engine.py with run(). "
                                 f"Detail: {eng_exc}"
                    }

        try:
            stats = compute_stats(trades, starting_capital=capital)
        except Exception:
            # Fallback for engine Trade namedtuples (breakout_retest etc.)
            from backtest.engine import compute_stats as _eng_stats
            from backtest.run import compute_dollar_stats as _dol_stats
            raw = _eng_stats(trades)
            ds  = _dol_stats(trades, capital, risk_pct=risk_pct,
                            sizing_mode=sizing_mode)
            stats = {
                "total": {
                    "trades":            raw.get("n", 0),
                    "wins":              raw.get("wins", 0),
                    "losses":            raw.get("losses", 0),
                    "timeouts":          raw.get("timeouts", 0),
                    "win_rate":          raw.get("wr", 0) / 100,
                    "pf":                raw.get("pf", 0),
                    "avg_r":             raw.get("avg_r", 0),
                    "final_equity":      ds["final_balance"],
                    "total_return_pct":  ds["total_return_pct"],
                    "max_drawdown_usd":  ds["max_drawdown_usd"],
                    "max_drawdown_pct":  ds["max_drawdown_pct"],
                    "avg_win":           ds["avg_win_usd"],
                    "avg_loss":          ds["avg_loss_usd"],
                    "longest_win_streak":  ds["max_consec_wins"],
                    "longest_loss_streak": ds["max_consec_loss"],
                    "sharpe":              ds.get("sharpe", 0.0),
                },
                "monthly":   ds.get("monthly", []),
                "by_regime": ds.get("by_regime", {}),
                "by_symbol": {},
            }
        # Resolve bar_idx → Unix ms timestamp using ohlcv bar data
        def _bar_ts(sym: str, bar_idx: int) -> int:
            """Return Unix ms timestamp for bar_idx, or 0 if unavailable."""
            for tf in ("5m", "15m", "1h"):
                bars = ohlcv.get(f"{sym}:{tf}")
                if bars is None or not hasattr(bars, '__len__') or bar_idx >= len(bars):
                    continue
                try:
                    row = bars[bar_idx]
                    # numpy array: row[5]; dict: row["ts"]
                    ts = row["ts"] if isinstance(row, dict) else row[5]
                    return int(ts)
                except (IndexError, TypeError, KeyError):
                    continue
            return 0

        # Convert trades to JS-compatible dicts with dollar PnL
        from backtest.run import compute_dollar_stats as _ds_fn
        ds_for_trades = _ds_fn(trades, capital, risk_pct=risk_pct,
                               sizing_mode=sizing_mode)
        pnl_pairs = ds_for_trades.get("trade_pnls", [])
        sorted_trades = sorted(trades or [], key=lambda t: getattr(t, 'bar_idx', 0))
        equity = capital
        trades_serializable = []
        for i, t in enumerate(sorted_trades):
            pnl_val, risk_val = pnl_pairs[i] if i < len(pnl_pairs) else (0.0, 0.0)
            equity = round(equity + pnl_val, 2)
            d_raw = t.__dict__ if hasattr(t, "__dict__") else (
                    t._asdict() if hasattr(t, "_asdict") else
                    dict(t) if isinstance(t, dict) else {})
            trades_serializable.append({
                "symbol":       d_raw.get("symbol", ""),
                "direction":    d_raw.get("direction", ""),
                "regime":       d_raw.get("strategy", d_raw.get("regime", "")),
                "outcome":      d_raw.get("outcome", ""),
                "score":        d_raw.get("pnl_r", 0),
                "pnl_r":        d_raw.get("pnl_r", 0),
                "pnl":          round(pnl_val, 2),
                "risk_amount":  round(risk_val, 2),
                "equity_after": equity,
                "entry":        d_raw.get("entry", 0),
                "stop":         d_raw.get("stop", 0),
                "tp":           d_raw.get("tp", 0),
                "bar_idx":      d_raw.get("bar_idx", 0),
                "exit_ts":      _bar_ts(d_raw.get("symbol", ""), d_raw.get("bar_idx", 0)),
            })

        return {
            "stats":       stats,
            "trades":      trades_serializable,
            "symbols":     symbols,
            "capital":     capital,
            "risk_pct":    risk_pct,
            "strategy":    strategy,
            "sizing_mode": sizing_mode,
        }

    try:
        result = await _asyncio.to_thread(_run_sync)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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
  .badge-LONG    { background:#14532d; color:#bbf7d0; }
  .badge-SHORT   { background:#7f1d1d; color:#fecaca; }
  .badge-TREND   { background:#1d4ed8; color:#bfdbfe; }
  .badge-RANGE   { background:#713f12; color:#fef3c7; }
  .badge-CRASH   { background:#7f1d1d; color:#fecaca; }
  .badge-PUMP    { background:#14532d; color:#bbf7d0; }
  .badge-BREAKOUT{ background:#1d4ed8; color:#bfdbfe; }
  .badge-LEADLAG    { background:#0f3460; color:#93c5fd; }
  .badge-MICRORANGE { background:#3b0764; color:#e9d5ff; }
  .badge-SESSION    { background:#164e63; color:#a5f3fc; }
  .badge-EMA_PULLBACK { background:#052e16; color:#86efac; }
  .badge-ZONE       { background:#1e1b4b; color:#c7d2fe; }
  .badge-FVG        { background:#14532d; color:#6ee7b7; }
  .badge-VWAPBAND   { background:#134e4a; color:#5eead4; }
  .badge-OISPIKE    { background:#4a044e; color:#f5d0fe; }
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

  if (!d.stats || !d.stats.total) {
    document.getElementById('app').innerHTML =
      `<div style="padding:40px;color:#ef4444;text-align:center">
        Backtest returned no results. Run a backtest first.
      </div>`;
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
    """Live market conditions for all 8 configured symbols — prices, regime, ADX, funding,
    plus Coinglass paid data: OI 24h trend, L/S ratio, liquidation heatmap clusters."""
    symbols = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT",
               "XRPUSDT","LINKUSDT","DOGEUSDT","SUIUSDT"]
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


@app.get("/signals/readiness")
async def signal_readiness() -> JSONResponse:
    """Return how close each symbol is to firing a breakout_retest signal."""
    if _cache is None:
        return JSONResponse([])

    symbols = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT',
               'XRPUSDT','LINKUSDT','DOGEUSDT','SUIUSDT']
    result = []

    for sym in symbols:
        try:
            bars_5m = _cache.get_ohlcv(sym, window=50, tf="5m")
            bars_4h = _cache.get_ohlcv(sym, window=25, tf="4h")

            if not bars_5m or len(bars_5m) < 30:
                result.append({
                    "symbol": sym, "readiness_pct": 0,
                    "range_valid": False, "vol_ready": False,
                    "htf_bear": False, "htf_bull": False,
                    "state": "NO_DATA", "width_pct": 0,
                    "vol_ratio": 0, "proximity_pct": 0,
                    "reason": "Insufficient 5m bars",
                })
                continue

            # Range check
            RANGE_BARS = 8
            window = bars_5m[-(RANGE_BARS + 1):-1]
            rng_high = max(b["h"] for b in window)
            rng_low  = min(b["l"] for b in window)
            mid      = (rng_high + rng_low) / 2.0
            width    = (rng_high - rng_low) / mid if mid > 0 else 0
            range_valid = 0.0010 <= width <= 0.0200

            # Volume check
            vols = [b["v"] for b in bars_5m[-20:] if b.get("v", 0) > 0]
            vol_ma = sum(vols) / len(vols) if vols else 0
            cur_vol = bars_5m[-1].get("v", 0)
            vol_ratio = (cur_vol / vol_ma) if vol_ma > 0 else 0
            vol_ready = vol_ratio >= 1.25

            # HTF direction
            htf_bear = htf_bull = True
            if bars_4h and len(bars_4h) >= 21:
                closes_4h = [b["c"] for b in bars_4h]
                k = 2.0 / 22
                ema = sum(closes_4h[:21]) / 21
                for c in closes_4h[21:]:
                    ema = c * k + ema * (1 - k)
                htf_bull = closes_4h[-1] > ema
                htf_bear = closes_4h[-1] < ema

            # Proximity to breakout
            bar_close = bars_5m[-1]["c"]
            dist_to_low  = abs(bar_close - rng_low)  / mid * 100
            dist_to_high = abs(bar_close - rng_high) / mid * 100
            nearest_dist = min(dist_to_low, dist_to_high)
            half_width = width / 2 * 100
            proximity_pct = max(0, 100 - (nearest_dist / half_width * 100)) if half_width > 0 else 0
            direction = "SHORT" if dist_to_low < dist_to_high else "LONG"

            # Composite readiness score
            score = 0
            if range_valid:       score += 35
            if htf_bear:          score += 20
            if vol_ratio >= 0.80: score += 15
            if vol_ready:         score += 15
            score += min(15, int(proximity_pct * 0.15))

            if not range_valid:
                reason = f"Range too tight/wide ({width*100:.3f}%)"
            elif not htf_bear and not htf_bull:
                reason = "No HTF direction"
            elif not vol_ready:
                reason = f"Low volume ({vol_ratio:.2f}x, need 1.25x)"
            else:
                reason = f"Ready — watching for {direction} breakout"

            result.append({
                "symbol":        sym,
                "readiness_pct": min(score, 100),
                "range_valid":   range_valid,
                "width_pct":     round(width * 100, 4),
                "vol_ready":     vol_ready,
                "vol_ratio":     round(vol_ratio, 2),
                "htf_bear":      htf_bear,
                "htf_bull":      htf_bull,
                "state":         "IDLE",
                "proximity_pct": round(proximity_pct, 1),
                "direction":     direction,
                "rng_low":       round(rng_low, 6),
                "rng_high":      round(rng_high, 6),
                "bar_close":     round(bar_close, 6),
                "reason":        reason,
            })

        except Exception as exc:
            result.append({
                "symbol": sym, "readiness_pct": 0,
                "reason": str(exc), "state": "ERROR",
            })

    result.sort(key=lambda x: x["readiness_pct"], reverse=True)
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
const SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','LINKUSDT','DOGEUSDT','SUIUSDT'];

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
