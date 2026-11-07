"""
Value-at-Risk models and the tests a bank regulator uses to check them.

VaR at 99 %: the weekly loss that should be exceeded in only 1 week out of 100. Every
forecast uses only returns up to the week before, so the backtest is a fair out-of-sample test.
"""
import numpy as np
import pandas as pd
from arch import arch_model
from scipy import stats

LEVEL = 0.99
HS_WINDOW = 104          # historical simulation: the last two years of weeks
EWMA_LAMBDA = 0.97       # RiskMetrics decay; 0.94 is the daily value, weekly data decays more slowly
GARCH_REFIT = 13         # re-estimate GARCH every quarter, on all data up to then
WARM_UP = 104            # first forecast after two years of data


# ─── Models: each returns a VaR forecast (a positive loss) for every week ────────────

def historical_simulation(returns: pd.Series, window: int = HS_WINDOW, level: float = LEVEL) -> pd.Series:
    """Loss at the (1 - level) quantile of the last `window` weeks."""
    return -returns.rolling(window).quantile(1 - level).shift(1)


def ewma(returns: pd.Series, decay: float = EWMA_LAMBDA, level: float = LEVEL) -> pd.Series:
    """RiskMetrics: exponentially weighted variance, normal distribution, mean assumed zero."""
    variance = np.empty(len(returns))
    variance[0] = returns.iloc[:WARM_UP].var()
    r = returns.to_numpy()
    for t in range(1, len(r)):
        variance[t] = decay * variance[t - 1] + (1 - decay) * r[t - 1] ** 2
    return pd.Series(stats.norm.ppf(level) * np.sqrt(variance), index=returns.index)


def garch(returns: pd.Series, refit: int = GARCH_REFIT, level: float = LEVEL) -> pd.Series:
    """GARCH(1,1) with Student-t errors: volatility clusters and fat tails."""
    scaled = returns * 100                                    # arch fits better on percent returns
    forecasts = pd.Series(np.nan, index=returns.index)
    for start in range(WARM_UP, len(returns), refit):
        fit = arch_model(scaled.iloc[:start], vol="GARCH", p=1, q=1, dist="t", mean="Constant").fit(disp="off")
        mu, omega, alpha, beta, nu = (fit.params[k] for k in ["mu", "omega", "alpha[1]", "beta[1]", "nu"])
        variance = fit.conditional_volatility.iloc[-1] ** 2
        last = scaled.iloc[start - 1]
        quantile = stats.t.ppf(1 - level, nu) * np.sqrt((nu - 2) / nu)
        for t in range(start, min(start + refit, len(returns))):
            variance = omega + alpha * (last - mu) ** 2 + beta * variance      # one step ahead, using week t-1
            forecasts.iloc[t] = -(mu + np.sqrt(variance) * quantile) / 100
            last = scaled.iloc[t]
    return forecasts


def filtered_historical_simulation(returns: pd.Series, decay: float = EWMA_LAMBDA, level: float = LEVEL) -> pd.Series:
    """
    Past returns divided by the EWMA volatility of their week give standardised shocks with the
    real (fat-tailed) shape; the VaR is today's volatility times the quantile of all shocks so far.
    """
    sigma = ewma(returns, decay, level) / stats.norm.ppf(level)
    shocks = returns / sigma
    quantile = shocks.expanding(min_periods=WARM_UP).quantile(1 - level).shift(1)
    return -quantile * sigma


MODELS = {"Historical simulation": historical_simulation, "EWMA (RiskMetrics)": ewma, "GARCH(1,1), Student-t": garch,
          "Filtered historical simulation": filtered_historical_simulation}


# ─── Backtests ───────────────────────────────────────────────────────────────

def exceptions(returns: pd.Series, var: pd.Series) -> pd.Series:
    """True in weeks where the loss was larger than the forecast VaR."""
    valid = var.notna()
    return (returns[valid] < -var[valid]).rename("exception")


def kupiec(hits: pd.Series, level: float = LEVEL) -> dict:
    """Kupiec POF test: is the number of exceptions consistent with the VaR level?"""
    n, x, p = len(hits), int(hits.sum()), 1 - level
    observed = x / n
    log_null = (n - x) * np.log(1 - p) + x * np.log(p)
    log_alt = (n - x) * np.log(1 - observed) + (x * np.log(observed) if x else 0.0)
    lr = -2 * (log_null - log_alt)
    return {"weeks": n, "exceptions": x, "expected": n * p, "rate": observed, "lr": lr, "p_value": 1 - stats.chi2.cdf(lr, 1)}


def christoffersen(hits: pd.Series) -> dict:
    """Independence test: do exceptions cluster, i.e. is an exception more likely right after another one?"""
    h = hits.astype(int).to_numpy()
    pairs = list(zip(h[:-1], h[1:]))
    n00, n01 = pairs.count((0, 0)), pairs.count((0, 1))
    n10, n11 = pairs.count((1, 0)), pairs.count((1, 1))
    pi0, pi1 = n01 / max(n00 + n01, 1), n11 / max(n10 + n11, 1)
    pi = (n01 + n11) / max(len(pairs), 1)

    def ll(p, a, b):
        return (a * np.log(1 - p) if a else 0.0) + (b * np.log(p) if b else 0.0)

    lr = -2 * (ll(pi, n00 + n10, n01 + n11) - ll(pi0, n00, n01) - ll(pi1, n10, n11))
    return {"after_exception": pi1, "after_normal_week": pi0, "lr": lr, "p_value": 1 - stats.chi2.cdf(lr, 1)}


def traffic_light(hits: pd.Series, window: int = 250) -> pd.Series:
    """
    Basel traffic light on every rolling window of 250 observations: green up to 4 exceptions,
    yellow 5-9, red 10 or more. Returns the share of windows in each zone.
    """
    counts = hits.astype(int).rolling(window).sum().dropna()
    zones = pd.cut(counts, [-1, 4, 9, np.inf], labels=["green", "yellow", "red"])
    return zones.value_counts(normalize=True).reindex(["green", "yellow", "red"]).fillna(0)


def expected_shortfall_ratio(returns: pd.Series, var: pd.Series) -> float:
    """Average loss in exception weeks divided by the VaR forecast for those weeks: how far beyond VaR losses go."""
    hits = exceptions(returns, var)
    weeks = hits[hits].index
    return float((-returns[weeks] / var[weeks]).mean()) if len(weeks) else float("nan")


# ─── Portfolio snapshot ──────────────────────────────────────────────────────

def max_drawdown(returns: pd.Series) -> float:
    wealth = (1 + returns).cumprod()
    return float((wealth / wealth.cummax() - 1).min())


def risk_contributions(stock_returns: pd.DataFrame, weeks: int = 104) -> pd.Series:
    """Share of the equal-weight portfolio's variance that comes from each stock (Euler allocation)."""
    cov = stock_returns.tail(weeks).cov().to_numpy()
    w = np.full(cov.shape[0], 1 / cov.shape[0])
    contribution = w * (cov @ w)
    return pd.Series(contribution / contribution.sum(), index=stock_returns.columns)
