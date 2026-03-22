"""Unit tests for signal functions using a mock cache."""
import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Shared MockCache — no mocking library, just plain attribute overrides
# ---------------------------------------------------------------------------

class MockCache:
    """Minimal cache stub. Override attributes per test case as needed."""

    def __init__(
        self,
        closes: list[float] | None = None,
        cvd: list[float] | None = None,
        ohlcv: list[dict] | None = None,
        range_low: float | None = None,
        range_high: float | None = None,
        vol_ma: float = 0.0,
        funding_rate: float | None = None,
    ) -> None:
        self._closes = closes or []
        self._cvd = cvd or []
        self._ohlcv = ohlcv or []
        self._range_low = range_low
        self._range_high = range_high
        self._vol_ma = vol_ma
        self._funding_rate = funding_rate

    def get_closes(self, _symbol: str, window: int, tf: str = "") -> list[float]:
        return self._closes[-window:]

    def get_cvd(self, _symbol: str, window: int, tf: str = "") -> list[float]:
        return self._cvd[-window:]

    def get_ohlcv(self, _symbol: str, window: int, tf: str = "") -> list[dict]:
        return self._ohlcv[-window:]

    def get_range_low(self, _symbol: str) -> float | None:
        return self._range_low

    def get_range_high(self, _symbol: str) -> float | None:
        return self._range_high

    def get_vol_ma(self, _symbol: str, window: int = 20, tf: str = "") -> float:
        return self._vol_ma

    def get_funding_rate(self, _symbol: str) -> float | None:
        return self._funding_rate

    def get_last_price(self, _symbol: str) -> float:
        return self._closes[-1] if self._closes else 0.0


# Legacy helper kept for the existing stub tests below
def _make_cache(closes=None, ohlcv=None) -> MagicMock:
    cache = MagicMock()
    cache.get_closes.return_value = closes or []
    cache.get_ohlcv.return_value = ohlcv or []
    cache.get_cvd.return_value = []
    cache.get_oi_history.return_value = []
    cache.get_funding_rate.return_value = None
    cache.get_basis_history.return_value = []
    cache.get_skew_history.return_value = []
    cache.get_liq_clusters.return_value = []
    cache.get_agg_trades.return_value = []
    cache.get_range_low.return_value = None
    cache.get_range_high.return_value = None
    cache.get_range_start_timestamp.return_value = None
    cache.get_vol_ma.return_value = 0.0
    cache.get_vol_24h.return_value = None
    cache.get_exchange_inflow.return_value = None
    cache.get_inflow_ma.return_value = 0.0
    cache.get_account_balance.return_value = 0.0
    cache.get_last_price.return_value = 50000.0
    return cache


# ---------------------------------------------------------------------------
# check_cvd_divergence
# ---------------------------------------------------------------------------

class TestCvdDivergence:
    """Tests for signals.trend.cvd.check_cvd_divergence."""

    def test_bullish_divergence_returns_true(self):
        """Prices falling, CVD rising — classic bullish hidden divergence."""
        from signals.trend.cvd import check_cvd_divergence
        # prices[-1] < prices[-3]: price dropped from 100 → 90
        # cvd[-1] > cvd[-3]: CVD rose from -200 → 50  (buyers absorbing)
        cache = MockCache(
            closes=[100.0, 95.0, 90.0],
            cvd=[-200.0, -100.0, 50.0],
        )
        assert check_cvd_divergence("BTCUSDT", cache) is True

    def test_no_divergence_returns_false(self):
        """Both price and CVD falling — no divergence."""
        from signals.trend.cvd import check_cvd_divergence
        cache = MockCache(
            closes=[100.0, 95.0, 90.0],
            cvd=[200.0, 100.0, 50.0],   # CVD also falling
        )
        assert check_cvd_divergence("BTCUSDT", cache) is False

    def test_price_rising_no_divergence(self):
        """Price rising, CVD rising — trend, not divergence."""
        from signals.trend.cvd import check_cvd_divergence
        cache = MockCache(
            closes=[90.0, 95.0, 100.0],  # prices[-1] > prices[-3], condition fails
            cvd=[50.0, 100.0, 200.0],
        )
        assert check_cvd_divergence("BTCUSDT", cache) is False

    def test_insufficient_data_returns_false(self):
        """Fewer than 3 candles — must return False regardless of values."""
        from signals.trend.cvd import check_cvd_divergence
        cache = MockCache(closes=[100.0, 90.0], cvd=[50.0, 60.0])
        assert check_cvd_divergence("BTCUSDT", cache) is False

    def test_empty_cvd_returns_false(self):
        """CVD series empty — no crash, just a clean False."""
        from signals.trend.cvd import check_cvd_divergence
        cache = MockCache(closes=[100.0, 95.0, 90.0], cvd=[])
        assert check_cvd_divergence("BTCUSDT", cache) is False

    def test_empty_prices_returns_false(self):
        from signals.trend.cvd import check_cvd_divergence
        cache = MockCache(closes=[], cvd=[-200.0, -100.0, 50.0])
        assert check_cvd_divergence("BTCUSDT", cache) is False


# ---------------------------------------------------------------------------
# check_absorption_ratio
# ---------------------------------------------------------------------------

def _absorption_candle(
    o: float, h: float, l: float, c: float, v: float
) -> dict:
    return {"o": o, "h": h, "l": l, "c": c, "v": v, "ts": 0}


class TestAbsorptionRatio:
    """Tests for signals.range.absorption.check_absorption_ratio."""

    # Baseline: price at range_low, high volume, small body → True
    _BASE_CANDLE = _absorption_candle(o=99.0, h=100.5, l=98.0, c=99.2, v=300.0)
    # body = |99.2 - 99.0| = 0.2 ;  range = 100.5 - 98.0 = 2.5
    # body/range = 0.08  →  < 0.4 ✓
    # price (99.2) within 1 % of range_low (99.0): (99.2-99.0)/99.0 ≈ 0.002 ✓
    # vol (300) >= vol_ma (200) * 1.5 = 300 ✓

    def _base_cache(self, num_candles: int = 10) -> MockCache:
        candles = [_absorption_candle(100.0, 101.0, 99.0, 100.0, 150.0)] * (num_candles - 1)
        candles.append(self._BASE_CANDLE)
        return MockCache(
            ohlcv=candles,
            range_low=99.0,
            vol_ma=200.0,
        )

    def test_absorption_detected(self):
        """High vol, small body, price at range_low → True."""
        from signals.range.absorption import check_absorption_ratio
        assert check_absorption_ratio("BTCUSDT", self._base_cache()) is True

    def test_price_above_range_low_returns_false(self):
        """Price >1 % above range_low — not at support."""
        from signals.range.absorption import check_absorption_ratio
        candles = [_absorption_candle(100.0, 101.0, 99.0, 100.0, 150.0)] * 9
        # close at 102 with range_low=99 → (102-99)/99 ≈ 3% > 1%
        candles.append(_absorption_candle(101.0, 103.0, 100.0, 102.0, 300.0))
        cache = MockCache(ohlcv=candles, range_low=99.0, vol_ma=200.0)
        assert check_absorption_ratio("BTCUSDT", cache) is False

    def test_low_volume_returns_false(self):
        """Volume below 1.5× MA — not elevated enough to signal absorption."""
        from signals.range.absorption import check_absorption_ratio
        candles = [_absorption_candle(100.0, 101.0, 99.0, 100.0, 150.0)] * 9
        # vol=200, ma=200 → 200 < 200*1.5=300
        candles.append(_absorption_candle(99.0, 100.5, 98.0, 99.2, 200.0))
        cache = MockCache(ohlcv=candles, range_low=99.0, vol_ma=200.0)
        assert check_absorption_ratio("BTCUSDT", cache) is False

    def test_large_body_returns_false(self):
        """Large body relative to range — not absorption, just a trending candle."""
        from signals.range.absorption import check_absorption_ratio
        candles = [_absorption_candle(100.0, 101.0, 99.0, 100.0, 150.0)] * 9
        # body = |99.8 - 98.0| = 1.8 ; range = 100.0 - 98.0 = 2.0 → ratio = 0.9 > 0.4
        candles.append(_absorption_candle(98.0, 100.0, 98.0, 99.8, 300.0))
        cache = MockCache(ohlcv=candles, range_low=99.0, vol_ma=200.0)
        assert check_absorption_ratio("BTCUSDT", cache) is False

    def test_range_low_not_set_returns_false(self):
        """range_low is None — cannot determine support proximity."""
        from signals.range.absorption import check_absorption_ratio
        cache = self._base_cache()
        cache._range_low = None
        assert check_absorption_ratio("BTCUSDT", cache) is False

    def test_insufficient_candles_returns_false(self):
        """Fewer than 5 candles — not enough history."""
        from signals.range.absorption import check_absorption_ratio
        cache = MockCache(
            ohlcv=[self._BASE_CANDLE] * 4,
            range_low=99.0,
            vol_ma=200.0,
        )
        assert check_absorption_ratio("BTCUSDT", cache) is False

    def test_zero_vol_ma_returns_false(self):
        """vol_ma of 0.0 — no baseline to compare against."""
        from signals.range.absorption import check_absorption_ratio
        cache = self._base_cache()
        cache._vol_ma = 0.0
        assert check_absorption_ratio("BTCUSDT", cache) is False


# ---------------------------------------------------------------------------
# Trend signals
# ---------------------------------------------------------------------------

class TestCvdBullish:
    """Tests for signals.trend.cvd.check_cvd_bullish."""

    def test_returns_bool(self):
        from signals.trend.cvd import check_cvd_bullish
        result = check_cvd_bullish("BTCUSDT", _make_cache())
        assert isinstance(result, bool)

    # TODO: def test_positive_cvd_slope_returns_true(self): ...
    # TODO: def test_flat_cvd_returns_false(self): ...
    # TODO: def test_bearish_divergence_returns_false(self): ...


class TestLiqSweep:
    """Tests for signals.trend.liquidity.check_liq_sweep."""

    def test_returns_bool(self):
        from signals.trend.liquidity import check_liq_sweep
        result = check_liq_sweep("BTCUSDT", _make_cache())
        assert isinstance(result, bool)

    # TODO: def test_wick_below_and_close_above_returns_true(self): ...
    # TODO: def test_no_swing_low_returns_false(self): ...


class TestOiFunding:
    """Tests for signals.trend.oi_funding.check_oi_funding."""

    def test_returns_bool(self):
        from signals.trend.oi_funding import check_oi_funding
        result = check_oi_funding("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


class TestHtfStructure:
    """Tests for signals.trend.htf_structure.check_htf_structure."""

    def test_returns_bool(self):
        from signals.trend.htf_structure import check_htf_structure
        result = check_htf_structure("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Range signals
# ---------------------------------------------------------------------------

class TestAbsorption:
    """Tests for signals.range.absorption.check_absorption_ratio."""

    def test_returns_bool(self):
        from signals.range.absorption import check_absorption_ratio
        result = check_absorption_ratio("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


class TestWyckoffSpring:
    """Tests for signals.range.wyckoff_spring.check_wyckoff_spring."""

    def test_returns_bool(self):
        from signals.range.wyckoff_spring import check_wyckoff_spring
        result = check_wyckoff_spring("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


class TestUpthrust:
    """Tests for signals.range.upthrust.check_wyckoff_upthrust."""

    def test_returns_bool(self):
        from signals.range.upthrust import check_wyckoff_upthrust
        result = check_wyckoff_upthrust("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Bear signals
# ---------------------------------------------------------------------------

class TestCvdBearish:
    """Tests for signals.bear.cvd_bearish.check_cvd_bearish."""

    def test_returns_bool(self):
        from signals.bear.cvd_bearish import check_cvd_bearish
        result = check_cvd_bearish("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


class TestFundingExtreme:
    """Tests for signals.bear.funding_extreme.check_funding_extreme_positive."""

    def test_returns_bool(self):
        from signals.bear.funding_extreme import check_funding_extreme_positive
        result = check_funding_extreme_positive("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Crash signals
# ---------------------------------------------------------------------------

class TestDeadCat:
    """Tests for signals.crash.dead_cat.check_dead_cat_setup."""

    def test_returns_bool(self):
        from signals.crash.dead_cat import check_dead_cat_setup
        result = check_dead_cat_setup("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


class TestLiqGrabShort:
    """Tests for signals.crash.liq_grab_short.check_liq_grab_short."""

    def test_returns_bool(self):
        from signals.crash.liq_grab_short import check_liq_grab_short
        result = check_liq_grab_short("BTCUSDT", _make_cache())
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Scorer output shape
# ---------------------------------------------------------------------------

class TestScorerShape:
    """Verify scorer output dicts have all required keys."""

    @pytest.mark.asyncio
    async def test_trend_long_scorer_keys(self):
        # TODO: patch signal functions to control output
        from core.scorer import score
        cache = _make_cache()
        result = await score("BTCUSDT", cache)
        assert set(result.keys()) >= {"symbol", "regime", "direction", "score", "signals", "fire"}
        assert result["regime"] == "TREND"
        assert result["direction"] == "LONG"

    @pytest.mark.asyncio
    async def test_range_scorer_keys(self):
        from core.range_scorer import score
        cache = _make_cache()
        result = await score("BTCUSDT", cache)
        assert set(result.keys()) >= {"symbol", "regime", "direction", "score", "signals", "fire"}

    @pytest.mark.asyncio
    async def test_crash_scorer_keys(self):
        from core.crash_scorer import score
        cache = _make_cache()
        result = await score("BTCUSDT", cache)
        assert result["regime"] == "CRASH"
        assert result["direction"] == "SHORT"
