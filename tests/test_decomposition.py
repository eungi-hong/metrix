"""The return decomposition, against data with known parameters.

The series are synthetic draws from the model itself (tests/finance_helpers
constants), so the fitted parameters have a right answer to be checked
against, and every identity the README claims is asserted here.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.services.decomposition import (
    MODEL_MARKET_ONLY,
    MODEL_MARKET_SECTOR,
    InsufficientData,
    decompose,
    fit,
    winsorize,
)
from tests import finance_helpers as fh


def draws(n: int, seed: int = 11) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(stock, market, sector) returns from the model, `n` sessions."""
    rng = np.random.default_rng(seed)
    r_m = rng.normal(fh.MARKET_DRIFT, fh.MARKET_VOL, n)
    u = rng.normal(0, fh.SECTOR_OWN_VOL, n)
    e = rng.normal(0, fh.STOCK_OWN_VOL, n)
    r_s = fh.SECTOR_ALPHA + fh.SECTOR_BETA * r_m + u
    r_i = fh.ALPHA + fh.BETA_M * r_m + fh.BETA_S * u + e
    return r_i, r_m, r_s


def test_the_fit_recovers_the_parameters_it_was_drawn_with():
    stock, market, sector = draws(5000)
    model = fit(stock, market, sector, winsor_z=0, min_sessions=60)

    assert model.kind == MODEL_MARKET_SECTOR
    assert model.beta_market == pytest.approx(fh.BETA_M, abs=0.03)
    assert model.beta_sector == pytest.approx(fh.BETA_S, abs=0.04)
    assert model.sector_beta == pytest.approx(fh.SECTOR_BETA, abs=0.02)
    assert model.sigma_e == pytest.approx(fh.STOCK_OWN_VOL, rel=0.03)


def test_the_parts_add_up_to_the_return_exactly():
    stock, market, sector = draws(120)
    model = fit(stock, market, sector, winsor_z=5, min_sessions=60)

    for r_i, r_m, r_s in ((-0.087, -0.031, -0.044), (0.052, 0.002, 0.0), (0.0, 0.04, -0.02)):
        d = decompose(model, r_i, r_m, r_s)
        assert model.alpha + d.market + d.sector + d.abnormal == pytest.approx(r_i, abs=1e-15)
        assert d.expected == pytest.approx(model.alpha + d.market + d.sector, abs=1e-15)


def test_sigma_e_uses_n_minus_three_degrees_of_freedom():
    stock, market, sector = draws(90)
    model = fit(stock, market, sector, winsor_z=0, min_sessions=60)

    # Recompute by hand: sector residual, then the stock regression.
    ones = np.ones(90)
    (a, b), *_ = np.linalg.lstsq(np.column_stack([ones, market]), sector, rcond=None)
    s_perp = sector - (a + b * market)
    design = np.column_stack([ones, market, s_perp])
    coefficients, *_ = np.linalg.lstsq(design, stock, rcond=None)
    residuals = stock - design @ coefficients

    assert model.sigma_e == pytest.approx(math.sqrt(residuals @ residuals / (90 - 3)), rel=1e-10)
    assert model.beta_market == pytest.approx(coefficients[1], rel=1e-10)
    assert model.sessions == 90


def test_the_sector_residual_is_uncorrelated_with_the_market_in_the_window():
    stock, market, sector = draws(200)
    model = fit(stock, market, sector, winsor_z=0, min_sessions=60)
    s_perp = np.array([model.sector_residual(m, s) for m, s in zip(market, sector)])

    assert abs(np.corrcoef(market, s_perp)[0, 1]) < 1e-10
    assert abs(s_perp.mean()) < 1e-12


def test_sar_uses_the_forecast_error_variance():
    stock, market, sector = draws(120)
    model = fit(stock, market, sector, winsor_z=0, min_sessions=60)
    r_i, r_m, r_s = -0.06, -0.035, -0.04
    d = decompose(model, r_i, r_m, r_s)

    x = model.design_row(r_m, r_s)
    expected = d.abnormal / (model.sigma_e * math.sqrt(1 + x @ np.array(model.xtx_inv) @ x))
    assert d.sar == pytest.approx(expected, rel=1e-12)
    # A day far outside the window's market moves is down-weighted.
    assert abs(d.sar) < abs(d.abnormal / model.sigma_e)


def test_without_a_sector_the_model_is_market_only():
    stock, market, _ = draws(120)
    model = fit(stock, market, None, winsor_z=0, min_sessions=60)
    d = decompose(model, -0.05, -0.02, None)

    assert model.kind == MODEL_MARKET_ONLY
    assert model.beta_sector == 0.0 and d.sector == 0.0
    assert len(model.xtx_inv) == 2  # [1, r_m]
    ones = np.ones(120)
    coefficients, *_ = np.linalg.lstsq(np.column_stack([ones, market]), stock, rcond=None)
    residuals = stock - np.column_stack([ones, market]) @ coefficients
    assert model.sigma_e == pytest.approx(math.sqrt(residuals @ residuals / (120 - 2)), rel=1e-10)


def test_the_dominant_component_is_the_largest_part():
    stock, market, sector = draws(120)
    model = fit(stock, market, sector, winsor_z=0, min_sessions=60)

    assert decompose(model, -0.05, -0.04, -0.042).dominant == "market"
    assert decompose(model, -0.04, 0.0, -0.045).dominant == "sector"
    assert decompose(model, 0.09, 0.001, 0.001).dominant == "company"


def test_winsorising_keeps_an_earlier_shock_from_inflating_sigma_e():
    stock, market, sector = draws(120, seed=3)
    shocked = stock.copy()
    shocked[40] += 0.40  # an earlier +40% company day inside the window

    raw = fit(shocked, market, sector, winsor_z=0, min_sessions=60)
    clipped = fit(shocked, market, sector, winsor_z=5, min_sessions=60)
    clean = fit(stock, market, sector, winsor_z=0, min_sessions=60)

    assert raw.sigma_e > 2 * clean.sigma_e  # the shock dominates the residual variance
    assert clipped.sigma_e < raw.sigma_e
    assert abs(clipped.sigma_e - clean.sigma_e) < abs(raw.sigma_e - clean.sigma_e)
    # And so the next real move is not hidden: its SAR is larger.
    day = (-0.07, -0.002, 0.001)
    assert abs(decompose(clipped, *day).sar) > abs(decompose(raw, *day).sar)


def test_winsorize_clips_at_robust_standard_deviations():
    values = np.array([0.01, -0.01, 0.0, 0.02, -0.02, 0.5])
    clipped = winsorize(values, 3)
    median = np.median(values)
    scale = 1.4826 * np.median(np.abs(values - median))

    assert clipped[-1] == pytest.approx(median + 3 * scale)
    assert np.array_equal(clipped[:-1], values[:-1])
    assert np.array_equal(winsorize(values, 0), values)  # 0 turns it off
    assert np.array_equal(winsorize(np.zeros(5), 5), np.zeros(5))  # no spread


def test_too_little_history_or_a_degenerate_window_is_refused():
    stock, market, sector = draws(59)
    with pytest.raises(InsufficientData, match="59 sessions"):
        fit(stock, market, sector, winsor_z=5, min_sessions=60)

    stock, market, sector = draws(100)
    with pytest.raises(InsufficientData, match="degenerate"):
        fit(stock, np.zeros(100), sector, winsor_z=0, min_sessions=60)
