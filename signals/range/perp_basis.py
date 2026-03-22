"""Perp basis signal — spot/perp spread confirming range sentiment for LONG."""

# Basis = (perp_price - spot_price) / spot_price * 100  (annualised %)
# Neutral band for range-long: basis between -0.05 % and +0.10 %
_BASIS_LOW  = -0.0005   # -0.05 % of spot
_BASIS_HIGH =  0.0010   # +0.10 % of spot


def check_perp_basis(symbol: str, cache) -> bool:
    """True when the perp/spot basis is in the neutral-to-slightly-positive band.

    A basis within [-0.05 %, +0.10 %] indicates no extreme carry pressure — the
    market is not overheated (positive basis) or panicking (deeply negative basis),
    consistent with range-bound conditions suitable for a long entry.

    Requires at least 2 basis readings in the cache.
    """
    basis_series = cache.get_basis_history(symbol, n=2)
    if not basis_series:
        return False

    basis = basis_series[-1]
    return _BASIS_LOW <= basis <= _BASIS_HIGH
