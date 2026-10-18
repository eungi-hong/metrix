"""Return decomposition: how much of a day's move was market, sector, company.

A stock that fell 4% on a day its sector fell 4% did not have a 4% piece of
news of its own. This module splits a day's return into parts with a two-step
factor model, estimated on a trailing window that ends the session before the
move (the same no-lookahead rule as the detector):

1.  Sector residual. Regress the sector benchmark on the market,
    ``r_s = a + b * r_m + u``, and keep ``s_perp = r_s - (a + b * r_m)``: the
    part of the sector's day the market does not explain.
2.  Stock model. ``r_i = alpha + beta_m * r_m + beta_s * s_perp + e`` by
    ordinary least squares, with ``sigma_e`` on ``n - 3`` degrees of freedom.

On the day of the move, ``market = beta_m * r_m``, ``sector = beta_s *
s_perp``, ``expected = alpha + market + sector`` and ``abnormal = r_i -
expected``, so ``alpha + market + sector + abnormal`` is the return exactly.
Orthogonalising the sector factor is what makes the split well defined:
without it the market and sector betas share the market's variance and their
split is arbitrary.

The standardised abnormal return uses the forecast-error variance (Patell),
``SAR = abnormal / (sigma_e * sqrt(1 + x' (X'X)^-1 x))`` with ``x = [1, r_m,
s_perp]``, so a day whose market or sector move is far from anything in the
window is not over-weighted. Daily returns are fat-tailed: a SAR is a
heuristic size in residual standard deviations, not a probability.

Earlier shocks in the window would inflate ``sigma_e`` and hide the next one,
so each estimation series is winsorised first at ``z`` robust standard
deviations (median plus or minus ``z * 1.4826 * MAD``). Winsorising every
series, the market included, keeps a crash day from being clipped in the
stock but not in the market, which would invent a residual.

Without a sector benchmark the model is market only (``n - 2`` degrees of
freedom) and the sector component is zero. This module is pure: arrays in,
numbers out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

ModelKind = Literal["market_sector", "market_only"]
Component = Literal["market", "sector", "company"]

MODEL_MARKET_SECTOR: ModelKind = "market_sector"
MODEL_MARKET_ONLY: ModelKind = "market_only"

# Converts a median absolute deviation into a standard deviation for normal data.
MAD_TO_SD = 1.4826


class InsufficientData(ValueError):
    """The window cannot support the model (too short, or degenerate)."""


@dataclass(frozen=True, slots=True)
class FactorModel:
    """A fitted model, with everything needed to apply it to another day."""

    kind: ModelKind
    alpha: float
    beta_market: float
    beta_sector: float  # 0.0 for a market-only model
    # The sector-on-market regression that defines s_perp (0.0 without a sector).
    sector_alpha: float
    sector_beta: float
    sigma_e: float
    r_squared: float
    sessions: int
    # (X'X)^-1 of the estimation design matrix, for the forecast-error variance.
    xtx_inv: tuple[tuple[float, ...], ...]

    def sector_residual(self, market: float, sector: float | None) -> float:
        if self.kind == MODEL_MARKET_ONLY or sector is None:
            return 0.0
        return sector - (self.sector_alpha + self.sector_beta * market)

    def design_row(self, market: float, sector: float | None) -> np.ndarray:
        if self.kind == MODEL_MARKET_ONLY:
            return np.array([1.0, market])
        return np.array([1.0, market, self.sector_residual(market, sector)])

    def abnormal(self, stock: float, market: float, sector: float | None) -> float:
        """The return the model does not explain, with parameters fixed here."""
        expected = (
            self.alpha
            + self.beta_market * market
            + self.beta_sector * self.sector_residual(market, sector)
        )
        return stock - expected


@dataclass(frozen=True, slots=True)
class Decomposition:
    """One day's return in parts. `alpha + market + sector + abnormal` is the return."""

    model: FactorModel
    total: float
    market: float
    sector: float
    expected: float
    abnormal: float
    sar: float | None
    dominant: Component


def winsorize(values: np.ndarray, z: float) -> np.ndarray:
    """Clip to median plus or minus `z` robust standard deviations.

    `z <= 0` disables it, as does a series with no spread (MAD of zero).
    """
    if z <= 0 or values.size == 0:
        return values
    median = float(np.median(values))
    scale = MAD_TO_SD * float(np.median(np.abs(values - median)))
    if scale == 0:
        return values
    return np.clip(values, median - z * scale, median + z * scale)


def _ols(design: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Coefficients, residuals and (X'X)^-1. Raises InsufficientData if singular."""
    xtx = design.T @ design
    if np.linalg.matrix_rank(xtx) < xtx.shape[0]:
        raise InsufficientData("the estimation window is degenerate (a constant series)")
    xtx_inv = np.linalg.inv(xtx)
    coefficients = xtx_inv @ (design.T @ target)
    return coefficients, target - design @ coefficients, xtx_inv


def fit(
    stock: np.ndarray,
    market: np.ndarray,
    sector: np.ndarray | None,
    *,
    winsor_z: float,
    min_sessions: int,
) -> FactorModel:
    """Fit the model on an estimation window of aligned daily returns."""
    n = int(stock.size)
    if n != market.size or (sector is not None and sector.size != n):
        raise ValueError("estimation series must be aligned and the same length")
    if n < min_sessions:
        raise InsufficientData(f"{n} sessions of history before this move; {min_sessions} needed")

    r_i = winsorize(np.asarray(stock, dtype=float), winsor_z)
    r_m = winsorize(np.asarray(market, dtype=float), winsor_z)
    ones = np.ones(n)

    if sector is None:
        kind: ModelKind = MODEL_MARKET_ONLY
        sector_alpha = sector_beta = 0.0
        design = np.column_stack([ones, r_m])
    else:
        kind = MODEL_MARKET_SECTOR
        r_s = winsorize(np.asarray(sector, dtype=float), winsor_z)
        (sector_alpha, sector_beta), s_perp, _ = _ols(np.column_stack([ones, r_m]), r_s)
        design = np.column_stack([ones, r_m, s_perp])

    coefficients, residuals, xtx_inv = _ols(design, r_i)
    dof = n - design.shape[1]
    if dof <= 0:
        raise InsufficientData("not enough sessions for the model's parameters")
    sse = float(residuals @ residuals)
    sst = float(((r_i - r_i.mean()) ** 2).sum())
    return FactorModel(
        kind=kind,
        alpha=float(coefficients[0]),
        beta_market=float(coefficients[1]),
        beta_sector=float(coefficients[2]) if kind == MODEL_MARKET_SECTOR else 0.0,
        sector_alpha=float(sector_alpha),
        sector_beta=float(sector_beta),
        sigma_e=float(np.sqrt(sse / dof)),
        r_squared=float(1 - sse / sst) if sst > 0 else 0.0,
        sessions=n,
        xtx_inv=tuple(tuple(float(v) for v in row) for row in xtx_inv),
    )


def decompose(model: FactorModel, stock: float, market: float, sector: float | None) -> Decomposition:
    """Split one day's return with a model fitted on the window before it."""
    market_part = model.beta_market * market
    sector_part = model.beta_sector * model.sector_residual(market, sector)
    expected = model.alpha + market_part + sector_part
    abnormal = stock - expected

    sar: float | None = None
    if model.sigma_e > 0:
        x = model.design_row(market, sector)
        leverage = float(x @ np.array(model.xtx_inv) @ x)
        sar = abnormal / (model.sigma_e * float(np.sqrt(1 + leverage)))

    parts: dict[Component, float] = {
        "market": abs(market_part),
        "sector": abs(sector_part),
        "company": abs(abnormal),
    }
    return Decomposition(
        model=model,
        total=stock,
        market=market_part,
        sector=sector_part,
        expected=expected,
        abnormal=abnormal,
        sar=sar,
        dominant=max(parts, key=lambda name: parts[name]),
    )
