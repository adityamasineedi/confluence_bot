"""Telegram command listener — responds to bot commands sent to the chat.

Supported commands:
  /market  — full market snapshot: price, regime, OI trend, funding, L/S ratio,
              liquidation heatmap clusters for every tracked symbol.
  /status  — brief bot health: uptime, open trades, circuit breaker state.

Uses getUpdates long-polling (no webhook required).
No-ops when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.
"""
import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID",   "")
_BASE_URL = "https://api.telegram.org/bot{token}"

# Regime → emoji
_REGIME_EMOJI = {
    "TREND":    "📈",
    "RANGE":    "↔️",
    "CRASH":    "💥",
    "PUMP":     "🚀",
    "BREAKOUT": "🔔",
    "BEAR":     "🐻",
}


def _enabled() -> bool:
    return bool(_TOKEN and _CHAT_ID)


# ── Low-level HTTP helpers (sync, called via asyncio.to_thread) ──────────────

def _api_get(method: str, params: dict) -> dict | None:
    url  = f"{_BASE_URL.format(token=_TOKEN)}/{method}"
    qs   = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{url}?{qs}", timeout=35) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.debug("Telegram getUpdates error: %s", exc)
        return None


def _api_send(chat_id: str, text: str) -> None:
    url  = f"{_BASE_URL.format(token=_TOKEN)}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)


# ── Market snapshot formatter ─────────────────────────────────────────────────

def _oi_change_pct(cache, symbol: str) -> float | None:
    """24-hour OI change % from Coinglass history (oldest vs newest reading)."""
    history = cache.get_oi_history(symbol, window=24)
    if len(history) < 2:
        return None
    oldest = history[0]
    newest = history[-1]
    if oldest == 0:
        return None
    return (newest - oldest) / oldest * 100.0


def _funding_label(rate: float | None) -> str:
    if rate is None:
        return "—"
    pct = rate * 100.0
    if rate > 0.0010:
        tag = "🔴 extreme+"
    elif rate > 0.0003:
        tag = "🟠 +"
    elif rate > 0:
        tag = "🟡 +"
    elif rate > -0.0003:
        tag = "🟡 "
    elif rate > -0.0010:
        tag = "🟢 "
    else:
        tag = "🔵 extreme-"
    return f"{tag}{pct:+.4f}%"


def _ls_label(ratio: float | None) -> str:
    if ratio is None:
        return "—"
    if ratio > 1.8:
        return f"{ratio:.2f} 🔴 crowded LONG"
    if ratio < 0.6:
        return f"{ratio:.2f} 🟢 crowded SHORT"
    return f"{ratio:.2f} neutral"


def _nearest_clusters(cache, symbol: str, price: float) -> tuple[str, str]:
    """Return (below_str, above_str) for the nearest significant liq clusters."""
    clusters = cache.get_liq_clusters(symbol)
    if not clusters or price == 0:
        return "—", "—"

    below = [c for c in clusters if c["price"] < price]
    above = [c for c in clusters if c["price"] > price]

    # Nearest below = highest price below current; nearest above = lowest price above
    nearest_below = max(below, key=lambda c: c["price"]) if below else None
    nearest_above = min(above, key=lambda c: c["price"]) if above else None

    def _fmt(c: dict, ref: float) -> str:
        dist_pct = abs(c["price"] - ref) / ref * 100.0
        size_m   = c["size_usd"] / 1_000_000.0
        side_tag = "🟢" if c["side"] == "buy" else "🔴"
        return f"{side_tag} ${c['price']:,.2f}  ({dist_pct:.1f}% away, ${size_m:.1f}M)"

    below_str = _fmt(nearest_below, price) if nearest_below else "—"
    above_str = _fmt(nearest_above, price) if nearest_above else "—"
    return below_str, above_str


def _format_market(cache, symbols: list[str]) -> str:
    """Build the full /market message from live cache data."""
    from core.regime_detector import detect_regime

    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines   = [f"📊 <b>Market Snapshot</b>  {now_utc}\n"]

    for symbol in symbols:
        price = cache.get_last_price(symbol)
        if price == 0.0:
            continue

        try:
            regime     = str(detect_regime(symbol, cache))
        except Exception:
            regime = "?"
        regime_emoji = _REGIME_EMOJI.get(regime, "📊")

        oi_chg    = _oi_change_pct(cache, symbol)
        funding   = cache.get_funding_rate(symbol)
        ls_ratio  = cache.get_long_short_ratio(symbol)
        liq_below, liq_above = _nearest_clusters(cache, symbol, price)

        oi_str = f"{oi_chg:+.1f}% 24h" if oi_chg is not None else "—"

        # Ticker header
        ticker = symbol.replace("USDT", "")
        lines.append(
            f"<b>{ticker}</b>  {regime_emoji} {regime}  "
            f"<code>${price:,.4f}</code>"
        )
        lines.append(f"  OI:     {oi_str}")
        lines.append(f"  Fund:   {_funding_label(funding)}")
        lines.append(f"  L/S:    {_ls_label(ls_ratio)}")
        lines.append(f"  Liq ↓:  {liq_below}")
        lines.append(f"  Liq ↑:  {liq_above}")
        lines.append("")

    # ── Market-wide summary ───────────────────────────────────────────────
    all_funding = [cache.get_funding_rate(s) for s in symbols
                   if cache.get_funding_rate(s) is not None]
    all_ls      = [cache.get_long_short_ratio(s) for s in symbols
                   if cache.get_long_short_ratio(s) is not None]

    if all_funding:
        avg_fund = sum(all_funding) / len(all_funding)
        extreme  = sum(1 for r in all_funding if abs(r) > 0.001)
        lines.append(
            f"<b>Avg funding:</b> {avg_fund*100:+.4f}%  "
            f"({extreme}/{len(all_funding)} extreme)"
        )
    if all_ls:
        avg_ls = sum(all_ls) / len(all_ls)
        lines.append(f"<b>Avg L/S ratio:</b> {avg_ls:.2f}")

    if not all_funding and not all_ls:
        lines.append("<i>Coinglass data not yet populated (API key required)</i>")

    return "\n".join(lines)


def _format_status() -> str:
    """Build /status message: circuit breaker, open trades, uptime."""
    import sqlite3, os as _os
    db_path = _os.environ.get("DB_PATH", "confluence_bot.db")

    try:
        from core.circuit_breaker import status as cb_status, is_tripped
        cb     = cb_status()
        cb_str = (
            f"🔴 TRIPPED — {cb.get('reason', '?')}"
            if is_tripped()
            else "🟢 OK"
        )
    except Exception:
        cb_str = "?"

    try:
        with sqlite3.connect(db_path) as conn:
            open_n = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='OPEN'"
            ).fetchone()[0]
            today_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdt),0) FROM trades "
                "WHERE DATE(ts)=DATE('now')"
            ).fetchone()[0]
    except Exception:
        open_n, today_pnl = "?", "?"

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sign    = "+" if isinstance(today_pnl, float) and today_pnl >= 0 else ""

    return (
        f"🤖 <b>Bot Status</b>  {now_utc}\n\n"
        f"Circuit breaker: {cb_str}\n"
        f"Open trades:     {open_n}\n"
        f"Today PnL:       {sign}{today_pnl:.2f} USDT\n"
        if isinstance(today_pnl, float)
        else (
            f"🤖 <b>Bot Status</b>  {now_utc}\n\n"
            f"Circuit breaker: {cb_str}\n"
            f"Open trades:     {open_n}\n"
        )
    )


# ── Command dispatcher ────────────────────────────────────────────────────────

async def _handle_update(update: dict, cache, symbols: list[str]) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    text    = message.get("text", "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    # Only respond to the configured chat (security gate)
    if chat_id != _CHAT_ID:
        return

    cmd = text.split()[0].split("@")[0].lower() if text else ""

    if cmd == "/market":
        reply = await asyncio.to_thread(_format_market, cache, symbols)
        await asyncio.to_thread(_api_send, chat_id, reply)

    elif cmd == "/status":
        reply = await asyncio.to_thread(_format_status)
        await asyncio.to_thread(_api_send, chat_id, reply)

    elif cmd == "/help":
        reply = (
            "📋 <b>Available commands</b>\n\n"
            "/market — live OI, funding, L/S ratio & liq clusters per symbol\n"
            "/status — circuit breaker, open trades, today's PnL\n"
            "/help   — this message"
        )
        await asyncio.to_thread(_api_send, chat_id, reply)


# ── Main polling loop ─────────────────────────────────────────────────────────

async def start_command_listener(cache, symbols: list[str]) -> None:
    """Long-poll Telegram getUpdates and dispatch commands forever.

    Called as an asyncio.Task from main.py.
    Silently no-ops when Telegram env vars are not set.
    """
    if not _enabled():
        log.info("Telegram command listener disabled (no TOKEN/CHAT_ID)")
        return

    log.info("Telegram command listener started")
    offset = 0

    while True:
        try:
            result = await asyncio.to_thread(
                _api_get, "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": '["message"]'},
            )
            if result and result.get("ok"):
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    asyncio.create_task(_handle_update(update, cache, symbols))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.debug("Command listener poll error: %s", exc)
            await asyncio.sleep(5)
