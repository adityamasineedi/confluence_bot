"""Local OHLCV data store — download once, cache forever.
Stores data as compressed JSON files in backtest/data/
File naming: {SYMBOL}_{TF}_{YYYY-MM}.json.gz
"""
import gzip
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(_DATA_DIR, exist_ok=True)


def _month_key(ts_ms: int) -> str:
    """Convert unix ms timestamp to YYYY-MM string."""
    return datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m")


def _file_path(symbol: str, tf: str, month: str) -> str:
    return os.path.join(_DATA_DIR, f"{symbol}_{tf}_{month}.json.gz")


def save_bars(symbol: str, tf: str, bars: list[dict]) -> int:
    """Save bars to monthly files. Returns number of bars saved."""
    if not bars:
        return 0

    # Group bars by month
    monthly: dict[str, list[dict]] = {}
    for bar in bars:
        month = _month_key(bar["ts"])
        monthly.setdefault(month, []).append(bar)

    saved = 0
    for month, month_bars in monthly.items():
        path = _file_path(symbol, tf, month)
        # Merge with existing if file exists
        existing = []
        if os.path.exists(path):
            existing = load_bars_file(path)

        # Merge and deduplicate by timestamp
        all_bars = {b["ts"]: b for b in existing}
        for b in month_bars:
            all_bars[b["ts"]] = b
        merged = sorted(all_bars.values(), key=lambda x: x["ts"])

        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(merged, f)
        saved += len(month_bars)

    return saved


def load_bars(symbol: str, tf: str,
              from_ms: int, to_ms: int) -> list[dict]:
    """Load bars from local cache for a date range."""
    from_month = _month_key(from_ms)
    to_month   = _month_key(to_ms)

    # Enumerate months in range
    from_dt = datetime.strptime(from_month, "%Y-%m")
    to_dt   = datetime.strptime(to_month,   "%Y-%m")

    bars = []
    current = from_dt
    while current <= to_dt:
        month_str = current.strftime("%Y-%m")
        path = _file_path(symbol, tf, month_str)
        if os.path.exists(path):
            bars.extend(load_bars_file(path))
        # Advance to next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    # Filter to exact range
    return [b for b in bars if from_ms <= b["ts"] <= to_ms]


def load_bars_file(path: str) -> list[dict]:
    """Load bars from a single .json.gz file."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def get_cached_range(symbol: str, tf: str) -> tuple[int, int] | tuple[None, None]:
    """Return (earliest_ts_ms, latest_ts_ms) of cached data, or (None, None)."""
    pattern = f"{symbol}_{tf}_"
    try:
        files = [f for f in os.listdir(_DATA_DIR)
                 if f.startswith(pattern) and f.endswith(".json.gz")]
    except FileNotFoundError:
        return None, None
    if not files:
        return None, None

    earliest = None
    latest   = None
    for fname in files:
        bars = load_bars_file(os.path.join(_DATA_DIR, fname))
        if bars:
            ts_min = bars[0]["ts"]
            ts_max = bars[-1]["ts"]
            if earliest is None or ts_min < earliest:
                earliest = ts_min
            if latest is None or ts_max > latest:
                latest = ts_max
    return earliest, latest


def missing_ranges(symbol: str, tf: str,
                   from_ms: int, to_ms: int) -> list[tuple[int, int]]:
    """Return list of (start_ms, end_ms) gaps that need downloading."""
    cached = load_bars(symbol, tf, from_ms, to_ms)
    if not cached:
        return [(from_ms, to_ms)]

    tf_ms = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
        "1w": 604_800_000,
    }.get(tf, 3_600_000)

    sorted_bars = sorted(cached, key=lambda x: x["ts"])
    gaps = []

    # Check if data starts late
    if sorted_bars[0]["ts"] > from_ms + tf_ms * 10:
        gaps.append((from_ms, sorted_bars[0]["ts"]))

    # Check for internal gaps larger than 2.5x bar interval
    prev_ts = sorted_bars[0]["ts"]
    for bar in sorted_bars[1:]:
        if bar["ts"] - prev_ts > tf_ms * 2.5:
            gaps.append((prev_ts, bar["ts"]))
        prev_ts = bar["ts"]

    # Check if data ends early
    if sorted_bars[-1]["ts"] < to_ms - tf_ms * 10:
        gaps.append((sorted_bars[-1]["ts"], to_ms))

    return gaps


def migrate_legacy_json(symbol: str, tf: str) -> int:
    """Import old-format {SYMBOL}_{TF}.json into compressed monthly cache.
    Returns number of bars migrated, or 0 if no legacy file found.
    """
    legacy_path = os.path.join(_DATA_DIR, f"{symbol}_{tf}.json")
    if not os.path.exists(legacy_path):
        return 0

    # Check if already migrated (gz files exist for this symbol+tf)
    pattern = f"{symbol}_{tf}_"
    try:
        existing_gz = [f for f in os.listdir(_DATA_DIR)
                       if f.startswith(pattern) and f.endswith(".json.gz")]
    except FileNotFoundError:
        existing_gz = []

    if existing_gz:
        return 0  # already migrated

    try:
        with open(legacy_path) as f:
            bars = json.load(f)
    except Exception:
        return 0

    if not bars or not isinstance(bars, list):
        return 0

    # Ensure bars are dicts with "ts" key
    if isinstance(bars[0], list):
        # Raw kline arrays — convert
        bars = [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]),
                 "l": float(b[3]), "c": float(b[4]), "v": float(b[5])}
                for b in bars]

    saved = save_bars(symbol, tf, bars)
    log.info("Migrated %s %s: %d bars from legacy JSON to compressed cache",
             symbol, tf, saved)
    return saved


def cache_info() -> dict:
    """Return summary of what's cached locally."""
    try:
        files = [f for f in os.listdir(_DATA_DIR) if f.endswith(".json.gz")]
    except FileNotFoundError:
        return {"symbols": {}, "total_bars": 0, "total_files": 0,
                "total_size_mb": 0.0}
    by_symbol: dict = {}
    total_bars = 0
    total_size = 0
    for fname in files:
        parts = fname.replace(".json.gz", "").split("_")
        if len(parts) >= 3:
            sym = parts[0]
            tf  = parts[1]
            by_symbol.setdefault(sym, {}).setdefault(tf, 0)
            path = os.path.join(_DATA_DIR, fname)
            total_size += os.path.getsize(path)
            bars = load_bars_file(path)
            by_symbol[sym][tf] += len(bars)
            total_bars += len(bars)
    return {
        "symbols": by_symbol,
        "total_bars": total_bars,
        "total_files": len(files),
        "total_size_mb": round(total_size / 1_048_576, 1),
    }
