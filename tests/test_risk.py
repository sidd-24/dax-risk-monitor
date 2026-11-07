import json

import numpy as np
import pandas as pd
import pytest

import data
import risk


@pytest.fixture(scope="module")
def returns():
    rng = np.random.default_rng(0)
    values = rng.standard_t(4, 400) * 0.02
    return pd.Series(values, index=pd.date_range("2010-01-08", periods=400, freq="W-FRI"))


@pytest.mark.parametrize("name", list(risk.MODELS))
def test_forecasts_never_use_the_week_they_forecast(returns, name):
    model = risk.MODELS[name]
    changed = returns.copy()
    changed.iloc[300:] *= 5                                # a crash from week 300 on
    before, after = model(returns), model(changed)
    pd.testing.assert_series_equal(before.iloc[:301], after.iloc[:301])   # week 300's forecast is unaffected
    assert before.iloc[risk.WARM_UP:].notna().all() and (before.iloc[risk.WARM_UP:] > 0).all()


def test_historical_simulation_is_the_empirical_quantile(returns):
    var = risk.historical_simulation(returns, window=100)
    assert var.iloc[150] == pytest.approx(-returns.iloc[50:150].quantile(0.01))


def test_ewma_recursion(returns):
    var = risk.ewma(returns, decay=0.9)
    sigma = var / 2.3263478740
    assert sigma.iloc[11] ** 2 == pytest.approx(0.9 * sigma.iloc[10] ** 2 + 0.1 * returns.iloc[10] ** 2, rel=1e-6)


def test_garch_and_fhs_react_to_a_volatility_shock(returns):
    calm = returns.copy()
    shocked = returns.copy()
    shocked.iloc[290:300] = -0.15
    for model in [risk.garch, risk.filtered_historical_simulation, risk.ewma]:
        assert model(shocked).iloc[300] > 1.5 * model(calm).iloc[300]


def test_kupiec_known_values():
    hits = pd.Series([True] * 10 + [False] * 990)              # exactly 1 %
    result = risk.kupiec(hits)
    assert result["lr"] == pytest.approx(0, abs=1e-9) and result["p_value"] == pytest.approx(1)
    too_many = risk.kupiec(pd.Series([True] * 25 + [False] * 975))
    assert too_many["p_value"] < 0.001
    none = risk.kupiec(pd.Series([False] * 1000))
    assert none["exceptions"] == 0 and none["p_value"] < 0.001       # zero exceptions is also suspicious at 1,000 weeks


def test_christoffersen_detects_clustering():
    spread = pd.Series(([True] + [False] * 99) * 10)
    clustered = pd.Series([False] * 450 + [True] * 10 + [False] * 540)
    assert risk.christoffersen(spread)["p_value"] > 0.05
    assert risk.christoffersen(clustered)["p_value"] < 0.001


def test_traffic_light_zones():
    hits = pd.Series([False] * 250 + [True] * 12)              # windows end with 0..12 exceptions
    zones = risk.traffic_light(hits)
    assert zones.sum() == pytest.approx(1)
    assert zones["green"] == pytest.approx(5 / 13) and zones["yellow"] == pytest.approx(5 / 13) and zones["red"] == pytest.approx(3 / 13)


def test_max_drawdown_and_risk_contributions():
    assert risk.max_drawdown(pd.Series([0.1, -0.5, 0.2])) == pytest.approx(-0.5)
    rng = np.random.default_rng(1)
    stocks = pd.DataFrame({"calm": rng.normal(0, 0.01, 200), "wild": rng.normal(0, 0.05, 200)})
    shares = risk.risk_contributions(stocks)
    assert shares.sum() == pytest.approx(1) and shares["wild"] > 0.9


def test_parse_weekly_uses_adjusted_close():
    payload = {"Weekly Adjusted Time Series": {
        "2026-10-30": {"4. close": "100.0", "5. adjusted close": "98.0"},
        "2026-11-06": {"4. close": "110.0", "5. adjusted close": "110.0"}}}
    series = data.parse_weekly(json.loads(json.dumps(payload)))
    assert series.tolist() == [98.0, 110.0] and series.index.is_monotonic_increasing


def test_ecb_rate_and_weekly_risk_free(tmp_path, monkeypatch):
    (tmp_path / "ecb_rate.csv").write_text(
        "header;line\n"
        "BBK:BBIN1(1.0);M;D0;ECB;ECBMIN;EUR;ME;2026-09;2.60;P1M\n"
        "BBK:BBIN1(1.0);M;D0;ECB;ECBMIN;EUR;ME;2026-10;2.65;P1M\n", encoding="utf-8")
    monkeypatch.setattr(data, "RAW", tmp_path)
    assert data.ecb_rate().tolist() == [2.60, 2.65]
    weeks = pd.DatetimeIndex(["2026-09-25", "2026-10-30", "2026-11-06"])
    assert data.weekly_risk_free(weeks).tolist() == pytest.approx([0.026 / 52, 0.0265 / 52, 0.0265 / 52])


def test_readme_snapshot_is_filled_and_replaced(tmp_path):
    import report
    readme = tmp_path / "README.md"
    readme.write_text("# T\n<!-- SNAPSHOT:START -->\n<!-- SNAPSHOT:END -->\n", encoding="utf-8")
    report.update_readme("first", readme)
    report.update_readme("second", readme)
    assert readme.read_text(encoding="utf-8") == "# T\n<!-- SNAPSHOT:START -->\nsecond\n<!-- SNAPSHOT:END -->\n"
