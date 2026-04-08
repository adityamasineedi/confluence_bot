"""Telegram Bot alert sender.

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Start your bot (send it any message)
  3. Fetch your chat ID:
       curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
     The "id" field in "chat" is your TELEGRAM_CHAT_ID.
  4. Set environment variables:
       TELEGRAM_BOT_TOKEN=123456789:ABCdef...
       TELEGRAM_CHAT_ID=987654321
       (For a group/channel, CHAT_ID is negative, e.g. -1001234567890)

The module no-ops silently when either env var is missing.
"""
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Lazy-loaded — ensures .env is read even if module is imported early
_TOKEN:   str = ""
_CHAT_ID: str = ""
_LOADED:  bool = False


def _ensure_loaded() -> None:
    global _TOKEN, _CHAT_ID, _LOADED
    if _LOADED:
        return
    _LOADED  = True
    _TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    _CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID",   "")

# Regime → emoji
_REGIME_EMOJI = {
    "TREND":    "📈",
    "RANGE":    "↔️",
    "CRASH":    "💥",
    "PUMP":     "🚀",
    "BREAKOUT": "🔔",
}
_DIR_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}


def _enabled() -> bool:
    _ensure_loaded()
    return bool(_TOKEN and _CHAT_ID)


def _send(text: str) -> None:
    """Blocking HTTP POST to Telegram API (called via asyncio.to_thread)."""
    if not _enabled():
        return
    url  = _API_URL.format(token=_TOKEN)
    body = urllib.parse.urlencode({
        "chat_id":    _CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                log.warning("Telegram: non-200 response %d", resp.status)
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)


# ── Message formatters ────────────────────────────────────────────────────────

def _signal_alert(score_dict: dict, order: dict) -> str:
    """Format a signal-fire alert message."""
    symbol    = score_dict.get("symbol", "?")
    regime    = score_dict.get("regime", "?")
    direction = score_dict.get("direction", "?")
    score     = score_dict.get("score", 0.0)
    signals   = score_dict.get("signals", {})

    entry = order.get("entry", 0.0)
    stop  = order.get("stop",  0.0)
    tp    = order.get("take_profit", 0.0)
    qty   = order.get("qty",  0.0)
    paper = order.get("paper", False)

    fired  = [k for k, v in signals.items() if v]
    missed = [k for k, v in signals.items() if not v]

    r_emoji = _REGIME_EMOJI.get(regime, "📊")
    d_emoji = _DIR_EMOJI.get(direction, "⚪")

    rr = ""
    if entry and stop and tp:
        stop_d = abs(entry - stop)
        tp_d   = abs(tp - entry)
        if stop_d > 0:
            rr = f"  |  RR {tp_d / stop_d:.1f}×"

    lines = [
        f"{r_emoji} <b>{regime} {direction}</b> {d_emoji}  {'[PAPER] ' if paper else ''}",
        f"<b>{symbol}</b>   score <b>{score:.0%}</b>{rr}",
        "",
        f"Entry:  <code>{entry:,.4f}</code>",
        f"SL:     <code>{stop:,.4f}</code>",
        f"TP:     <code>{tp:,.4f}</code>",
        f"Qty:    <code>{int(qty) if qty == int(qty) else f'{qty:.4f}'}</code>",
        "",
        f"✅ Fired:  {', '.join(fired) if fired else '—'}",
        f"❌ Missed: {', '.join(missed) if missed else '—'}",
    ]
    return "\n".join(lines)


def _regime_change_alert(symbol: str, old_regime: str, new_regime: str) -> str:
    old_e = _REGIME_EMOJI.get(old_regime, "📊")
    new_e = _REGIME_EMOJI.get(new_regime, "📊")
    return (
        f"🔄 <b>Regime change</b>\n"
        f"{symbol}:  {old_e} {old_regime} → {new_e} <b>{new_regime}</b>"
    )


# ── Public async API (called from executor / main loop) ───────────────────────

import asyncio


async def send_signal_alert(score_dict: dict, order: dict) -> None:
    """Send a Telegram message when a signal fires and an order is placed."""
    if not _enabled():
        return
    text = _signal_alert(score_dict, order)
    await asyncio.to_thread(_send, text)


async def send_regime_change(symbol: str, old_regime: str, new_regime: str) -> None:
    """Send a Telegram message when the regime changes for a symbol."""
    if not _enabled():
        return
    text = _regime_change_alert(symbol, old_regime, new_regime)
    await asyncio.to_thread(_send, text)


async def send_text(message: str) -> None:
    """Send a plain text message (used for system alerts, errors, etc.)."""
    if not _enabled():
        return
    await asyncio.to_thread(_send, message)


def _trade_close_alert(trade: dict, outcome: str, exit_price: float, pnl: float) -> str:
    """Format a trade-close notification."""
    symbol    = trade.get("symbol", "?")
    direction = trade.get("direction", "?")
    entry     = float(trade.get("entry", 0))
    size      = float(trade.get("size", 0))
    regime    = trade.get("regime", "?")

    if outcome == "TP":
        result_emoji = "✅"
        result_label = "Take Profit hit"
    elif outcome == "SL":
        result_emoji = "❌"
        result_label = "Stop Loss hit"
    elif outcome == "CANCELLED":
        result_emoji = "⚠️"
        result_label = "Order cancelled"
    else:
        result_emoji = "🔒"
        result_label = f"Closed ({outcome})"

    pnl_sign = "+" if pnl >= 0 else ""
    r_emoji  = _REGIME_EMOJI.get(regime, "📊")
    d_emoji  = _DIR_EMOJI.get(direction, "⚪")

    lines = [
        f"{result_emoji} <b>Trade Closed</b>  {r_emoji} {regime} {direction} {d_emoji}",
        f"<b>{symbol}</b>   {result_label}",
        "",
        f"Entry:  <code>{entry:,.4f}</code>",
        f"Exit:   <code>{exit_price:,.4f}</code>",
        f"Size:   <code>{int(size) if size == int(size) else f'{size:.4f}'}</code>",
        f"PnL:    <b>{pnl_sign}{pnl:,.2f} USDT</b>",
    ]
    return "\n".join(lines)


async def send_trade_close(
    trade: dict, outcome: str, exit_price: float, pnl: float
) -> None:
    """Send a Telegram message when a trade is closed (TP hit, SL hit, or cancelled)."""
    if not _enabled():
        return
    text = _trade_close_alert(trade, outcome, exit_price, pnl)
    await asyncio.to_thread(_send, text)
