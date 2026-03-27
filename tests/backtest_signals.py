"""Offline backtester — replay historical OHLCV through all signal functions."""
import asyncio
import json
import os
import sys
from typing import Any


class BacktestCache:
    """Minimal cache implementation backed by static historical data for backtesting."""

    def __init__(self, ohlcv_data: dict[str, list[dict]]) -> None:
        """
        Args:
            ohlcv_data: dict keyed by "symbol:tf" -> list of OHLCV dicts sorted oldest->newest
        """
        self._data = ohlcv_data

    def get_closes(self, symbol: str, window: int, tf: str) -> list[float]:
        """Return last `window` close prices from the static dataset."""
        key = f"{symbol}:{tf}"
        candles = self._data.get(key, [])
        return [c["c"] for c in candles[-window:]]

    def get_ohlcv(self, symbol: str, window: int, tf: str) -> list[dict]:
        """Return last `window` OHLCV dicts from the static dataset."""
        key = f"{symbol}:{tf}"
        return self._data.get(key, [])[-window:]

    def get_oi(self, symbol: str, window: int) -> list[float]:
        return []

    def get_funding(self, symbol: str, window: int = 1) -> list[float]:
        return []

    def get_skew(self, symbol: str) -> float:
        return 0.0

    def get_skew_series(self, symbol: str, window: int) -> list[float]:
        return []

    def get_last_price(self, symbol: str) -> float:
        key = f"{symbol}:1m"
        candles = self._data.get(key, [])
        return candles[-1]["c"] if candles else 0.0

    def get_vol_ma(self, symbol: str, window: int, tf: str) -> float:
        key = f"{symbol}:{tf}"
        candles = self._data.get(key, [])[-window:]
        if not candles:
            return 0.0
        return sum(c["v"] for c in candles) / len(candles)

    def get_vwap(self, symbol: str, window: int, tf: str) -> float:
        key = f"{symbol}:{tf}"
        candles = self._data.get(key, [])[-window:]
        if not candles:
            return 0.0
        total_vol = sum(c["v"] for c in candles)
        if total_vol == 0:
            return 0.0
        return sum(c["c"] * c["v"] for c in candles) / total_vol

    def get_account_balance(self) -> float:
        return 10000.0


def load_ohlcv_from_file(path: str) -> dict[str, list[dict]]:
    """Load OHLCV data from a JSON file.

    Expected format:
    {
        "BTCUSDT:1h": [{"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100.0, "ts": 1700000000}, ...]
    }

    TODO: also support CSV format
    """
    with open(path) as f:
        return json.load(f)


async def run_backtest(
    symbol: str,
    ohlcv_path: str,
    start_idx: int = 100,
) -> list[dict]:
    """Replay historical data and collect all signal firings.

    Args:
        symbol: trading pair to backtest
        ohlcv_path: path to JSON file with historical OHLCV data
        start_idx: first candle index to start evaluating (need lookback)

    Returns:
        list of score dicts where fire=True

    TODO: implement sliding window replay
    TODO: for each candle, slice data up to that point and evaluate all scorers
    TODO: track PnL using subsequent candles for exit simulation
    """
    data = load_ohlcv_from_file(ohlcv_path)
    firings: list[dict] = []

    # TODO: iterate over candles from start_idx
    # TODO: for each step, create a BacktestCache with data[:step]
    # TODO: detect regime, route direction, collect scores
    # TODO: append fire=True results to firings

    return firings


def print_backtest_summary(firings: list[dict]) -> None:
    """Print a summary of backtest results.

    TODO: compute win rate, average R, max drawdown
    TODO: break down by regime and direction
    """
    print(f"Total signals fired: {len(firings)}")
    for f in firings:
        print(f"  {f.get('symbol')} | {f.get('regime')} {f.get('direction')} | score={f.get('score'):.2f}")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    path = sys.argv[2] if len(sys.argv) > 2 else "data/btc_1h.json"
    firings = asyncio.run(run_backtest(symbol, path))
    print_backtest_summary(firings)
