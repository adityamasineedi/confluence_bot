"""FastAPI metrics server — exposes trade stats and signal history via HTTP."""
import os
import sqlite3
from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="confluence_bot metrics", version="0.1.0")

_DB_PATH = os.environ.get("DB_PATH", "confluence_bot.db")


def _get_conn() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    except Exception:
        conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


# ── Live dashboard ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>confluence_bot dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', monospace; }
  header { background: #1a1d27; padding: 16px 24px; border-bottom: 1px solid #2a2d3a;
           display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 1.2rem; font-weight: 600; color: #a78bfa; }
  #status-dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e;
                box-shadow: 0 0 8px #22c55e; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 16px; padding: 20px; }
  .card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 18px; }
  .card h3 { font-size: 0.75rem; color: #6b7280; text-transform: uppercase;
             letter-spacing: .06em; margin-bottom: 8px; }
  .card .val { font-size: 2rem; font-weight: 700; }
  .green { color: #22c55e; } .red { color: #ef4444; } .purple { color: #a78bfa; }
  .blue  { color: #60a5fa; }
  section { padding: 0 20px 20px; }
  section h2 { font-size: 0.85rem; color: #6b7280; text-transform: uppercase;
               letter-spacing: .06em; margin-bottom: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th { background: #1a1d27; color: #6b7280; padding: 8px 10px; text-align: left;
       border-bottom: 1px solid #2a2d3a; font-weight: 500; }
  td { padding: 8px 10px; border-bottom: 1px solid #1e2130; }
  tr:hover td { background: #1e2130; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 0.7rem; font-weight: 600; }
  .badge-TREND  { background: #1d4ed8; color: #bfdbfe; }
  .badge-RANGE  { background: #713f12; color: #fef3c7; }
  .badge-CRASH  { background: #7f1d1d; color: #fecaca; }
  .badge-LONG   { background: #14532d; color: #bbf7d0; }
  .badge-SHORT  { background: #7f1d1d; color: #fecaca; }
  .badge-FIRE   { background: #7c3aed; color: #ede9fe; }
  #refresh-ts   { margin-left: auto; font-size: 0.72rem; color: #4b5563; }
</style>
</head>
<body>
<header>
  <div id="status-dot"></div>
  <h1>confluence_bot</h1>
  <span id="refresh-ts">loading…</span>
</header>

<div class="grid">
  <div class="card">
    <h3>Total Trades</h3>
    <div class="val blue" id="stat-trades">—</div>
  </div>
  <div class="card">
    <h3>Win Rate</h3>
    <div class="val green" id="stat-winrate">—</div>
  </div>
  <div class="card">
    <h3>Total PnL (USDT)</h3>
    <div class="val" id="stat-pnl">—</div>
  </div>
  <div class="card">
    <h3>Signals Fired Today</h3>
    <div class="val purple" id="stat-fired">—</div>
  </div>
</div>

<section>
  <h2>Current Regimes</h2>
  <table>
    <thead><tr><th>Symbol</th><th>Regime</th><th>Since</th></tr></thead>
    <tbody id="regime-body"><tr><td colspan="3" style="color:#4b5563">loading…</td></tr></tbody>
  </table>
</section>

<section>
  <h2>Recent Signals</h2>
  <table>
    <thead><tr><th>Time</th><th>Symbol</th><th>Regime</th><th>Dir</th><th>Score</th><th>Fire</th></tr></thead>
    <tbody id="signals-body"><tr><td colspan="6" style="color:#4b5563">loading…</td></tr></tbody>
  </table>
</section>

<section>
  <h2>Recent Trades</h2>
  <table>
    <thead><tr><th>Time</th><th>Symbol</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>Size</th><th>PnL</th><th>Status</th></tr></thead>
    <tbody id="trades-body"><tr><td colspan="9" style="color:#4b5563">loading…</td></tr></tbody>
  </table>
</section>

<script>
const symbols = ['BTCUSDT', 'ETHUSDT'];

async function fetchJSON(url) {
  const r = await fetch(url);
  return r.json();
}

function badge(cls, text) {
  return `<span class="badge badge-${text}">${text}</span>`;
}

async function refresh() {
  const [stats, signals, trades] = await Promise.all([
    fetchJSON('/stats/summary'),
    fetchJSON('/signals/recent?limit=30'),
    fetchJSON('/trades/recent?limit=20'),
  ]);

  // Stats
  document.getElementById('stat-trades').textContent   = stats.total_trades;
  document.getElementById('stat-winrate').textContent  = (stats.win_rate * 100).toFixed(1) + '%';
  const pnlEl = document.getElementById('stat-pnl');
  pnlEl.textContent = (stats.total_pnl_usdt >= 0 ? '+' : '') + stats.total_pnl_usdt.toFixed(2);
  pnlEl.className = 'val ' + (stats.total_pnl_usdt >= 0 ? 'green' : 'red');
  document.getElementById('stat-fired').textContent = stats.fired_today;

  // Regimes
  const regimes = await Promise.all(symbols.map(s => fetchJSON('/regime/' + s)));
  document.getElementById('regime-body').innerHTML = regimes.map(r =>
    `<tr><td>${r.symbol}</td><td>${badge('regime', r.regime)}</td><td>${r.ts || '—'}</td></tr>`
  ).join('');

  // Signals
  document.getElementById('signals-body').innerHTML = signals.length
    ? signals.map(s => `<tr>
        <td>${s.ts ? s.ts.slice(11,19) : ''}</td>
        <td>${s.symbol}</td>
        <td>${badge('regime', s.regime)}</td>
        <td>${badge('dir', s.direction)}</td>
        <td>${(+s.score * 100).toFixed(0)}%</td>
        <td>${s.fire ? '<span class="badge badge-FIRE">FIRE</span>' : ''}</td>
      </tr>`).join('')
    : '<tr><td colspan="6" style="color:#4b5563">no signals yet</td></tr>';

  // Trades
  document.getElementById('trades-body').innerHTML = trades.length
    ? trades.map(t => `<tr>
        <td>${t.ts ? t.ts.slice(11,19) : ''}</td>
        <td>${t.symbol}</td>
        <td>${badge('dir', t.direction)}</td>
        <td>${(+t.entry).toFixed(2)}</td>
        <td>${(+t.stop_loss).toFixed(2)}</td>
        <td>${(+t.take_profit).toFixed(2)}</td>
        <td>${(+t.size).toFixed(4)}</td>
        <td style="color:${t.pnl_usdt>0?'#22c55e':t.pnl_usdt<0?'#ef4444':'#6b7280'}">${t.pnl_usdt!=null?(+t.pnl_usdt).toFixed(2):'open'}</td>
        <td>${t.status}</td>
      </tr>`).join('')
    : '<tr><td colspan="9" style="color:#4b5563">no trades yet</td></tr>';

  document.getElementById('refresh-ts').textContent =
    'updated ' + new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh, 5000);   // auto-refresh every 5 s
</script>
</body>
</html>""")


def start(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port)
