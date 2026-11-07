"""
Backtests the four VaR models and writes the risk report: charts in docs/, numbers in
results/summary.json, and the current snapshot in the README (between the SNAPSHOT markers).

    python report.py
"""
import json
import re
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

import data
import risk

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLORS = {"Historical simulation": "#9ca3af", "EWMA (RiskMetrics)": "#f59e0b", "GARCH(1,1), Student-t": "#8b5cf6",
          "Filtered historical simulation": "#2563eb"}


def backtest(returns: pd.Series) -> tuple[pd.DataFrame, dict]:
    test = returns.index[risk.WARM_UP:]
    forecasts, rows = {}, []
    for name, model in risk.MODELS.items():
        var = model(returns)[test]
        hits = risk.exceptions(returns[test], var)
        k, c, light = risk.kupiec(hits), risk.christoffersen(hits), risk.traffic_light(hits)
        forecasts[name] = var
        rows.append({"model": name, "exceptions": k["exceptions"], "expected": round(k["expected"], 1),
                     "kupiec_p": k["p_value"], "clustering_p": c["p_value"], "green_share": light["green"],
                     "yellow_share": light["yellow"], "red_share": light["red"], "average_var": float(var.mean()),
                     "loss_beyond_var": risk.expected_shortfall_ratio(returns[test], var)})
    return pd.DataFrame(rows), forecasts


def plot_backtest(returns: pd.Series, forecasts: dict, path):
    test = forecasts["Filtered historical simulation"].index
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(test, returns[test] * 100, width=6, color="#d1d5db", label="Weekly portfolio return")
    for name in ["EWMA (RiskMetrics)", "Filtered historical simulation"]:
        ax.plot(test, -forecasts[name] * 100, color=COLORS[name], linewidth=1, label=f"99 % VaR: {name}")
    hits = risk.exceptions(returns[test], forecasts["Filtered historical simulation"])
    ax.scatter(hits[hits].index, returns[hits[hits].index] * 100, color="#dc2626", s=14, zorder=3,
               label="Loss beyond the filtered HS VaR")
    ax.set_ylabel("% per week")
    ax.legend(frameon=False, fontsize=8, loc="lower left", ncols=2)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title("Equal-weight portfolio of 15 DAX stocks: weekly returns and VaR forecasts", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_exceptions(returns: pd.Series, forecasts: dict, path):
    years = range(forecasts["EWMA (RiskMetrics)"].index.year.min(), forecasts["EWMA (RiskMetrics)"].index.year.max() + 1)
    fig, ax = plt.subplots(figsize=(10, 3))
    width = 0.2
    for i, (name, var) in enumerate(forecasts.items()):
        hits = risk.exceptions(returns[var.index], var)
        per_year = hits.groupby(hits.index.year).sum().reindex(years, fill_value=0)
        ax.bar(np.array(list(years)) + (i - 1.5) * width, per_year, width, color=COLORS[name], label=name)
    ax.axhline(0.52, color="#111827", linestyle=":", linewidth=1, label="Expected per year (1 % of 52 weeks)")
    ax.set_ylabel("Exceptions")
    ax.set_xticks(list(years)[::2], [str(y) for y in list(years)[::2]])
    ax.legend(frameon=False, fontsize=7, ncols=3)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def snapshot(stock_returns: pd.DataFrame, portfolio: pd.Series, forecasts_now: dict) -> pd.DataFrame:
    rf = data.weekly_risk_free(stock_returns.index)
    recent = stock_returns.tail(104)
    beta = recent.apply(lambda s: np.cov(s, portfolio.tail(104))[0, 1] / portfolio.tail(104).var())
    excess = stock_returns.sub(rf, axis=0)
    table = pd.DataFrame({
        "Company": [data.STOCKS[s][0] for s in stock_returns.columns],
        "Sector": [data.STOCKS[s][1] for s in stock_returns.columns],
        "Volatility (52 w, p.a.)": stock_returns.tail(52).std() * np.sqrt(52),
        "Beta (2 y)": beta,
        "Share of portfolio risk": risk.risk_contributions(stock_returns),
        "Sharpe ratio (since 2005)": excess.mean() / stock_returns.std() * np.sqrt(52),
        "Max drawdown (since 2005)": stock_returns.apply(risk.max_drawdown),
    }, index=stock_returns.columns)
    return table.sort_values("Share of portfolio risk", ascending=False)


def snapshot_markdown(table: pd.DataFrame, portfolio: pd.Series, var_now: dict) -> str:
    week = portfolio.index.max().date()
    lines = [f"*Week ending {week}. Updated automatically every week.*", "",
             "| Next week's 99 % VaR | Loss on a EUR 1m portfolio |", "|---|---:|"]
    lines += [f"| {name} | EUR {value * 1e6:,.0f} ({value:.1%}) |" for name, value in var_now.items()]
    lines += ["", "| Stock | Sector | Volatility | Beta | Share of risk | Sharpe | Max drawdown |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for row in table.itertuples():
        lines.append(f"| {row.Company} | {row.Sector} | {row[3]:.0%} | {row[4]:.2f} | {row[5]:.1%} | {row[6]:.2f} | {row[7]:.0%} |")
    return "\n".join(lines)


def next_week_var(returns: pd.Series) -> dict:
    """The forecast for the coming week: models applied to the history plus one placeholder week."""
    extended = pd.concat([returns, pd.Series([0.0], index=[returns.index.max() + pd.Timedelta(days=7)])])
    return {name: float(model(extended).iloc[-1]) for name, model in risk.MODELS.items()}


def update_readme(section: str, path="README.md"):
    readme = Path(path)
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        # also matches an empty pair of markers, as in a fresh README
        text = re.sub(r"(<!-- SNAPSHOT:START -->).*?(<!-- SNAPSHOT:END -->)", lambda m: f"{m.group(1)}\n{section}\n{m.group(2)}",
                      text, flags=re.DOTALL)
        readme.write_text(text, encoding="utf-8")


def main():
    stock_returns = data.returns()
    portfolio = data.portfolio_returns(stock_returns)
    results, forecasts = backtest(portfolio)
    Path("docs").mkdir(exist_ok=True)
    Path("results").mkdir(exist_ok=True)
    plot_backtest(portfolio, forecasts, "docs/backtest.png")
    plot_exceptions(portfolio, forecasts, "docs/exceptions_by_year.png")
    var_now = next_week_var(portfolio)
    table = snapshot(stock_returns, portfolio, var_now)
    rf = data.weekly_risk_free(portfolio.index)
    summary = {
        "weeks": len(portfolio), "first_week": str(portfolio.index.min().date()), "last_week": str(portfolio.index.max().date()),
        "backtest_from": str(forecasts["EWMA (RiskMetrics)"].index.min().date()),
        "portfolio": {"return_pa": float((1 + portfolio).prod() ** (52 / len(portfolio)) - 1),
                      "volatility_pa": float(portfolio.std() * np.sqrt(52)),
                      "sharpe": float((portfolio - rf).mean() / portfolio.std() * np.sqrt(52)),
                      "max_drawdown": risk.max_drawdown(portfolio), "worst_week": str(portfolio.idxmin().date()),
                      "worst_week_return": float(portfolio.min())},
        "backtest": results.to_dict("records"), "var_next_week": var_now,
        "snapshot": table.reset_index(names="symbol").to_dict("records"),
    }
    Path("results/summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    update_readme(snapshot_markdown(table, portfolio, var_now))
    print(results.round(3).to_string(index=False))
    print(json.dumps(summary["portfolio"], indent=2), json.dumps(var_now, indent=2))
    print(table.round(3).to_string())


if __name__ == "__main__":
    main()
