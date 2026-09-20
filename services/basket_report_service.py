"""PDF rendering for a basket backtest result.

Layout via reportlab; the equity-curve chart is a Plotly figure rasterized
through utils.plotly_render (Kaleido, eventlet-safe). Consumes exactly the
payload shape services.basket_service.run_basket_backtest() returns -- no
separate re-computation, so the PDF always matches the JSON response for the
same request.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from utils.logging import get_logger
from utils.plotly_render import render_plotly_png

logger = get_logger(__name__)

_METRIC_LABELS = {
    "cagr": "CAGR",
    "volatility": "Volatility",
    "sharpe": "Sharpe Ratio",
    "sortino": "Sortino Ratio",
    "calmar": "Calmar Ratio",
    "max_drawdown": "Max Drawdown",
    "win_rate": "Win Rate",
    "best_day": "Best Day",
    "worst_day": "Worst Day",
    "value_at_risk": "Value at Risk (95%)",
    "cvar": "Conditional VaR",
    "ulcer_index": "Ulcer Index",
    "recovery_factor": "Recovery Factor",
    "tail_ratio": "Tail Ratio",
    "skew": "Skew",
    "kurtosis": "Kurtosis",
    "alpha": "Alpha",
    "beta": "Beta",
    "information_ratio": "Information Ratio",
    "benchmark_cagr": "Benchmark CAGR",
    "excess_cagr": "Excess CAGR",
}

_PCT_METRICS = {
    "cagr",
    "volatility",
    "max_drawdown",
    "win_rate",
    "best_day",
    "worst_day",
    "value_at_risk",
    "cvar",
    "benchmark_cagr",
    "excess_cagr",
}

_PAGE_WIDTH = A4[0] - 3.6 * cm  # minus left+right margins


def _fmt_metric(key: str, value: Any) -> str:
    if value is None:
        return "-"
    if key in _PCT_METRICS:
        return f"{value * 100:.2f}%"
    return f"{value:.3f}"


def _styled_table(rows: list[list[str]], *, col_widths: list[float], font_size: int = 9) -> Table:
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), font_size),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return t


def _equity_curve_png(
    equity: list[dict], benchmark_equity: list[dict], benchmark_name: str | None
) -> bytes:
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[row["date"] for row in equity],
            y=[row["value"] for row in equity],
            mode="lines",
            name="Portfolio",
            line={"color": "#2563eb", "width": 2},
        )
    )
    if benchmark_equity:
        fig.add_trace(
            go.Scatter(
                x=[row["date"] for row in benchmark_equity],
                y=[row["value"] for row in benchmark_equity],
                mode="lines",
                name=benchmark_name or "Benchmark",
                line={"color": "#9ca3af", "width": 2, "dash": "dot"},
            )
        )
    fig.update_layout(
        template="plotly_white",
        width=900,
        height=420,
        margin={"l": 50, "r": 30, "t": 30, "b": 40},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},
        yaxis_title="Portfolio Value",
    )
    return render_plotly_png(fig)


def render_backtest_pdf(payload: dict[str, Any]) -> bytes:
    """Build a PDF report from a run_basket_backtest() success payload.

    Assumes payload is the same shape returned to the JSON caller (status,
    meta, basket, versions, equity, benchmark_equity, metrics, correlation,
    diversification, health, ...). Sections whose source data is empty are
    skipped rather than rendered blank.
    """
    basket = payload["basket"]
    meta = payload["meta"]
    metrics = payload.get("metrics") or {}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.8 * cm,
        rightMargin=1.8 * cm,
        topMargin=1.8 * cm,
        bottomMargin=1.8 * cm,
        title=f"{basket['name']} - Backtest Report",
    )
    styles = getSampleStyleSheet()
    h1 = styles["Title"]
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], spaceBefore=14, spaceAfter=6)
    body = styles["BodyText"]
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, textColor=colors.grey)

    story: list = []

    # --- Header ---
    story.append(Paragraph(f"{_esc(basket['name'])} - Backtest Report", h1))
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    header_lines = [
        f"Period: {_esc(str(meta['start']))} to {_esc(str(meta['end']))}",
        f"Benchmark: {_esc(basket.get('benchmark') or 'None')}",
        f"Initial Capital: {basket['initial_capital']:,.2f}",
        f"Generated: {generated}",
    ]
    story.append(Paragraph("<br/>".join(header_lines), small))
    story.append(Spacer(1, 0.4 * cm))

    if meta.get("data_warnings"):
        warn_text = _esc("; ".join(str(w) for w in meta["data_warnings"]))
        story.append(Paragraph(f"<b>Data warnings:</b> {warn_text}", small))
        story.append(Spacer(1, 0.3 * cm))

    # --- Summary metrics ---
    story.append(Paragraph("Performance Summary", h2))
    summary_keys = ["cagr", "volatility", "sharpe", "sortino", "calmar", "max_drawdown", "win_rate"]
    if "alpha" in metrics:
        summary_keys += ["alpha", "beta", "information_ratio", "excess_cagr"]
    rows = [["Metric", "Value"]]
    for key in summary_keys:
        if key in metrics:
            rows.append([_METRIC_LABELS.get(key, key), _fmt_metric(key, metrics[key])])
    if len(rows) > 1:
        story.append(_styled_table(rows, col_widths=[8 * cm, 5 * cm]))

    # --- Equity curve chart ---
    equity = payload.get("equity") or []
    if len(equity) > 1:
        story.append(Paragraph("Equity Curve", h2))
        try:
            png = _equity_curve_png(
                equity, payload.get("benchmark_equity") or [], basket.get("benchmark")
            )
            story.append(Image(io.BytesIO(png), width=_PAGE_WIDTH, height=_PAGE_WIDTH * 420 / 900))
        except Exception:
            logger.exception("basket PDF: equity curve chart render failed")
            story.append(Paragraph("(chart unavailable)", small))

    # --- Risk metrics ---
    risk_keys = [
        "best_day",
        "worst_day",
        "value_at_risk",
        "cvar",
        "ulcer_index",
        "recovery_factor",
        "tail_ratio",
        "skew",
        "kurtosis",
    ]
    rows = [["Metric", "Value"]]
    for key in risk_keys:
        if key in metrics:
            rows.append([_METRIC_LABELS.get(key, key), _fmt_metric(key, metrics[key])])
    if len(rows) > 1:
        story.append(Paragraph("Risk Metrics", h2))
        story.append(_styled_table(rows, col_widths=[8 * cm, 5 * cm]))

    # --- Current holdings (latest version) ---
    versions = payload.get("versions") or []
    if versions:
        latest = versions[-1]
        story.append(
            Paragraph(
                f"Current Holdings (v{latest['version_number']}, "
                f"as of {_esc(str(latest['effective_date']))})",
                h2,
            )
        )
        rows = [["Symbol", "Exchange", "Weight"]]
        for h in latest["holdings"]:
            rows.append([h["symbol"], h.get("exchange", ""), f"{h['weight'] * 100:.2f}%"])
        story.append(_styled_table(rows, col_widths=[6 * cm, 4 * cm, 4 * cm]))

    # --- Rebalance history ---
    if len(versions) > 1:
        story.append(Paragraph("Rebalance History", h2))
        rows = [["Version", "Date", "Value at Rebalance", "Cost"]]
        for v in versions:
            value = v.get("value_at_rebalance")
            cost = v.get("cost_at_rebalance")
            rows.append(
                [
                    f"v{v['version_number']}",
                    v["effective_date"],
                    f"{value:,.2f}" if value is not None else "-",
                    f"{cost:,.2f}" if cost is not None else "-",
                ]
            )
        story.append(_styled_table(rows, col_widths=[2.5 * cm, 3.5 * cm, 5 * cm, 3 * cm]))

    # --- Correlation matrix ---
    corr = payload.get("correlation") or {}
    symbols = corr.get("symbols") or []
    matrix = corr.get("matrix") or []
    if symbols and matrix:
        story.append(Paragraph("Correlation Matrix", h2))
        rows = [[""] + symbols]
        for sym, row_vals in zip(symbols, matrix, strict=True):
            rows.append([sym] + [f"{v:.2f}" if v is not None else "-" for v in row_vals])
        col_width = min(2.2 * cm, _PAGE_WIDTH / max(len(symbols) + 1, 1))
        story.append(
            _styled_table(
                rows, col_widths=[2.5 * cm] + [col_width] * len(symbols), font_size=7
            )
        )
        avg = corr.get("average_pairwise")
        if avg is not None:
            story.append(Paragraph(f"Average pairwise correlation: {avg:.3f}", small))

    # --- Diversification ---
    div = payload.get("diversification") or {}
    if div:
        story.append(Paragraph("Diversification", h2))
        rows = [["Metric", "Value"]]
        for key, value in div.items():
            label = "HHI" if key == "hhi" else key.replace("_", " ").title()
            value_str = f"{value:.3f}" if isinstance(value, int | float) and value is not None else "-"
            rows.append([label, value_str])
        story.append(_styled_table(rows, col_widths=[8 * cm, 5 * cm]))

    # --- Portfolio health ---
    health = payload.get("health") or {}
    if health.get("score") is not None:
        story.append(Paragraph("Portfolio Health", h2))
        story.append(
            Paragraph(f"Overall Score: {health['score']} ({_esc(str(health.get('grade', '-')))})", body)
        )
        rows = [["Pillar", "Score", "Weight", "Comment"]]
        for p in health.get("pillars", []):
            rows.append(
                [
                    p.get("label", p.get("key", "")),
                    f"{p['score']:.1f}" if p.get("score") is not None else "-",
                    f"{p['weight']:.2f}" if p.get("weight") is not None else "-",
                    p.get("comment", ""),
                ]
            )
        story.append(_styled_table(rows, col_widths=[4 * cm, 2 * cm, 2 * cm, 8.5 * cm], font_size=8))

    doc.build(story)
    return buf.getvalue()
