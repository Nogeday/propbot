"""Analysis screen of the panel (ADR-0009 item 4, SPEC §6.8): report artifacts as cards.

`render_analysis` reads `reports/` with `latest_artifacts` (newest artifact per kind and
label) and returns the inner HTML of the "Analiz & Simülasyon" page; `app` wraps it in the
common layout. Report files are git-ignored and can be edited by hand, so:

- every dynamic text goes through `esc`; long labels are wrapped by CSS, numbers beyond
  1e9 are written in scientific form, and charts guard every coordinate (`charts`);
- an unreadable file is listed with its error text and never breaks the page; a card that
  fails to render becomes an error card; the number of cards and table rows is bounded;
- honesty: a synthetic artifact carries the synthetic-data badge on its
  card, a cost-free run a red cost-free banner, every card says when it was made
  and from which file; a status is shown as passed only where the artifact itself says so.
"""

import datetime as dt
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from propbot.backtest.artifacts import (
    COMPARISON_RUN_TAG,
    COST_FREE_TAG,
    GATE_IDS,
    SYNTHETIC_TAG,
    AnyArtifact,
    ArtifactEnvelope,
    ArtifactProblem,
    BacktestArtifact,
    CostMatrixArtifact,
    DataInfo,
    DataValidationArtifact,
    GoNoGoArtifact,
    LoadedArtifact,
    MonteCarloArtifact,
    ParityArtifact,
    StatsModel,
    TableRowModel,
    WalkForwardArtifact,
    latest_artifacts,
)
from propbot.backtest.config import MAX_P_BREACH, MIN_OOS_TO_IS_RATIO, MIN_P_PASS_ALL_PHASES
from propbot.backtest.report import tr_int
from propbot.dashboard import charts
from propbot.dashboard.charts import Group, HBar, Marker, Threshold, compact_number, finite
from propbot.notify.format import ISTANBUL

MAX_CARDS: Final = 30
MAX_PROBLEMS: Final = 50
MAX_NOTES: Final = 8
MAX_TABLE_ROWS: Final = 36
MAX_FOLDS: Final = 40
MAX_COST_STRATEGIES: Final = 12
MAX_PARAMS: Final = 24
MAX_REPORT_KEYS: Final = 20
MAX_MISMATCHES_SHOWN: Final = 5
JSON_CHARS: Final = 200

SYNTHETIC_BADGE: Final = f"{SYNTHETIC_TAG} — kanıt değildir"
COST_FREE_BANNER: Final = (
    f"{COST_FREE_TAG} — maliyetler sıfır; yalnızca karşılaştırma içindir, kanıt değildir "
    "(SPEC §6.3)."
)

KIND_TITLES: Final = {
    "go_no_go": "Go/No-Go",
    "monte_carlo": "Monte Carlo prop simülasyonu",
    "backtest": "Backtest",
    "cost_matrix": "Maliyet matrisi",
    "walk_forward": "Walk-forward",
    "parity": "Parite",
    "data_validation": "Veri doğrulama",
}
# The order of the cards: the decision first, then its evidence.
KIND_ORDER: Final = tuple(KIND_TITLES)

# The command that writes each report into reports/ (shown in the empty state).
REPORT_COMMANDS: Final = {
    "monte_carlo": "uv run propbot montecarlo --trades <işlemler.csv> "
    "--venue config/venues/ftmo_2step_100k.yaml --r-is-net",
    "backtest": "uv run propbot backtest --strategy <strateji|all> --data data/<dosya>.csv "
    "--trades-out <işlemler.csv>",
    "cost_matrix": "uv run propbot backtest --strategy <strateji|all> --data data/<dosya>.csv "
    "--matrix",
    "walk_forward": "uv run propbot backtest --strategy <strateji|all> --data data/<dosya>.csv "
    "--walk-forward",
    "go_no_go": "uv run propbot gonogo --strategies <strateji,strateji>",
    "data_validation": "uv run propbot data check --data data/<dosya>.csv",
    "parity": "uv run propbot parity --strategy <strateji> --data data/<dosya>.csv "
    "--tv-trades <tradingview.csv> --tv-tz America/New_York",
}

_GATE_STATUS: Final = {
    "PASS": ("GEÇTİ", "ok"),
    "FAIL": ("KALDI", "bad"),
    "NOT_EVALUABLE": ("DEĞERLENDİRİLEMEDİ", "warn"),
}
_REASONS: Final = {"daily_loss": "günlük kayıp", "max_loss": "toplam kayıp"}
_SAMPLES: Final = {"all": "tüm örneklem", "oos": "yalnızca OOS"}
# The fill model alone never makes a decision run: a cost-free or scaled-cost run is
# pessimistic as well. Whether it is one is the separate "Koşu türü" row.
_FILL_MODELS: Final = {
    "pessimistic": "kötümser",
    "tv_ohlc": "TradingView emülasyonu (yalnızca parite)",
}
DECISION_RUN: Final = "karar koşusu"
COMPARISON_RUN: Final = COMPARISON_RUN_TAG
UNKNOWN_RUN: Final = "bilinmiyor"
_EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def esc(value: object) -> str:
    return charts.esc(value)


def badge(text: str, tone: str) -> str:
    return f'<span class="badge {esc(tone)}">{esc(text)}</span>'


@dataclass(frozen=True, slots=True)
class Raw:
    """Markup already built (and escaped) by this module, for a table cell."""

    html: str


Cell = str | Raw


# --- formatting --------------------------------------------------------------------------


def num(value: float | None, digits: int = 2) -> str:
    return compact_number(value, digits)


def r_text(value: float | None, digits: int = 3) -> str:
    number = finite(value)
    if number is None:
        return "—"
    sign = "+" if number >= 0.0 else ""
    return f"{sign}{compact_number(number, digits)}R"


def pct(fraction: float | None, digits: int = 1) -> str:
    number = finite(fraction)
    return "—" if number is None else "%" + compact_number(100.0 * number, digits)


def level(percent: float | None, digits: int = 2) -> str:
    """A value that is already a percentage: 7.5 -> %7,50."""
    number = finite(percent)
    return "—" if number is None else "%" + compact_number(number, digits)


def count(value: int) -> str:
    return tr_int(value) if abs(value) < 10**15 else compact_number(float(value), 0)


def _utc(text: str) -> dt.datetime | None:
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)


def local_time(text: str) -> str:
    moment = _utc(text)
    if moment is None:
        return text
    return moment.astimezone(ISTANBUL).strftime("%d.%m.%Y %H:%M") + " (TR)"


def day_text(ts_ms: float) -> str:
    try:
        moment = _EPOCH + dt.timedelta(milliseconds=ts_ms)
    except (OverflowError, ValueError):
        return "—"
    return moment.astimezone(ISTANBUL).strftime("%d.%m.%Y")


def stamp_ms(text: str) -> float | None:
    moment = _utc(text)
    return None if moment is None else moment.timestamp() * 1000.0


def compact_json(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    return charts.clip(text, JSON_CHARS)


# --- HTML building blocks ----------------------------------------------------------------


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Cell]],
    *,
    empty: str = "Gösterilecek satır yok.",
    caption: str = "",
) -> str:
    if not rows:
        return f'<p class="muted">{esc(empty)}</p>'
    head = "".join(f'<th scope="col">{esc(h)}</th>' for h in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{cell.html}</td>'
            if isinstance(cell, Raw)
            else f"<td>{esc(cell)}</td>"
            for cell in row
        )
        + "</tr>"
        for row in rows
    )
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return (
        f'<div class="table-wrap"><table class="an-table">{cap}<thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def figure(svg: str, caption: str) -> str:
    caption_html = f'<figcaption class="muted">{esc(caption)}</figcaption>'
    return f'<figure class="an-chart">{svg}{caption_html}</figure>'


def kv_list(items: Sequence[tuple[str, str]]) -> str:
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in items)
    return f'<dl class="an-kv">{rows}</dl>'


def _data_text(data: DataInfo | None) -> str:
    if data is None:
        return "veri dosyası: raporda kayıtlı değil"
    span = ""
    if data.first_utc and data.last_utc:
        span = f", {local_time(data.first_utc)} – {local_time(data.last_utc)}"
    return (
        f"veri: {data.file_name} ({data.symbol}, {count(data.tf_seconds)} sn bar, "
        f"{count(data.bars)} bar{span}, bar saat dilimi {data.bar_tz})"
    )


def card(title: str, loaded: LoadedArtifact, body: str) -> str:
    artifact = loaded.artifact
    flags: list[str] = []
    if artifact.synthetic:
        flags.append(badge(SYNTHETIC_BADGE, "bad"))
    banner = ""
    if any(COST_FREE_TAG in note for note in artifact.notes) or (
        isinstance(artifact, BacktestArtifact) and artifact.costs.cost_free
    ):
        banner = f'<p class="banner bad" role="note">{esc(COST_FREE_BANNER)}</p>'
    provenance = (
        f'<p class="source">Üretildi: {esc(local_time(artifact.created_utc))} · rapor dosyası: '
        f"{esc(loaded.file_name)} · {esc(_data_text(artifact.data))}</p>"
    )
    return (
        f'<section class="card an-card" aria-label="{esc(title)}">'
        f'<h2>{esc(title)} <span class="muted">· {esc(artifact.label)}</span></h2>'
        + (f'<p class="an-flags">{"".join(flags)}</p>' if flags else "")
        + banner
        + provenance
        + body
        + notes_html(artifact)
        + "</section>"
    )


def notes_html(artifact: ArtifactEnvelope) -> str:
    if not artifact.notes:
        return ""
    shown = artifact.notes[:MAX_NOTES]
    items = "".join(f"<li>{esc(note)}</li>" for note in shown)
    more = len(artifact.notes) - len(shown)
    extra = f'<p class="muted">+{more} not daha (rapor dosyasında).</p>' if more > 0 else ""
    return f'<div class="an-notes"><h3>Notlar</h3><ul>{items}</ul>{extra}</div>'


# --- Go/No-Go ----------------------------------------------------------------------------


def go_no_go_body(artifact: GoNoGoArtifact) -> str:
    # GO is shown only when the artifact itself says GO (the schema allows that only on
    # real data with all seven gates PASS); everything else is NO-GO.
    verdict = badge("GO", "ok") if artifact.overall == "GO" else badge("NO-GO", "bad")
    by_id = {gate.id: gate for gate in artifact.gates}
    rows: list[list[Cell]] = []
    for gate_id in GATE_IDS:
        gate = by_id.get(gate_id)
        if gate is None:
            rows.append([gate_id, "—", Raw(badge("RAPORDA YOK", "warn")), "Bu kapı raporda yok."])
            continue
        text, tone = _GATE_STATUS.get(gate.status, ("DEĞERLENDİRİLEMEDİ", "warn"))
        rows.append([gate.id, gate.title, Raw(badge(text, tone)), gate.evidence])
    reasons = "".join(f"<li>{esc(reason)}</li>" for reason in artifact.reasons[:MAX_TABLE_ROWS])
    reasons_html = f"<h3>Gerekçeler</h3><ul>{reasons}</ul>" if reasons else ""
    return (
        f'<p class="an-verdict">Genel karar: {verdict}</p>'
        + table(("Kapı", "Başlık", "Durum", "Kanıt"), rows)
        + reasons_html
    )


# --- Monte Carlo -------------------------------------------------------------------------


def _phase_label(index: int, phases: int) -> str:
    if phases == 1:
        return "Faz 1'i geçme (tek fazlı program)"
    if index == 0:
        return "Faz 1'i geçme"
    if index == phases - 1:
        return "İki fazı geçme" if phases == 2 else f"Tüm fazları geçme ({phases} faz)"
    return f"Faz 1–{index + 1}'i geçme"


def monte_carlo_body(artifact: MonteCarloArtifact) -> str:
    inputs = artifact.inputs
    rules = inputs.rules
    phases = len(artifact.p_pass_phase)
    bars: list[HBar] = []
    for index, probability in enumerate(artifact.p_pass_phase):
        threshold = None
        if index == phases - 1:
            threshold = Threshold(
                MIN_P_PASS_ALL_PHASES, f"G4 eşiği ≥ {pct(MIN_P_PASS_ALL_PHASES, 0)}"
            )
        bars.append(HBar(_phase_label(index, phases), probability, pct(probability), threshold))
    bars.append(
        HBar(
            "İhlal",
            artifact.p_breach,
            pct(artifact.p_breach),
            Threshold(MAX_P_BREACH, f"G4 eşiği ≤ {pct(MAX_P_BREACH, 0)}"),
        )
    )
    bars.append(HBar("Sonuçlanmayan (ufuk doldu)", artifact.p_undecided, pct(artifact.p_undecided)))
    probability_chart = figure(
        charts.hbar_chart(
            bars,
            title="Monte Carlo olasılıkları",
            axis_label="olasılık (yolların payı)",
            tick_format=lambda v: pct(v, 0),
        ),
        "Çizgiler SPEC §9 G4 tabanıdır (config/backtest.yaml daha sıkı olabilir); "
        "G4 kararını yalnızca Go/No-Go raporu verir.",
    )
    breach_rows: list[list[Cell]] = [
        [f"Faz {share.phase}", _REASONS.get(share.reason, share.reason), pct(share.probability)]
        for share in artifact.breach_shares[:MAX_TABLE_ROWS]
    ]
    breach = table(("Faz", "Neden", "Olasılık"), breach_rows, empty="İhlal eden yol yok.")
    duration_rows: list[list[Cell]] = []
    for index, stats in enumerate(artifact.time_to_pass[:MAX_TABLE_ROWS]):
        label = "Faz 1" if index == 0 else f"Faz 1–{index + 1}"
        if stats is None:
            duration_rows.append([label, "0", "geçen yol yok", "", "", "", "", ""])
            continue
        duration_rows.append(
            [
                label,
                count(stats.n),
                num(stats.median, 0),
                num(stats.p25, 0),
                num(stats.p75, 0),
                num(stats.p90, 0),
                num(stats.p95, 0),
                num(stats.mean, 1),
            ]
        )
    durations = table(
        ("Geçen", "Yol", "Medyan", "%25", "%75", "%90", "%95", "Ortalama"),
        duration_rows,
        caption="Geçme süresi (piyasa günü; işlemsiz günler dahil)",
    )
    dd = artifact.drawdown
    histogram = artifact.drawdown_histogram
    markers = [
        Marker(dd.median, f"medyan {level(dd.median)}"),
        Marker(dd.q95, f"%95 dilim {level(dd.q95)}"),
        Marker(dd.q99, f"%99 dilim {level(dd.q99)}"),
        Marker(rules.max_loss_pct, f"firma toplam kayıp sınırı {level(rules.max_loss_pct)}", True),
    ]
    dd_chart = figure(
        charts.histogram_chart(
            histogram.edges,
            histogram.counts,
            title="Maksimum düşüş dağılımı",
            x_label=(
                f"maks. düşüş (başlangıç bakiyesinin %'si; kural durdurması olmadan, "
                f"{count(inputs.dd_days)} piyasa günü)"
            ),
            y_label="yol sayısı",
            markers=markers,
            fmt=lambda v: level(v, 1),
        ),
        f"Beklenen kuyruk kaybı (ES): en kötü %5 → {level(dd.es95)} · en kötü %1 → "
        f"{level(dd.es99)}.",
    )
    targets = " + ".join(level(t) for t in rules.targets_pct)
    min_days = count(rules.min_trading_days) + (
        " (varsayım)" if rules.min_trading_days_assumed else ""
    )
    input_items = [
        ("İşlem dosyası", inputs.trades_file),
        ("İşlem sayısı", count(inputs.n_trades)),
        ("Risk / işlem", level(inputs.risk_pct)),
        ("Maliyet", f"{num(inputs.cost_r, 3)} R / işlem"),
        ("Yol sayısı", count(inputs.n_paths)),
        ("Ufuk", f"{count(inputs.max_days)} piyasa günü"),
        ("Tohum", str(inputs.seed)),
        ("İşlem atlama olasılığı", pct(inputs.skip_prob)),
        ("Venue", inputs.venue),
        ("Hedef(ler)", targets),
        ("Günlük kayıp sınırı", level(rules.daily_loss_limit_pct)),
        ("Toplam kayıp sınırı", level(rules.max_loss_pct)),
        ("Asgari işlem günü", min_days),
    ]
    steps = ""
    if inputs.experimental_risk_steps:
        steps = (
            '<p class="banner warn" role="note">Deneysel risk basamaklarıyla koşulmuş: G4 için '
            "sabit seçilen riskle yeniden koşulmalı.</p>"
        )
    return (
        probability_chart
        + '<div class="an-split"><div><h3>İhlal dağılımı (faz × neden)</h3>'
        + breach
        + "</div><div>"
        + durations
        + "</div></div>"
        + dd_chart
        + "<h3>Girdiler</h3>"
        + steps
        + kv_list(input_items)
    )


# --- backtest ----------------------------------------------------------------------------

_STAT_ROWS: Final[tuple[tuple[str, Callable[[StatsModel], str]], ...]] = (
    ("İşlem", lambda s: count(s.n)),
    (
        "Kazanan / kaybeden / başa baş",
        lambda s: f"{count(s.wins)} / {count(s.losses)} / {count(s.breakeven)}",
    ),
    ("Kazanma oranı", lambda s: pct(s.win_rate)),
    ("Ortalama R (beklenti)", lambda s: r_text(s.mean_r)),
    ("Medyan R", lambda s: r_text(s.median_r)),
    ("Std. sapma (R)", lambda s: num(s.stdev_r, 3)),
    ("t-istatistiği", lambda s: num(s.t_stat, 2)),
    ("Profit factor", lambda s: num(s.profit_factor, 2)),
    ("Toplam R", lambda s: r_text(s.total_r, 2)),
    ("Toplam K/Z", lambda s: num(s.total_pnl, 0)),
    ("Ort. kazanç (R)", lambda s: r_text(s.avg_win_r)),
    ("Ort. kayıp (R)", lambda s: r_text(s.avg_loss_r)),
    ("Maks. düşüş (R)", lambda s: num(s.max_drawdown_r, 2)),
    ("Maks. düşüş (%)", lambda s: level(s.max_drawdown_pct)),
    ("En uzun kayıp serisi", lambda s: count(s.longest_losing_streak)),
    ("Tamamlanmamış işlem", lambda s: count(s.incomplete)),
)


def stats_table(columns: Sequence[tuple[str, StatsModel]]) -> str:
    rows: list[list[Cell]] = [
        [name, *(Raw(esc(render(stats))) for _, stats in columns)] for name, render in _STAT_ROWS
    ]
    return table(("Ölçüt", *(title for title, _ in columns)), rows)


def time_table(title: str, rows: Sequence[TableRowModel]) -> str:
    shown = list(rows)
    note = ""
    if len(shown) > MAX_TABLE_ROWS:
        note = f'<p class="muted">Yalnızca son {MAX_TABLE_ROWS} satır gösteriliyor.</p>'
        shown = shown[-MAX_TABLE_ROWS:]
    extent = max((abs(v) for row in shown if (v := finite(row.total_r)) is not None), default=0.0)
    body: list[list[Cell]] = [
        [
            row.label,
            Raw(esc(count(row.trades))),
            Raw(esc(pct(row.win_rate, 0))),
            Raw(esc(r_text(row.mean_r, 2))),
            Raw(charts.cell_bar(row.total_r, extent) + " " + esc(r_text(row.total_r, 1))),
        ]
        for row in shown
    ]
    return (
        f'<div class="an-mini"><h3>{esc(title)}</h3>'
        + table(("", "İşlem", "Kazanma", "Ort. R", "Toplam R"), body, empty="İşlem yok.")
        + note
        + "</div>"
    )


def run_kind(artifact: BacktestArtifact) -> str:
    """What the run was when it was made, from the artifact's own fields; the page grades no gate.

    The fields show three conditions of a decision run: the pessimistic fill model, real
    costs and the configured costs unscaled (factor exactly 1.0). Two more they do not
    show, because the page reads no config file: the OOS start and the per-trade risk the
    run was asked for (`-oos…` and `-pinerisk` runs). For those the runner's own note
    speaks: it writes one, naming every reason, on each run it does not make as a decision
    run, and the row repeats it. Any definite miss makes a comparison run; with none, a
    factor the file does not record (an older file) leaves it unknown, never a decision
    run (fail closed, like the Go/No-Go gates).

    "Decision run" here is the runner's statement at run time. The Go/No-Go report judges
    again against today's config (a risk.yaml or OOS start changed since turns an old
    decision run into a comparison run there).
    """
    stated = next((note for note in artifact.notes if note.startswith(COMPARISON_RUN)), None)
    if stated is not None:
        return stated
    reasons: list[str] = []
    if artifact.fill_model != "pessimistic":
        reasons.append("dolum modeli kötümser değil")
    if artifact.costs.cost_free:
        reasons.append("maliyetsiz koşu")
    factor = artifact.cost_factor
    if factor is not None and factor != 1.0:
        reasons.append(f"maliyet çarpanı {_factor_text(factor)} (1× değil)")
    if reasons:
        if factor is None:
            reasons.append("maliyet çarpanı kayıtlı değil")
        return f"{COMPARISON_RUN}: {'; '.join(reasons)}"
    if factor is None:
        return f"{UNKNOWN_RUN} (maliyet çarpanı rapor dosyasında kayıtlı değil)"
    return DECISION_RUN


def backtest_body(artifact: BacktestArtifact) -> str:
    costs = artifact.costs
    instrument = artifact.instrument
    params = list(artifact.params.items())
    param_text = ", ".join(f"{k}={v}" for k, v in params[:MAX_PARAMS]) or "—"
    if len(params) > MAX_PARAMS:
        param_text += f" … (+{len(params) - MAX_PARAMS})"
    windows = (
        f", {len(costs.spread_windows)} saatlik spread penceresi" if costs.spread_windows else ""
    )
    items = [
        ("Strateji", artifact.strategy_id),
        (
            "Enstrüman",
            f"{instrument.symbol} ({instrument.kind}, tick {num(instrument.tick_size, 4)}, "
            f"puan değeri {num(instrument.point_value, 2)})",
        ),
        ("Dolum modeli", _FILL_MODELS.get(artifact.fill_model, artifact.fill_model)),
        ("Koşu türü", run_kind(artifact)),
        (
            "Maliyetler",
            f"komisyon {num(costs.commission_per_unit_side, 2)} / birim / taraf, "
            f"slipaj {num(costs.slippage_ticks, 2)} tick, spread {num(costs.spread_points, 2)} puan"
            f"{windows}",
        ),
        ("Başlangıç sermayesi", num(artifact.initial_capital, 0)),
        ("OOS başlangıcı", local_time(artifact.oos_start_utc)),
        ("Parametreler", param_text),
        (
            "Tamamlanmamış / düşürülen giriş",
            f"{count(artifact.incomplete_entries)} / {count(artifact.dropped_entries)}",
        ),
    ]
    ratio = finite(artifact.g3_ratio)
    g3 = (
        f"OOS/IS beklenti oranı (G3): <strong>{esc(num(ratio, 2))}</strong> "
        f'<span class="muted">(SPEC §9 tabanı ≥ {esc(num(MIN_OOS_TO_IS_RATIO, 2))}; '
        "kararı Go/No-Go raporu verir)</span>"
        if ratio is not None
        else "OOS/IS beklenti oranı (G3): <strong>hesaplanamadı</strong>"
    )
    if artifact.g3_note:
        g3 += f' <span class="muted">— {esc(artifact.g3_note)}</span>'
    points = [(float(p.exit_ts_ms), p.cum_r) for p in artifact.equity_curve]
    oos_ms = stamp_ms(artifact.oos_start_utc)
    marker = None if oos_ms is None else Marker(oos_ms, "OOS başlangıcı")
    x_texts = (day_text(points[0][0]), day_text(points[-1][0])) if points else ("", "")
    curve = figure(
        charts.line_chart(
            points,
            title=f"{artifact.strategy_id}: kümülatif R",
            x_label="işlem çıkış tarihi (TR)",
            y_label="kümülatif R",
            x_texts=x_texts,
            marker=marker,
            fmt=lambda v: compact_number(v, 1),
        ),
        "Özsermaye eğrisi (R); kesik çizgi IS/OOS sınırıdır.",
    )
    tables = artifact.tables
    return (
        kv_list(items)
        + stats_table(
            (
                ("Tümü", artifact.stats_all),
                ("IS", artifact.stats_is),
                ("OOS", artifact.stats_oos),
            )
        )
        + f"<p>{g3}</p>"
        + curve
        + '<div class="an-tables">'
        + time_table("Hafta günü", tables.weekday)
        + time_table("Saat (NY)", tables.hour)
        + time_table("Ay", tables.month)
        + "</div>"
    )


# --- cost matrix -------------------------------------------------------------------------


def _factor_text(factor: float) -> str:
    # "1,50" -> "1,5"; "0,00" would strip to nothing, so zero keeps its digit.
    return (compact_number(factor, 2).rstrip("0").rstrip(",") or "0") + "×"


def cost_matrix_body(artifact: CostMatrixArtifact) -> str:
    rows = list(artifact.rows[:MAX_COST_STRATEGIES])
    more = len(artifact.rows) - len(rows)
    means = [v for row in rows for cell in row.cells if (v := finite(cell.mean_r)) is not None]
    y_range = (min([0.0, *means]), max([0.0, *means]))
    multiples = "".join(
        figure(
            charts.grouped_bar_chart(
                [
                    Group(_factor_text(cell.factor), (cell.mean_r,), (r_text(cell.mean_r, 3),))
                    for cell in row.cells
                ],
                title=f"{row.strategy_id}: maliyet çarpanına göre ortalama R",
                series=("ortalama R",),
                y_label="ortalama R",
                y_range=y_range,
                fmt=lambda v: compact_number(v, 2),
                width=320.0,
                height=200.0,
            ),
            row.strategy_id,
        )
        for row in rows
    )
    body: list[list[Cell]] = []
    for row in rows:
        for cell in row.cells:
            mean = finite(cell.mean_r)
            below = mean is not None and mean < 0.0
            flag = " " + badge("sıfırın altında", "bad") if below else ""
            body.append(
                [
                    row.strategy_id,
                    Raw(esc(_factor_text(cell.factor))),
                    Raw(esc(count(cell.n))),
                    Raw(esc(r_text(cell.mean_r)) + flag),
                    Raw(esc(num(cell.t_stat, 2))),
                    Raw(esc(r_text(cell.total_r, 2))),
                    Raw(esc(num(cell.profit_factor, 2))),
                    Raw(esc(num(cell.max_drawdown_r, 2))),
                ]
            )
    note = f'<p class="muted">+{more} strateji daha (rapor dosyasında).</p>' if more > 0 else ""
    return (
        f"<p>Örneklem: {esc(_SAMPLES.get(artifact.sample, artifact.sample))} · çarpanlar: "
        f"{esc(', '.join(_factor_text(f) for f in artifact.factors))} (her maliyet kalemi "
        "çarpanla çarpılır)</p>"
        + f'<div class="an-multiples">{multiples}</div>'
        + table(
            ("Strateji", "Çarpan", "İşlem", "Ort. R", "t", "Toplam R", "PF", "Maks. DD (R)"),
            body,
            empty="Raporda strateji yok.",
        )
        + note
    )


# --- walk-forward ------------------------------------------------------------------------


def walk_forward_body(artifact: WalkForwardArtifact) -> str:
    config = artifact.config
    summary = artifact.summary
    folds = list(artifact.folds[-MAX_FOLDS:])
    more = len(artifact.folds) - len(folds)
    chart = figure(
        charts.grouped_bar_chart(
            [
                Group(
                    f"K{fold.index + 1}",
                    (fold.is_stats.mean_r, fold.oos_stats.mean_r),
                    (r_text(fold.is_stats.mean_r, 2), r_text(fold.oos_stats.mean_r, 2)),
                )
                for fold in folds
            ],
            title=f"{artifact.strategy_id}: kat başına IS ve OOS beklentisi",
            series=("IS ortalama R", "OOS ortalama R"),
            y_label="ortalama R",
            fmt=lambda v: compact_number(v, 2),
        ),
        "Her kat için IS (sol) ve OOS (sağ) işlem başına ortalama R.",
    )
    fold_rows: list[list[Cell]] = [
        [
            f"K{fold.index + 1}",
            f"{fold.is_first_day} – {fold.is_last_day}",
            f"{fold.oos_first_day} – {fold.oos_last_day}",
            Raw(esc(f"{count(fold.is_stats.n)} · {r_text(fold.is_stats.mean_r)}")),
            Raw(esc(f"{count(fold.oos_stats.n)} · {r_text(fold.oos_stats.mean_r)}")),
            Raw(
                esc(num(fold.ratio, 2))
                + (f' <span class="muted">{esc(fold.ratio_note)}</span>' if fold.ratio_note else "")
            ),
        ]
        for fold in folds
    ]
    pooled = summary.pooled_oos
    efficiency = num(summary.efficiency, 2)
    if summary.efficiency_note:
        efficiency += f" ({summary.efficiency_note})"
    items = [
        (
            "Pencereler",
            f"IS {count(config.is_days)} gün · OOS {count(config.oos_days)} gün · "
            f"adım {count(config.step_days)} gün",
        ),
        ("Kapsam", f"{artifact.span_first_day} – {artifact.span_last_day}"),
        ("Kat sayısı", count(summary.n_folds)),
        ("OOS'u pozitif katların payı", pct(summary.share_positive_oos)),
        (
            "Birleşik OOS",
            f"{count(pooled.n)} işlem · ort. {r_text(pooled.mean_r)} · t "
            f"{num(pooled.t_stat, 2)} · toplam {r_text(pooled.total_r, 2)} · PF "
            f"{num(pooled.profit_factor, 2)} · maks. DD {num(pooled.max_drawdown_r, 2)} R",
        ),
        ("Verimlilik oranı (OOS/IS)", efficiency),
    ]
    note = f'<p class="muted">+{more} eski kat gösterilmedi.</p>' if more > 0 else ""
    return (
        "<p><strong>Parametre araması yapılmadı:</strong> her katta aynı sabit parametreler "
        f'kullanıldı; walk-forward yalnızca kararlılığı ölçer.</p><p class="muted">'
        f"{esc(summary.note)}</p>"
        + kv_list(items)
        + chart
        + table(
            ("Kat", "IS dönemi", "OOS dönemi", "IS işlem · ort.", "OOS işlem · ort.", "Oran"),
            fold_rows,
            empty="Kat yok (veri penceresi yetmedi).",
        )
        + note
    )


# --- data validation and parity ----------------------------------------------------------


def data_validation_body(artifact: DataValidationArtifact) -> str:
    state = badge("temiz", "ok") if artifact.clean else badge("sorun var", "bad")
    keys = list(artifact.report.items())
    rows: list[list[Cell]] = [[k, compact_json(v)] for k, v in keys[:MAX_REPORT_KEYS]]
    more = len(keys) - len(rows)
    note = f'<p class="muted">+{more} alan daha (rapor dosyasında).</p>' if more > 0 else ""
    return (
        f"<p>Sonuç: {state}</p>"
        + table(("Alan", "Değer (özet)"), rows, empty="Raporda ayrıntı yok.")
        + note
    )


def parity_body(artifact: ParityArtifact) -> str:
    state = badge("GEÇTİ", "ok") if artifact.passed else badge("KALDI", "bad")
    items = [
        ("Strateji", artifact.strategy_id),
        ("Eşleşen", count(artifact.matched)),
        ("Uyuşmayan", count(artifact.mismatched)),
        ("Yalnızca Python", count(artifact.python_only)),
        ("Yalnızca TradingView", count(artifact.tradingview_only)),
        (
            "Eşleşme oranı",
            pct(artifact.match_rate)
            if artifact.match_rate is not None
            else "hesaplanamadı (iki tarafta da işlem yok)",
        ),
        ("Eşik", pct(artifact.threshold)),
        ("Kaynaklar", ", ".join(artifact.sources[:MAX_PARAMS]) or "—"),
    ]
    shown = artifact.mismatches[:MAX_MISMATCHES_SHOWN]
    mismatch_rows: list[list[Cell]] = [[compact_json(m)] for m in shown]
    return (
        f"<p>Sonuç: {state}</p>"
        + kv_list(items)
        + f"<h3>Uyuşmazlıklar ({esc(count(len(artifact.mismatches)))}; ilk "
        f"{len(shown)} gösteriliyor)</h3>"
        + table(("Kayıt",), mismatch_rows, empty="Uyuşmazlık yok.")
    )


# --- page --------------------------------------------------------------------------------


def _body(artifact: AnyArtifact) -> str:
    match artifact:
        case GoNoGoArtifact():
            return go_no_go_body(artifact)
        case MonteCarloArtifact():
            return monte_carlo_body(artifact)
        case BacktestArtifact():
            return backtest_body(artifact)
        case CostMatrixArtifact():
            return cost_matrix_body(artifact)
        case WalkForwardArtifact():
            return walk_forward_body(artifact)
        case ParityArtifact():
            return parity_body(artifact)
        case DataValidationArtifact():
            return data_validation_body(artifact)


def _title(artifact: AnyArtifact) -> str:
    base = KIND_TITLES.get(artifact.kind, artifact.kind)
    strategy = getattr(artifact, "strategy_id", None)
    return f"{base}: {strategy}" if isinstance(strategy, str) else base


def render_card(loaded: LoadedArtifact) -> str:
    title = _title(loaded.artifact)
    try:
        return card(title, loaded, _body(loaded.artifact))
    except Exception as exc:  # one bad card must not take the page down (SPEC §6.8 item 5)
        return (
            f'<section class="card an-card" aria-label="{esc(title)}"><h2>{esc(title)}</h2>'
            f'<p class="banner bad" role="status">{esc(loaded.file_name)}: kart çizilemedi '
            f"({esc(type(exc).__name__)}). Rapor dosyası beklenmeyen değerler içeriyor olabilir."
            "</p></section>"
        )


def _sort_key(loaded: LoadedArtifact) -> tuple[int, str]:
    kind = loaded.artifact.kind
    order = KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)
    return order, loaded.artifact.label


def commands_html() -> str:
    rows: list[list[Cell]] = [
        [KIND_TITLES[kind], Raw(f"<code>{esc(REPORT_COMMANDS[kind])}</code>")]
        for kind in KIND_ORDER
    ]
    return table(("Rapor", "Üreten komut"), rows)


def problems_html(problems: Sequence[ArtifactProblem]) -> str:
    if not problems:
        return ""
    shown = problems[:MAX_PROBLEMS]
    # ArtifactError messages already start with the file name; others get it in front.
    items = "".join(
        f"<li>{esc(p.message)}</li>"
        if p.message.startswith(p.file_name)
        else f"<li><code>{esc(p.file_name)}</code>: {esc(p.message)}</li>"
        for p in shown
    )
    more = len(problems) - len(shown)
    extra = f'<p class="muted">+{more} sorunlu dosya daha.</p>' if more > 0 else ""
    return (
        '<section class="card an-card" aria-label="Okunamayan raporlar">'
        f"<h2>Okunamayan raporlar {badge(str(len(problems)), 'warn')}</h2>"
        "<p>Bu dosyalar gösterilmedi; diğer kartları etkilemezler.</p>"
        f'<ul class="an-problems">{items}</ul>{extra}</section>'
    )


def render_analysis(reports_dir: Path) -> str:
    try:
        latest = latest_artifacts(reports_dir)
        artifacts, problems = latest.artifacts, latest.problems
    except OSError as exc:
        artifacts = ()
        problems = (ArtifactProblem(reports_dir.name, f"rapor dizini okunamadı: {exc.strerror}"),)
    ordered = sorted(artifacts, key=_sort_key)
    shown = ordered[:MAX_CARDS]
    synthetic = sum(1 for item in artifacts if item.artifact.synthetic)
    summary = (
        f"{count(len(artifacts))} rapor (her tür ve etiket için en yeni dosya)"
        + (f", {count(synthetic)} tanesi sentetik veriyle" if synthetic else "")
        + (f", {count(len(problems))} dosya okunamadı" if problems else "")
    )
    head = (
        '<section class="card"><h1>Analiz &amp; Simülasyon</h1>'
        f"<p>Rapor klasörü: <code>{esc(str(reports_dir))}</code> · {esc(summary)}.</p>"
        '<p class="muted">Sentetik veriyle üretilen kartlar kanıt değildir. Bir kapı ya da test '
        "yalnızca raporun kendisi öyle diyorsa geçti olarak gösterilir.</p></section>"
    )
    parts = [head, problems_html(problems)]
    if not artifacts:
        parts.append(
            '<section class="card an-card" aria-label="Henüz rapor yok"><h2>Henüz rapor yok</h2>'
            "<p>Bu klasörde okunabilir bir rapor artefaktı (JSON) bulunamadı. Raporlar aşağıdaki "
            "komutlarla üretilir; panel her tür ve etiket için en yeni dosyayı gösterir.</p>"
            + commands_html()
            + "</section>"
        )
        return "".join(parts)
    parts.extend(render_card(item) for item in shown)
    if len(ordered) > len(shown):
        parts.append(
            f'<p class="muted">+{count(len(ordered) - len(shown))} rapor daha gösterilmedi '
            f"(en fazla {MAX_CARDS} kart).</p>"
        )
    missing = [kind for kind in KIND_ORDER if not any(i.artifact.kind == kind for i in artifacts)]
    if missing:
        parts.append(
            '<section class="card"><details><summary>Eksik raporlar: '
            f"{esc(', '.join(KIND_TITLES[k] for k in missing))}</summary>"
            + commands_html()
            + "</details></section>"
        )
    return "".join(parts)
