"""Go/No-Go gate report (SPEC §9, G1-G7): the last check before a paid challenge.

CLAUDE.md invariant 1 lets the bot place orders on its own only after the last Go/No-Go
report says PASS, so a wrong GO is the worst defect this module can have; a wrong NO-GO
is only annoying. Every doubt therefore resolves to "not PASS":

- A gate is PASS only when every check behind it passes on artifacts that are real (not
  synthetic, ADR-0007), not older than `criteria.max_artifact_age_days`, not dated after
  the report, and internally consistent (a hand-edited statistic that does not follow
  from its own inputs is a FAIL). A missing or unreadable input is NOT_EVALUABLE.
- Artifacts are matched to strategies by the `strategy_id` they carry, data validation
  reports to backtests by the data file's sha256 AND the same reading of it (bar time
  zone, bar length, first and last bar); a file's label is never trusted.
- The caller passes EVERY artifact (`artifacts.all_artifacts`), not the newest per label:
  a newer comparison run under the same label would otherwise hide an older decision run.
  When the reader hit its file limit (`artifacts_truncated`) the data gates are
  NOT_EVALUABLE.
- When several artifacts fit, the newest one is used, never the best one: picking among
  runs by their result would be cherry-picking. For G1-G3 the newest run that MAY be a
  decision run decides; if its costs cannot be confirmed it blocks, it is never skipped
  in favour of an older one.
- G4 trusts a Monte Carlo run only when its trade list names its backtest (provenance),
  that backtest is the judged strategies' decision run data, the list's row count and
  R / mae_r sums equal the decision runs', it simulated the pessimistic intratrade model
  and every venue rule it recorded equals today's profile.
- G6 trusts a parity report only when it compared enough trades with today's configured
  parameters and instrument; its data and the decision run's are both shown.
- Risk (user decision of 2026-11-19, open finding 82): the account is planned at
  config/risk.yaml's risk per trade, and R is not size-free (floored sizes, skipped
  trades below the minimum size, the scripts' own percent breakers). A decision run is
  therefore one whose recorded risk input equals the planned risk; a run at any other
  risk (the script's own, "--pine-risk") is a comparison run. G4 states the whole chain
  in one place: decision runs at the planned risk, their trade list, the simulation at
  the planned risk. Parity alone is judged at the script's own risk (it is compared with
  TradingView at the script's inputs), so G6 has its own expected parameter set.
  A trade list written by a run at the script's own risk carries the mark in its rows'
  strategy ids and is refused by G4 for the mark alone: its rows can equal the decision
  run's, so the row digests cannot tell it apart. The planned risk itself is never above
  the code ceiling of SPEC §8.1, whoever calls.
- The Master portfolio may be judged by its two leg ids or by its combined id (user
  decision of 2026-11-19, open finding 72), each from its own artifact. Not both in one
  report: the same trades would be counted twice against the trade list.
- A decision run has the configured OOS start: a run on another IS/OOS boundary is a
  what-if (a comparison run), never the run that decides G2/G3.
- A decision run was also sized and sessioned as today's configuration says: the venue's
  initial balance, the instrument's size grid (qty_step, min_qty), the session gap and
  the spread windows' zone. Each changes which trades exist; a run that differs blocks.
- G4 is judged on the reproducible simulation only: the default seed and horizon, no
  skipped trades and the decision path count. Any other choice is a what-if, and one
  picked among many (seed shopping) would choose the result. The list's rows (times
  included) must hash to the decision runs' rows: the day grouping depends on the times.
- G5 (demo forward) needs Faz 3, which does not exist yet: it is always NOT_EVALUABLE,
  so `evaluate` cannot return GO in Faz 2. That is intended.

Everything here is pure: the caller injects the artifacts, today's date, the creation
time and the names of the files under `docs/compliance/`. Only `write_report` touches
the filesystem.
"""

import datetime as dt
import itertools
import math
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

from propbot.backtest.analytics import expectancy_ratio
from propbot.backtest.artifacts import (
    COST_FREE_TAG,
    GATE_IDS,
    KINDS,
    MAX_EXAMINED_FILES,
    PINE_RISK_SUFFIX,
    SYNTHETIC_TAG,
    AnyArtifact,
    ArtifactError,
    ArtifactProblem,
    BacktestArtifact,
    CostInfo,
    DataInfo,
    DataValidationArtifact,
    Gate,
    GoNoGoArtifact,
    InstrumentInfo,
    LoadedArtifact,
    MonteCarloArtifact,
    ParityArtifact,
    Scalar,
    artifact_file_name,
    build_artifact,
    write_artifact,
)
from propbot.backtest.config import GoNoGoCriteria
from propbot.backtest.model import CostModel
from propbot.backtest.montecarlo import DEFAULT_MAX_DAYS, DEFAULT_PATHS, DEFAULT_SEED
from propbot.backtest.report import risk_shown, tr_int, tr_number
from propbot.core.instrument import Instrument
from propbot.risk.config import MAX_RISK_PER_TRADE_PCT
from propbot.risk.venue import (
    RULES_MAX_AGE_DAYS,
    MaxLossType,
    RulesState,
    VenueProfile,
    rules_freshness,
)

# The engineering thresholds (minimum OOS trades, bar hours per year, Monte Carlo size,
# artifact age) live in `GoNoGoCriteria` (config/backtest.yaml) with code floors.
DAYS_PER_YEAR: Final = 365.25
DEFAULT_LABEL: Final = "gonogo"
_MC_SUBJECT: Final = "Monte Carlo"
# Stored statistics must follow from their own inputs (hand-edited files are possible).
_CONSISTENCY_REL_TOL: Final = 1e-9
_CONSISTENCY_ABS_TOL: Final = 1e-12
_EPS: Final = 2.0**-52
_EVIDENCE_LIMIT: Final = 3800
_REASON_LIMIT: Final = 600
_MARKDOWN_LIMIT_BYTES: Final = 4 * 1024 * 1024
_MARKER = re.compile(r"<!-- propbot-go-no-go created_utc=(?P<stamp>\S+) -->")
_SYNTHETIC_FILE = re.compile(r"synthetic|sentetik", re.IGNORECASE)
# Probabilities of one Monte Carlo run are counts / n_paths: they add up to 1 within
# float rounding (passed + breached + undecided; breach shares = breach).
_PROBABILITY_TOL: Final = 1e-9
# Artifact text reaches the Markdown report: an HTML tag or comment opener (which could
# hide the rest of the report) and an image (a remote fetch when rendered) are defused.
_HTML_OPENER = re.compile(r"<(?=[!/?A-Za-z])")

Status = Literal["PASS", "FAIL", "NOT_EVALUABLE"]

STATUS_TEXT: Final[dict[str, str]] = {
    "PASS": "GEÇTİ",
    "FAIL": "KALDI",
    "NOT_EVALUABLE": "DEĞERLENDİRİLEMEDİ",
}
GATE_TITLES: Final[dict[str, str]] = {
    "G1": "Veri kapsamı ve kalitesi",
    "G2": "Maliyet sonrası OOS kenarı",
    "G3": "Sağlamlık (OOS / IS beklenti)",
    "G4": "Monte Carlo prop simülasyonu",
    "G5": "Demo forward (Faz 3)",
    "G6": "Pine–Python paritesi",
    "G7": "Venue kuralları ve otomasyon izni",
}
# Which gates an unreadable report file of a given kind may hide evidence for.
_PROBLEM_GATES: Final[dict[str, tuple[str, ...]]] = {
    # G4 too: its trade list must come from the newest decision runs (provenance chain).
    "backtest": ("G1", "G2", "G3", "G4"),
    "data_validation": ("G1",),
    "monte_carlo": ("G4",),
    "parity": ("G6",),
}
_ALL_DATA_GATES: Final = ("G1", "G2", "G3", "G4", "G6")
_RANK: Final[dict[str, int]] = {"PASS": 0, "NOT_EVALUABLE": 1, "FAIL": 2}

G1_MANUAL_ITEM: Final = (
    "G1: verinin işlem yapılacak mekanın (ya da eşdeğer) beslemesinden geldiği elle teyit "
    "edilmeli (kod yalnızca dosyanın kendisini görür)."
)
G3_MANUAL_ITEM: Final = (
    "G3: gün/saat filtrelerinin yalnızca IS örnekleminde seçildiği elle teyit edilmeli "
    "(kod bunu denetleyemez; G3'ün GEÇTİ sonucu yalnızca OOS/IS oranını kapsar)."
)
G4_MANUAL_ITEM: Final = (
    "G4: Monte Carlo işlem dosyasının, değerlendirilen stratejilerin maliyet sonrası işlem "
    "listesi olduğu (R değerleri net ya da --cost-r ile maliyet eklenmiş) elle teyit edilmeli."
)
EXPECTED_COSTS_MISSING: Final = (
    "beklenen maliyetler verilmedi (config/backtest.yaml): bir backtest'in karar koşusu olduğu "
    "doğrulanamaz"
)
EXPECTED_PARAMS_MISSING: Final = (
    "beklenen strateji parametreleri verilmedi (config/strategies.yaml): bir backtest'in karar "
    "koşusu olduğu doğrulanamaz"
)
EXPECTED_PARITY_PARAMS_MISSING: Final = (
    "beklenen parite parametreleri verilmedi (config/strategies.yaml, betiğin kendi riskiyle): "
    "parite raporunun bugünkü stratejiyi karşılaştırdığı doğrulanamaz"
)
RISK_PARAMS_MISSING: Final = (
    "stratejilerin işlem başı risk girdisinin adı verilmedi: bir koşunun planlanan riskle "
    "(config/risk.yaml) boyutlandırıldığı doğrulanamaz"
)
PARITY_RISK_SENTENCE: Final = (
    "parite betiğin kendi işlem başı riskiyle değerlendirilir (2026-11-19 kullanıcı kararının "
    "uygulaması, ADR-0010): "
    "TradingView karşılaştırması betiğin girdileriyle yapılır; planlanan risk (config/risk.yaml) "
    "yalnızca karar koşularına yazılır"
)
EXPECTED_INSTRUMENTS_MISSING: Final = (
    "beklenen enstrüman profilleri verilmedi (config/backtest.yaml): tik büyüklüğü ve puan değeri "
    "maliyetin R karşılığını belirler, bir backtest'in karar koşusu olduğu doğrulanamaz"
)
EXPECTED_OOS_MISSING: Final = (
    "yapılandırılmış OOS başlangıcı verilmedi (config/backtest.yaml samples.oos_start): bir "
    "backtest'in karar koşusu olduğu doğrulanamaz"
)
EXPECTED_SESSION_MISSING: Final = (
    "yapılandırılmış seans sonu boşluğu verilmedi (config/backtest.yaml "
    "session.min_gap_minutes): bir backtest'in karar koşusu olduğu doğrulanamaz"
)
TRUNCATION_UNKNOWN: Final = (
    "rapor dizinindeki her dosyanın okunup okunmadığı bilinmiyor (all_artifacts sonucu "
    "verilmedi): okunmayan bir rapor kararı değiştirebilir"
)
UNKNOWN_TRADES_SOURCE: Final = (
    "işlem listesinin kaynağı bilinmiyor; listeyi propbot backtest üretmeli"
)
G5_SENTENCE: Final = (
    "Demo forward testi Faz 3'te (MT5 köprüsü, demo hesap) yapılır; Faz 3 henüz yok, bu "
    "kapı değerlendirilemez ve bu yüzden rapor GO veremez."
)
CLOSING_REMINDER: Final = (
    "GO gerekli ama yeterli değildir: ücretli sınava (Faz 4) geçiş ayrıca kullanıcının kendi "
    "kararını ister (CLAUDE.md faz tablosu)."
)


class GoNoGoError(Exception):
    """The report cannot be produced from these inputs. The message is Turkish."""


@dataclass(frozen=True, slots=True)
class GoNoGoInputs:
    artifacts: Sequence[LoadedArtifact]  # from latest_artifacts or given explicitly
    criteria: GoNoGoCriteria
    venue_name: str  # must equal the Monte Carlo artifact's `inputs.venue`
    venue: VenueProfile
    strategy_ids: Sequence[str]  # strategies to judge; empty judges nothing (not a pass)
    planned_risk_pct: float  # risk per trade the account will use (config/risk.yaml)
    today: dt.date  # UTC date of `created`
    created: dt.datetime  # report time, timezone-aware
    compliance_files: Sequence[str]  # names under docs/compliance/ (read by the caller)
    problems: Sequence[ArtifactProblem] = ()  # files all_artifacts could not read
    label: str = DEFAULT_LABEL
    # Instrument symbol -> the configured costs (config/backtest.yaml, factor 1.0). A
    # backtest is a decision run only when its recorded costs equal these; None means the
    # caller did not say, and G1-G4 cannot be judged (fail closed).
    expected_costs: Mapping[str, CostModel] | None = None
    # all_artifacts examined only the newest MAX_EXAMINED_FILES files (`truncated`). None =
    # the caller did not say (e.g. it read latest_artifacts): treated like True, fail closed.
    artifacts_truncated: bool | None = None
    # Instrument symbol -> the configured instrument (config/backtest.yaml). Tick size and
    # point value turn the recorded costs into R, so equal cost numbers on another tick
    # size are other costs. None: as `expected_costs`, no run can be a decision run.
    expected_instruments: Mapping[str, Instrument] | None = None
    # Strategy id -> the effective parameters config/strategies.yaml gives today
    # (runner.effective_params; a Master leg id maps to the master_portfolio spec's, which
    # its leg artifacts record). A run made with other parameters tested another strategy,
    # so it cannot be the decision run. None or a missing id: no run can be confirmed.
    # Since the user decision of 2026-11-19 these carry the PLANNED risk in the risk input
    # (runner.effective_params(planned_risk_pct=...)): they are what a decision run records.
    expected_params: Mapping[str, Mapping[str, Scalar]] | None = None
    # The same configured parameters WITHOUT the planned-risk substitution: what a parity
    # run records (it is compared with TradingView at the script's own inputs). G6 judges
    # the parity report against these, never against `expected_params`. None or a missing
    # id: unknown, G6 is NOT_EVALUABLE.
    expected_parity_params: Mapping[str, Mapping[str, Scalar]] | None = None
    # Strategy id -> the name of its per-trade risk input (registry `risk_param`; the
    # scripts do not agree on it). A backtest's recorded risk is read through it. None or
    # a missing id: no run can be confirmed as sized at the planned risk (fail closed).
    risk_params: Mapping[str, str] | None = None
    # Judged id -> the id of the Pine script that produces it (a Master leg -> the
    # portfolio's id; every other id is its own script and may be left out). TradingView
    # has one trade list per script, so a leg has no parity report of its own: G6 reads
    # its script's. None: every id is looked up as its own script (a leg then has none).
    script_ids: Mapping[str, str] | None = None
    # config/backtest.yaml's samples.oos_start as a UTC stamp ("2025-01-01T00:00:00Z"). A
    # run with another recorded OOS start is a what-if on a chosen boundary (a comparison
    # run); None = unknown, no run can be confirmed as the decision run.
    expected_oos_start_utc: str | None = None
    # The directory `compliance_files` was listed from, as the caller resolved it, and
    # whether it exists (None = the caller did not say). Shown in G7's manual item.
    compliance_dir: str | None = None
    compliance_dir_found: bool | None = None
    # config/backtest.yaml's session.min_gap_minutes in seconds. Where sessions end decides
    # Pine's session.islastbar, which the strategies read (entry gates, forced exits): a
    # run with another gap made other trades. None = unknown, no run can be confirmed.
    expected_min_gap_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: Gate
    used: tuple[LoadedArtifact, ...]  # the artifacts the gate was judged from


@dataclass(frozen=True, slots=True)
class _Part:
    """One subject of a gate (a strategy, the Monte Carlo run, the venue)."""

    subject: str
    status: Status
    reasons: tuple[str, ...]
    used: tuple[LoadedArtifact, ...]
    values: dict[str, JsonValue]


# --- public API ---------------------------------------------------------------------------


def evaluate(inputs: GoNoGoInputs) -> GoNoGoArtifact:
    """Judge the seven gates and build the (validated) Go/No-Go artifact."""
    _check_inputs(inputs)
    results = [
        gate_data(inputs),
        gate_edge(inputs),
        gate_robustness(inputs),
        gate_monte_carlo(inputs),
        gate_demo_forward(inputs),
        gate_parity(inputs),
        gate_rules(inputs),
    ]
    return assemble(results, label=inputs.label, created=inputs.created)


def assemble(results: Sequence[GateResult], *, label: str, created: dt.datetime) -> GoNoGoArtifact:
    """GO only when all seven gates are PASS and no input is synthetic (the model agrees)."""
    gates = tuple(result.gate for result in results)
    synthetic = any(item.artifact.synthetic for result in results for item in result.used)
    ids = sorted(gate.id for gate in gates)
    every_pass = ids == list(GATE_IDS) and all(gate.status == "PASS" for gate in gates)
    go = every_pass and not synthetic
    reasons: list[str] = []
    if synthetic:
        reasons.append(
            f"{SYNTHETIC_TAG}: girdilerden en az biri sentetik; GO verilemez (ADR-0007)."
        )
    missing = [gate_id for gate_id in GATE_IDS if gate_id not in ids]
    if missing:
        reasons.append("eksik kapı: " + ", ".join(missing))
    for gate in gates:
        if gate.status != "PASS":
            first = gate.evidence.splitlines()[0] if gate.evidence else ""
            more = _non_passing_parts(gate) - 1
            extra = f" (+{more} konu daha)" if more > 0 else ""
            reasons.append(
                _clip(f"{gate.id} {STATUS_TEXT[gate.status]}: {first}{extra}", _REASON_LIMIT)
            )
    if go:
        reasons.append("G1-G7 kapılarının hepsi GEÇTİ ve hiçbir girdi sentetik değil.")
    notes = [CLOSING_REMINDER]
    try:
        return build_artifact(
            GoNoGoArtifact,
            label=label,
            created=created,
            synthetic=synthetic,
            data=None,
            notes=notes,
            gates=gates,
            overall="GO" if go else "NO_GO",
            reasons=tuple(reasons),
        )
    except ArtifactError as exc:
        raise GoNoGoError(str(exc)) from None


def gate_data(inputs: GoNoGoInputs) -> GateResult:
    """G1: enough real 1-minute data, validated clean (same file by sha256)."""
    criteria = inputs.criteria
    return _per_strategy_gate(
        "G1",
        inputs,
        lambda sid: _g1_part(inputs, sid),
        {
            "min_data_years": criteria.min_data_years,
            "required_tf_seconds": criteria.required_tf_seconds,
            "min_bar_hours_per_year": criteria.min_bar_hours_per_year,
            "max_artifact_age_days": criteria.max_artifact_age_days,
        },
        manual=(G1_MANUAL_ITEM,),
    )


def gate_edge(inputs: GoNoGoInputs) -> GateResult:
    """G2: OOS expectancy after costs > 0 and t-stat >= the floor, on a decision run."""
    return _per_strategy_gate(
        "G2",
        inputs,
        lambda sid: _g2_part(inputs, sid),
        {
            "min_oos_t_stat": inputs.criteria.min_oos_t_stat,
            "min_oos_trades": inputs.criteria.min_oos_trades,
        },
    )


def gate_robustness(inputs: GoNoGoInputs) -> GateResult:
    """G3: OOS expectancy >= ratio x IS expectancy (the filter question stays manual)."""
    return _per_strategy_gate(
        "G3",
        inputs,
        lambda sid: _g3_part(inputs, sid),
        {
            "min_oos_to_is_ratio": inputs.criteria.min_oos_to_is_ratio,
            "min_oos_trades": inputs.criteria.min_oos_trades,
        },
        manual=(G3_MANUAL_ITEM,),
    )


def gate_monte_carlo(inputs: GoNoGoInputs) -> GateResult:
    """G4: the newest Monte Carlo run for this venue, at the planned risk and rules."""
    criteria = inputs.criteria
    part = _g4_part(inputs)
    parts = [part, *_problem_parts(inputs, "G4")]
    return _gate(
        "G4",
        parts,
        {
            "min_p_pass_all_phases": criteria.min_p_pass_all_phases,
            "max_p_breach": criteria.max_p_breach,
            "min_paths": criteria.min_mc_paths,
            "min_trades": criteria.min_mc_trades,
            "planned_risk_pct": inputs.planned_risk_pct,
        },
        manual=(G4_MANUAL_ITEM,),
    )


def gate_demo_forward(inputs: GoNoGoInputs) -> GateResult:
    """G5: Faz 3 does not exist yet, so this is never PASS here."""
    del inputs  # the gate has no evidence to read until Faz 3
    part = _Part("demo forward", "NOT_EVALUABLE", (G5_SENTENCE,), (), {})
    return _gate("G5", [part], {})


def gate_parity(inputs: GoNoGoInputs) -> GateResult:
    """G6: a passed parity report per strategy with a match rate >= the floor."""
    return _per_strategy_gate(
        "G6",
        inputs,
        lambda sid: _g6_part(inputs, sid),
        {
            "min_parity_rate": inputs.criteria.min_parity_rate,
            "min_parity_trades": inputs.criteria.min_parity_trades,
        },
    )


def gate_rules(inputs: GoNoGoInputs) -> GateResult:
    """G7: fresh venue rules and automation allowed; written approval stays manual."""
    venue = inputs.venue
    freshness = rules_freshness(venue, inputs.today)
    reasons: list[tuple[Status, str]] = []
    verified = venue.rules_verified_at
    if freshness.state is RulesState.MISSING:
        reasons.append(("FAIL", "rules_verified_at boş: venue kuralları doğrulanmamış"))
    elif freshness.state is RulesState.STALE:
        reasons.append(
            (
                "FAIL",
                f"rules_verified_at {verified} — {freshness.age_days} gün önce; sınır "
                f"{RULES_MAX_AGE_DAYS} gün",
            )
        )
    elif freshness.state is RulesState.FUTURE:
        reasons.append(
            ("NOT_EVALUABLE", f"rules_verified_at {verified} ileri tarihli (yazım hatası?)")
        )
    else:
        reasons.append(("PASS", f"kurallar {freshness.age_days} gün önce doğrulandı ({verified})"))
    if venue.automation_allowed:
        reasons.append(("PASS", "profilde otomasyon izinli (automation_allowed: true)"))
    else:
        reasons.append(("FAIL", "profilde otomasyon izinli değil (automation_allowed: false)"))
    files = sorted(set(inputs.compliance_files))
    directory = inputs.compliance_dir or "docs/compliance/"
    if inputs.compliance_dir_found is False:
        # Not "no files": the folder itself was not found where it was looked for.
        listed = "klasör yok (bu yolda bulunamadı; --compliance-dir ile verin)"
    else:
        listed = ", ".join(files) if files else "dosya yok"
    manual = (
        "G7: firmanın gerektirdiği yerde yazılı otomasyon onayının docs/compliance/ altında "
        f"olduğu elle teyit edilmeli (kod karar veremez). {directory}: {listed}.",
    )
    part = _part(
        inputs.venue_name,
        reasons,
        (),
        {
            "venue": inputs.venue_name,
            "rules_verified_at": None if verified is None else verified.isoformat(),
            "rules_state": freshness.state.value,
            "age_days": freshness.age_days,
            "automation_allowed": venue.automation_allowed,
            "compliance_files": list[JsonValue](files),
            "compliance_dir": directory,
            "compliance_dir_found": inputs.compliance_dir_found,
        },
    )
    return _gate("G7", [part], {"rules_max_age_days": RULES_MAX_AGE_DAYS}, manual=manual)


def report_file_name(artifact: GoNoGoArtifact) -> str:
    """`go_no_go_YYYYMMDD.md` (SPEC §9), from the artifact's UTC date."""
    return f"go_no_go_{_created(artifact).strftime('%Y%m%d')}.md"


def write_report(artifact: GoNoGoArtifact, reports_dir: Path) -> tuple[Path, Path]:
    """Write the JSON artifact (never overwritten) and the day's Markdown report.

    The JSON file of every run is kept (its name carries the second). The Markdown file
    keeps the SPEC name, one per day, and always shows the NEWEST verdict of that day:
    a later run replaces it atomically. It is never replaced by an older run, and a file
    of that name that this module did not write (no marker line) is never replaced.
    """
    markdown_path = reports_dir / report_file_name(artifact)
    text = render_markdown(artifact)
    _check_replaceable(markdown_path, artifact)
    json_path = write_artifact(artifact, reports_dir)
    _replace_atomically(markdown_path, text.encode("utf-8"))
    return markdown_path, json_path


def render_markdown(artifact: GoNoGoArtifact) -> str:
    """The Turkish report: verdict, gate table, details, manual items, inputs, reminder."""
    created = _created(artifact)
    venue_text = next(
        (name for gate in artifact.gates if gate.id == "G7" and (name := _g7_venue(gate))),
        "belirtilmedi",
    )
    verdict = "GO" if artifact.overall == "GO" else "NO-GO"
    lines = [
        f"<!-- propbot-go-no-go created_utc={artifact.created_utc} -->",
        f"# Go/No-Go raporu — {created.strftime('%Y-%m-%d')} — {_cell(venue_text)}",
        "",
        f"**Karar: {verdict}** · oluşturulma {artifact.created_utc} · rapor dosyası "
        f"`{artifact_file_name(artifact)}`",
        "",
    ]
    if artifact.synthetic:
        lines += [
            f"> **{SYNTHETIC_TAG}** — girdilerden en az biri sentetik veriyle üretildi; bu rapor "
            "hiçbir kapıyı kanıtlamaz ve GO veremez (ADR-0007).",
            "",
        ]
    lines += ["## Gerekçeler", ""]
    lines += [f"- {_line(reason)}" for reason in artifact.reasons] or ["- yok"]
    lines += [
        "",
        "## Kapılar",
        "",
        "| Kapı | Başlık | Durum | Kanıt |",
        "|---|---|---|---|",
    ]
    for gate in artifact.gates:
        lines.append(
            f"| {gate.id} | {_cell(gate.title)} | {STATUS_TEXT[gate.status]} | "
            f"{_cell(gate.evidence)} |"
        )
    lines += ["", "## Ayrıntılar", ""]
    for gate in artifact.gates:
        lines += [f"### {gate.id} — {_line(gate.title)}: {STATUS_TEXT[gate.status]}", ""]
        lines += _render_details(gate.details)
        lines.append("")
    manual = _collect_strings(artifact, "manual")
    lines += ["## Elle teyit edilecekler", ""]
    lines += [f"- {_line(item)}" for item in manual] or ["- yok"]
    lines += ["", "## Girdi dosyaları", ""]
    files = _collect_files(artifact)
    if files:
        lines += ["| Dosya | Tür | Oluşturulma (UTC) | Sentetik |", "|---|---|---|---|"]
        for name, kind, stamp, synthetic in files:
            mark = SYNTHETIC_TAG if synthetic else "hayır"
            lines.append(f"| `{_cell(name)}` | {_cell(kind)} | {_cell(stamp)} | {mark} |")
    else:
        lines.append("- girdi dosyası yok")
    problems = _collect_strings(artifact, "problems")
    if problems:
        lines += ["", "## Okunamayan rapor dosyaları", ""]
        lines += [f"- {_line(item)}" for item in problems]
    other_notes = [note for note in artifact.notes if note != CLOSING_REMINDER]
    if other_notes:
        lines += ["", "## Notlar", ""]
        lines += [f"- {_line(note)}" for note in other_notes]
    lines += ["", "---", "", f"**Hatırlatma:** {CLOSING_REMINDER}", ""]
    return "\n".join(lines)


# --- gate parts ---------------------------------------------------------------------------


def _g1_part(inputs: GoNoGoInputs, sid: str) -> _Part:
    criteria = inputs.criteria
    selection = _backtest_for(inputs, sid)
    chosen, others = selection.chosen, selection.runs
    if selection.blocker is not None:
        return _blocked_part(inputs, sid, selection)
    if chosen is None:
        if others:
            newest = others[0]
            if newest.artifact.synthetic:
                return _part(
                    sid,
                    [("FAIL", f"{SYNTHETIC_TAG}: {newest.file_name} sentetik veriyle üretildi")],
                    (newest,),
                    {"backtest_file": newest.file_name},
                )
            return _no_decision_run(sid, others)
        return _part(sid, [("NOT_EVALUABLE", "backtest raporu yok")], (), {"backtest_file": None})
    bt = _backtest(chosen)
    values: dict[str, JsonValue] = {"backtest_file": chosen.file_name}
    blocked = _unusable(inputs, chosen)
    if blocked is not None:
        return _part(sid, [blocked], (chosen,), values)
    data = bt.data
    if data is None:
        return _part(
            sid,
            [("NOT_EVALUABLE", f"{chosen.file_name} veri bilgisi taşımıyor")],
            (chosen,),
            values,
        )
    values.update(
        data_file=data.file_name,
        sha256=data.sha256 or None,
        tf_seconds=data.tf_seconds,
        bars=data.bars,
        first_utc=data.first_utc,
        last_utc=data.last_utc,
    )
    checks: list[tuple[Status, str]] = []
    if data.tf_seconds > criteria.required_tf_seconds:
        checks.append(
            (
                "FAIL",
                f"bar uzunluğu {tr_int(data.tf_seconds)} sn > "
                f"{tr_int(criteria.required_tf_seconds)} sn",
            )
        )
    if data.bars == 0 or data.first_utc is None or data.last_utc is None:
        checks.append(("FAIL", f"{data.file_name}: veri boş"))
    else:
        first = dt.datetime.fromisoformat(data.first_utc)
        last = dt.datetime.fromisoformat(data.last_utc)
        years = (last - first).total_seconds() / (DAYS_PER_YEAR * 86_400)
        values["span_years"] = years
        if years < criteria.min_data_years:
            checks.append(
                (
                    "FAIL",
                    f"veri kapsamı {tr_number(years, 2)} yıl < "
                    f"{tr_number(criteria.min_data_years, 2)} yıl",
                )
            )
        hours = data.bars * data.tf_seconds / 3_600
        needed = criteria.min_data_years * criteria.min_bar_hours_per_year
        if hours < needed:
            checks.append(
                (
                    "FAIL",
                    f"bar sayısı {tr_int(data.bars)} yalnızca {tr_number(hours, 0)} saat kapsıyor "
                    f"< {tr_number(needed, 0)} saat: veri seyrek ya da eksik",
                )
            )
    used = [chosen]
    if not data.sha256:
        checks.append(("FAIL", f"{data.file_name}: sha256 yok, doğrulama raporuna bağlanamaz"))
    else:
        validation = _validation_for(inputs, data)
        if isinstance(validation, str):
            status: Status = "FAIL" if validation.startswith("!") else "NOT_EVALUABLE"
            checks.append((status, validation.lstrip("!")))
            values["validation_file"] = None
        else:
            used.append(validation)
            report = _validation(validation)
            values["validation_file"] = validation.file_name
            values["validation_clean"] = report.clean
            values["bar_tz"] = data.bar_tz
            check = _unusable(inputs, validation)
            if check is not None:
                checks.append(check)
            elif not report.clean:
                checks.append(
                    ("FAIL", f"{validation.file_name}: veri doğrulama raporu temiz değil")
                )
    if not checks:
        span = values.get("span_years")
        span_text = tr_number(span, 2) if isinstance(span, float) else "?"
        checks.append(
            (
                "PASS",
                f"{data.file_name}: {tr_int(data.tf_seconds)} sn bar, {span_text} yıl, "
                f"bar saat dilimi {data.bar_tz}; doğrulama raporu aynı okumayla temiz",
            )
        )
    return _part(sid, checks, tuple(used), values)


def _g2_part(inputs: GoNoGoInputs, sid: str) -> _Part:
    selection = _backtest_for(inputs, sid)
    chosen, others = selection.chosen, selection.runs
    if selection.blocker is not None:
        return _blocked_part(inputs, sid, selection)
    if chosen is None:
        if others:
            names = _run_names(inputs, others)
            return _part(
                sid,
                [
                    (
                        "FAIL",
                        "yalnızca karşılaştırma koşusu var (tv_ohlc dolum modeli, maliyetsiz, "
                        "maliyet çarpanı 1,0 değil, başka OOS başlangıcı ya da planlanan riskten "
                        "başka risk); G2 yalnızca config maliyetleriyle (çarpan 1,0), config OOS "
                        "başlangıcıyla ve planlanan riskle 'pessimistic' koşuyla sağlanır: "
                        f"{names}",
                    )
                ],
                others[:1],
                {"backtest_file": others[0].file_name},
            )
        return _part(sid, [("NOT_EVALUABLE", "backtest raporu yok")], (), {"backtest_file": None})
    bt = _backtest(chosen)
    oos = bt.stats_oos
    values: dict[str, JsonValue] = {
        "backtest_file": chosen.file_name,
        "fill_model": bt.fill_model,
        "cost_free": bt.costs.cost_free,
        "cost_factor": bt.cost_factor,
        "oos_start_utc": bt.oos_start_utc,
        "oos_n": oos.n,
        "oos_mean_r": oos.mean_r,
        "oos_stdev_r": oos.stdev_r,
        "oos_t_stat": oos.t_stat,
    }
    blocked = _unusable(inputs, chosen)
    if blocked is not None:
        return _part(sid, [blocked], (chosen,), values)
    minimum = inputs.criteria.min_oos_t_stat
    min_trades = inputs.criteria.min_oos_trades
    checks: list[tuple[Status, str]] = []
    if oos.n <= 0:
        checks.append(("FAIL", "OOS örnekleminde işlem yok"))
    elif oos.n < min_trades:
        checks.append(
            (
                "NOT_EVALUABLE",
                f"OOS örneklemi {tr_int(oos.n)} işlem < {tr_int(min_trades)}: "
                "t-istatistiği bu kadar az işlemle anlamlı değil",
            )
        )
    if oos.mean_r is None:
        checks.append(("FAIL", "OOS beklentisi yok"))
    elif not oos.mean_r > 0.0:
        checks.append(("FAIL", f"OOS beklentisi {tr_number(oos.mean_r, 3)} R ≤ 0"))
    if oos.t_stat is None:
        checks.append(
            ("FAIL", "OOS t-istatistiği yok (en az 2 işlem ve sıfırdan büyük sapma gerekir)")
        )
    else:
        expected = _t_stat(oos.mean_r, oos.stdev_r, oos.n)
        if expected is None or not _close(oos.t_stat, expected):
            checks.append(("FAIL", "OOS t-istatistiği ortalama, sapma ve işlem sayısıyla tutarsız"))
        elif min(oos.t_stat, expected) < minimum:
            # The smaller of stored and recomputed decides: the consistency tolerance must
            # not lift a value that is really below the floor onto it.
            checks.append(
                (
                    "FAIL",
                    f"OOS t-istatistiği {tr_number(oos.t_stat, 2)} < {tr_number(minimum, 2)}",
                )
            )
    if not checks:
        assert oos.mean_r is not None
        assert oos.t_stat is not None
        checks.append(
            (
                "PASS",
                f"OOS {tr_int(oos.n)} işlem, beklenti {tr_number(oos.mean_r, 3)} R, "
                f"t = {tr_number(oos.t_stat, 2)} ≥ {tr_number(minimum, 2)}",
            )
        )
    return _part(sid, checks, (chosen,), values)


def _g3_part(inputs: GoNoGoInputs, sid: str) -> _Part:
    selection = _backtest_for(inputs, sid)
    chosen, others = selection.chosen, selection.runs
    if selection.blocker is not None:
        return _blocked_part(inputs, sid, selection)
    if chosen is None:
        if others:
            return _no_decision_run(sid, others)
        return _part(sid, [("NOT_EVALUABLE", "backtest raporu yok")], (), {"backtest_file": None})
    bt = _backtest(chosen)
    values: dict[str, JsonValue] = {
        "backtest_file": chosen.file_name,
        "g3_ratio": bt.g3_ratio,
        "is_mean_r": bt.stats_is.mean_r,
        "oos_mean_r": bt.stats_oos.mean_r,
        "g3_note": bt.g3_note,
    }
    blocked = _unusable(inputs, chosen)
    if blocked is not None:
        return _part(sid, [blocked], (chosen,), values)
    minimum = inputs.criteria.min_oos_to_is_ratio
    min_trades = inputs.criteria.min_oos_trades
    expected, note = expectancy_ratio(bt.stats_oos.mean_r, bt.stats_is.mean_r)
    ratio = bt.g3_ratio
    checks: list[tuple[Status, str]] = []
    small = [
        f"{name} örneklemi {tr_int(sample.n)} işlem"
        for name, sample in (("IS", bt.stats_is), ("OOS", bt.stats_oos))
        if sample.n < min_trades
    ]
    if small:
        checks.append(
            (
                "NOT_EVALUABLE",
                f"{', '.join(small)} < {tr_int(min_trades)}: oran bu kadar az işlemle "
                "anlamlı değil",
            )
        )
    if ratio is None:
        why = bt.g3_note or note or "neden belirtilmemiş"
        checks.append(("FAIL", f"G3 oranı yok: {why}"))
    elif expected is None or not _close(ratio, expected):
        checks.append(("FAIL", "G3 oranı IS ve OOS beklentileriyle tutarsız"))
    elif min(ratio, expected) < minimum:
        # As in G2: the tolerance never lifts a ratio that is really below the floor.
        checks.append(("FAIL", f"OOS/IS oranı {tr_number(ratio, 2)} < {tr_number(minimum, 2)}"))
    elif not small:
        checks.append(
            (
                "PASS",
                f"OOS/IS oranı {tr_number(ratio, 2)} ≥ {tr_number(minimum, 2)} "
                "(yalnızca oran; filtre seçimi elle teyit edilecek)",
            )
        )
    return _part(sid, checks, (chosen,), values)


def _g4_part(inputs: GoNoGoInputs) -> _Part:
    criteria = inputs.criteria
    runs = _newest_first(
        item for item in inputs.artifacts if isinstance(item.artifact, MonteCarloArtifact)
    )
    mine = [item for item in runs if _monte_carlo(item).inputs.venue == inputs.venue_name]
    if not mine:
        if runs:
            names = ", ".join(
                f"{item.file_name} ({_monte_carlo(item).inputs.venue})" for item in runs
            )
            text = f"'{inputs.venue_name}' için Monte Carlo raporu yok; başka venue için: {names}"
        else:
            text = "Monte Carlo raporu yok"
        return _part(_MC_SUBJECT, [("NOT_EVALUABLE", text)], (), {"file": None})
    chosen = mine[0]
    mc = _monte_carlo(chosen)
    twins = [item for item in mine[1:] if _created(item.artifact) == _created(mc)]
    params = mc.inputs
    p_pass = mc.p_pass_phase[-1]
    values: dict[str, JsonValue] = {
        "file": chosen.file_name,
        "venue": params.venue,
        "trades_file": params.trades_file,
        "n_trades": params.n_trades,
        "risk_pct": params.risk_pct,
        "cost_r": params.cost_r,
        "n_paths": params.n_paths,
        "seed": params.seed,
        "max_days": params.max_days,
        "skip_prob": params.skip_prob,
        "experimental_risk_steps": params.experimental_risk_steps,
        "intratrade": params.intratrade,
        "sum_r": params.sum_r,
        "sum_mae_r": params.sum_mae_r,
        "max_loss_type": params.rules.max_loss_type,
        "daily_loss_basis": params.rules.daily_loss_basis,
        "daily_reset": None
        if params.rules.daily_reset_tz is None or params.rules.daily_reset_time is None
        else f"{params.rules.daily_reset_time} {params.rules.daily_reset_tz}",
        "p_pass_all_phases": p_pass,
        "p_breach": mc.p_breach,
        "p_undecided": mc.p_undecided,
        "provenance": None if mc.provenance is None else mc.provenance.model_dump(mode="json"),
    }
    if _SYNTHETIC_FILE.search(params.trades_file) is not None and not mc.synthetic:
        return _part(
            _MC_SUBJECT,
            [("FAIL", f"{SYNTHETIC_TAG}: işlem dosyası adı sentetik ({params.trades_file})")],
            (chosen,),
            values,
        )
    if mc.provenance is not None and mc.provenance.synthetic:
        return _part(
            _MC_SUBJECT,
            [
                (
                    "FAIL",
                    f"{SYNTHETIC_TAG}: işlem listesi sentetik veriyle üretilmiş (köken satırı, "
                    f"{params.trades_file})",
                )
            ],
            (chosen,),
            values,
        )
    blocked = _unusable(inputs, chosen)
    if blocked is not None:
        return _part(_MC_SUBJECT, [blocked], (chosen,), values)
    link, decision_runs = _provenance_checks(inputs, mc)
    chain = decision_risk_checks(inputs, decision_runs)
    checks = [*link, *chain]
    if twins:
        # The file name would pick between them: neither decides.
        names = ", ".join(item.file_name for item in twins)
        checks.append(("NOT_EVALUABLE", f"{_SAME_SECOND} Monte Carlo koşusu var: {names}"))
    inconsistent = _probabilities_inconsistent(mc)
    if inconsistent is not None:
        checks.append(("FAIL", inconsistent))
    if params.experimental_risk_steps:
        checks.append(
            (
                "NOT_EVALUABLE",
                "deneysel risk basamaklarıyla koşulmuş; sabit seçilen riskle yeniden koşun",
            )
        )
    # Exact equality, as for the decision runs: the simulation records the value it was
    # given (risk.yaml's, or --risk-pct) and JSON keeps a float bit for bit, so the planned
    # risk reads back as itself. A tolerance would only let another risk pass for it.
    risk_ok = params.risk_pct == inputs.planned_risk_pct
    if not risk_ok:
        # Exact, like every other risk in a refusal: rounded to two decimals 0.25 and
        # 0.250000001 would read as the same number next to "not the chosen risk".
        checks.append(
            (
                "NOT_EVALUABLE",
                f"simülasyon riski {risk_shown(params.risk_pct)}, planlanan risk "
                f"{risk_shown(inputs.planned_risk_pct)}: seçilen riskle koşulmamış",
            )
        )
    checks += [("NOT_EVALUABLE", text) for text in _rules_mismatch(mc, inputs.venue)]
    if params.intratrade is None:
        checks.append(
            (
                "NOT_EVALUABLE",
                "işlem içi model kaydedilmemiş (eski rapor): yüzen zararın sayılıp sayılmadığı "
                "bilinmiyor; Monte Carlo'yu yeniden koşun",
            )
        )
    elif params.intratrade != "stop":
        checks.append(
            (
                "NOT_EVALUABLE",
                f"işlem içi model '{params.intratrade}' (yüzen zarar yok sayıldı, ihlal "
                "olasılığı iyimser); G4 yalnızca kötümser 'stop' modeliyle geçer",
            )
        )
    knobs = _chosen_knobs(mc, criteria)
    if knobs:
        checks.append(
            (
                "NOT_EVALUABLE",
                "Monte Carlo tekrar üretilebilir ayarlarla koşulmamış ("
                + "; ".join(knobs)
                + "): bu ayarlar sonucu değiştirir ve firma kuralı değildir; birçok değer "
                "arasından seçmek sonucu seçmektir. Varsayılanlarla yeniden koşun",
            )
        )
    if params.n_paths < criteria.min_mc_paths:
        checks.append(
            (
                "NOT_EVALUABLE",
                f"yol sayısı {tr_int(params.n_paths)} < {tr_int(criteria.min_mc_paths)}: "
                "simülasyon çok küçük",
            )
        )
    if params.n_trades < criteria.min_mc_trades:
        checks.append(
            (
                "NOT_EVALUABLE",
                f"işlem sayısı {tr_int(params.n_trades)} < {tr_int(criteria.min_mc_trades)}: "
                "girdi çok küçük",
            )
        )
    if p_pass < criteria.min_p_pass_all_phases:
        checks.append(
            (
                "FAIL",
                f"P(tüm fazlar) {_pct(p_pass)} < {_pct(criteria.min_p_pass_all_phases)}",
            )
        )
    if mc.p_breach > criteria.max_p_breach:
        checks.append(("FAIL", f"P(ihlal) {_pct(mc.p_breach)} > {_pct(criteria.max_p_breach)}"))
    summary = (
        f"girdi {params.trades_file}: {tr_int(params.n_trades)} işlem, risk "
        f"%{tr_number(params.risk_pct, 2)}, maliyet {tr_number(params.cost_r, 3)} R, "
        f"{tr_int(params.n_paths)} yol, tohum {params.seed}, işlem içi model "
        f"{params.intratrade or 'kaydedilmemiş'}; ufuk {tr_int(params.max_days)} "
        f"piyasa günü, işlem atlama olasılığı {tr_number(params.skip_prob, 4)} (ikisi de firma "
        f"kuralı değil; G4 yalnızca {tr_int(DEFAULT_MAX_DAYS)} gün ve 0 ile geçer); günlük "
        "kayıp bazı "
        f"{params.rules.daily_loss_basis or 'kaydedilmemiş'}: simülatör iki bazı tek modelle "
        "hesaplar (ADR-0002 madde 7: stratejiler reset'te pozisyon taşımaz, bakiye = özsermaye; "
        "reset'i aşan işlem varsa montecarlo raporu uyarır)"
    )
    constant_risk = risk_ok and not params.experimental_risk_steps
    if decision_runs and not link and not chain and constant_risk:
        # The whole risk chain in one sentence (user decision of 2026-11-19, finding 82),
        # said only when every link holds: each judged strategy has a decision run at the
        # planned risk, the list is theirs (`link` is empty) and the simulation used it,
        # on every trade (experimental risk steps shrink the risk in a drawdown).
        planned = risk_shown(inputs.planned_risk_pct)
        summary += (
            f"; risk zinciri: karar koşuları planlanan riskle ({planned}, config/risk.yaml) "
            "boyutlandırıldı, işlem listesi o koşulardan, simülasyon aynı riskte"
        )
    if not checks:
        checks.append(
            (
                "PASS",
                f"P(tüm fazlar) {_pct(p_pass)} ≥ {_pct(criteria.min_p_pass_all_phases)}, "
                f"P(ihlal) {_pct(mc.p_breach)} ≤ {_pct(criteria.max_p_breach)}",
            )
        )
    checks.append(("PASS", summary))
    return _part(_MC_SUBJECT, checks, (chosen, *decision_runs), values)


def _g6_part(inputs: GoNoGoInputs, sid: str) -> _Part:
    # A Master leg has no TradingView trade list of its own: its script's report is read.
    script = sid if inputs.script_ids is None else inputs.script_ids.get(sid, sid)
    reports = _newest_first(
        item
        for item in inputs.artifacts
        if isinstance(item.artifact, ParityArtifact) and item.artifact.strategy_id == script
    )
    if not reports:
        text = "parite raporu yok"
        if script != sid:
            text += f" ('{sid}' bir betik bacağıdır; parite betiğin, '{script}', raporundan okunur)"
        return _part(sid, [("NOT_EVALUABLE", text)], (), {"file": None})
    chosen = reports[0]
    parity = _parity(chosen)
    twins = [item for item in reports[1:] if _created(item.artifact) == _created(parity)]
    total = parity.matched + parity.mismatched + parity.python_only + parity.tradingview_only
    data = parity.data
    values: dict[str, JsonValue] = {
        "file": chosen.file_name,
        "match_rate": parity.match_rate,
        "passed": parity.passed,
        "matched": parity.matched,
        "python_only": parity.python_only,
        "tradingview_only": parity.tradingview_only,
        "compared": total,
        "parity_data": None if data is None else data.model_dump(mode="json"),
    }
    blocked = _unusable(inputs, chosen)
    if blocked is not None:
        return _part(sid, [blocked], (chosen,), values)
    minimum = inputs.criteria.min_parity_rate
    checks: list[tuple[Status, str]] = []
    if twins:
        names = ", ".join(item.file_name for item in twins)
        checks.append(("NOT_EVALUABLE", f"{_SAME_SECOND} parite raporu var: {names}"))
    checks += _parity_link_checks(inputs, sid, parity, total)
    rate = parity.match_rate
    if rate is not None and total > 0:
        # The smaller of stored and recomputed decides, as in G2/G3: the model's rate
        # tolerance must not lift a rate that is really below the floor onto it.
        rate = min(rate, parity.matched / total)
    if rate is None:
        checks.append(("FAIL", "eşleşme oranı yok (iki tarafta da işlem yok)"))
    elif rate < minimum:
        checks.append(("FAIL", f"eşleşme {_pct(rate)} < {_pct(minimum)}"))
    if not parity.passed:
        checks.append(("FAIL", "parite raporu geçmedi"))
    if not checks:
        assert rate is not None
        checks.append(
            (
                "PASS",
                f"eşleşme {_pct(rate)} ≥ {_pct(minimum)}, {tr_int(total)} işlem karşılaştırıldı",
            )
        )
    if script != sid:
        checks.append(
            (
                "PASS",
                f"'{sid}' bir betik bacağıdır: parite betiğin ('{script}') raporundan okunur; "
                "TradingView işlem listesi betik başınadır, oran betiğin bütün bacaklarının "
                "işlemlerini birlikte kapsar (bacak başına ayrı oran yok)",
            )
        )
    checks.append(("PASS", PARITY_RISK_SENTENCE))
    # TradingView's history is short, so the parity bars need not be the decision run's
    # file; both are shown so a reader sees what was compared with what.
    decision = _backtest_for(inputs, sid).chosen
    checks.append(
        (
            "PASS",
            f"parite verisi: {_data_text(data)}; karar koşusunun verisi: "
            + ("karar koşusu yok" if decision is None else _data_text(decision.artifact.data)),
        )
    )
    return _part(sid, checks, (chosen,), values)


def _parity_link_checks(
    inputs: GoNoGoInputs, sid: str, parity: ParityArtifact, total: int
) -> list[tuple[Status, str]]:
    """G6: enough trades, compared with today's configured parameters and instrument.

    A rate from a handful of trades, or from another parameter set or tick size, says
    nothing about the strategy the decision run tested (review E). The parameters are
    compared with `expected_parity_params`: a parity run keeps the script's own risk, so
    against the decision set (planned risk) every honest report would differ.
    """
    checks: list[tuple[Status, str]] = []
    min_trades = inputs.criteria.min_parity_trades
    if total < min_trades:
        checks.append(
            (
                "NOT_EVALUABLE",
                f"karşılaştırılan işlem {tr_int(total)} < {tr_int(min_trades)}: eşleşme oranı bu "
                "kadar az işlemle anlamlı değil",
            )
        )
    if parity.data is None:
        checks.append(
            ("NOT_EVALUABLE", "parite raporu karşılaştırılan barların veri bilgisini taşımıyor")
        )
    if parity.params is None:
        checks.append(
            (
                "NOT_EVALUABLE",
                "parite raporu Python tarafının parametrelerini kaydetmiyor (eski rapor): hangi "
                "stratejinin karşılaştırıldığı bilinmiyor; propbot parity'yi yeniden koşun",
            )
        )
    elif inputs.expected_parity_params is None:
        checks.append(("NOT_EVALUABLE", EXPECTED_PARITY_PARAMS_MISSING))
    elif (wanted := inputs.expected_parity_params.get(sid)) is None:
        checks.append(("NOT_EVALUABLE", f"'{sid}' için beklenen parite parametreleri verilmedi"))
    elif different := _param_differences(parity.params, wanted):
        checks.append(
            (
                "NOT_EVALUABLE",
                "parite raporunun parametreleri config/strategies.yaml'ın bugünkü değerleriyle "
                "aynı değil: " + different + " (parite betiğin kendi riskiyle koşulur)",
            )
        )
    info = parity.instrument
    if info is None:
        checks.append(
            (
                "NOT_EVALUABLE",
                "parite raporu enstrümanı kaydetmiyor (eski rapor); propbot parity'yi yeniden "
                "koşun",
            )
        )
    elif inputs.expected_instruments is None:
        checks.append(("NOT_EVALUABLE", EXPECTED_INSTRUMENTS_MISSING))
    elif (instrument := inputs.expected_instruments.get(info.symbol)) is None:
        checks.append(("NOT_EVALUABLE", f"'{info.symbol}' için beklenen enstrüman profili yok"))
    elif names := _instrument_differences(info, instrument):
        checks.append(
            (
                "NOT_EVALUABLE",
                "parite raporunun enstrümanı config/backtest.yaml'daki profille aynı değil: "
                + ", ".join(names),
            )
        )
    sizing = _sizing_differences(inputs, parity.initial_capital, parity.min_gap_seconds)
    checks += [("NOT_EVALUABLE", f"parite raporunun Python tarafı: {text}") for text in sizing]
    return checks


def _data_text(data: DataInfo | None) -> str:
    if data is None:
        return "veri bilgisi yok"
    return (
        f"{data.file_name} (sha256 {_short_sha(data.sha256)}, bar saat dilimi {data.bar_tz}, "
        f"{data.first_utc or '?'} → {data.last_utc or '?'})"
    )


# --- selection and checks -----------------------------------------------------------------


RunKind = Literal["decision", "comparison", "doubtful"]
_SAME_SECOND: Final = "aynı saniyede oluşturulmuş başka"


@dataclass(frozen=True, slots=True)
class _Selection:
    """Which backtest of a strategy G1-G4 judge.

    `chosen` is the newest decision run, unless a newer run that MAY be one cannot be
    confirmed: then that run is the `blocker` and nothing is chosen (an older run is
    never picked over it, it might be the one that decides).
    """

    chosen: LoadedArtifact | None
    runs: tuple[LoadedArtifact, ...]  # every run of the strategy, newest first
    blocker: LoadedArtifact | None
    why: str  # why the blocker is not confirmed as a decision run


def _backtest_for(inputs: GoNoGoInputs, sid: str) -> _Selection:
    """The decision run of `sid`: the newest run whose kind is not "comparison".

    Decision run (ADR-0008, SPEC §6.3): pessimistic fill model, costs on, cost factor
    exactly 1.0, sized at the planned risk and the recorded costs equal to the configured
    ones for its instrument. tv_ohlc (parity), cost-free, other-factor and other-risk
    ("--pine-risk") runs are comparison runs: they neither hide nor replace a decision
    run. A run that may be a decision run but cannot be confirmed (factor not recorded,
    no expected costs, costs differ, risk input unknown) blocks.
    """
    runs = _newest_first(
        item
        for item in inputs.artifacts
        if isinstance(item.artifact, BacktestArtifact) and item.artifact.strategy_id == sid
    )
    for item in runs:
        kind, why = _run_kind(inputs, _backtest(item))
        if kind == "comparison":
            continue
        # Two possible decision runs of one strategy in the same second: the file name would
        # pick one (a label chosen to sort first), so neither decides.
        twins = [
            other
            for other in runs
            if other is not item
            and _created(other.artifact) == _created(item.artifact)
            and _run_kind(inputs, _backtest(other))[0] != "comparison"
        ]
        if twins:
            names = ", ".join(other.file_name for other in twins)
            return _Selection(None, runs, item, f"{_SAME_SECOND} olası karar koşusu: {names}")
        if kind == "decision":
            return _Selection(item, runs, None, "")
        return _Selection(None, runs, item, why)
    return _Selection(None, runs, None, "")


def _run_kind(inputs: GoNoGoInputs, bt: BacktestArtifact) -> tuple[RunKind, str]:
    if bt.fill_model != "pessimistic":
        return "comparison", f"'{bt.fill_model}' dolum modeli"
    if bt.costs.cost_free:
        return "comparison", COST_FREE_TAG
    factor = bt.cost_factor
    if factor is not None and factor != 1.0:
        # Exactly 1.0: the configured costs times 1.0 are the configured costs bit for
        # bit, while 0.99 or 1.5 is a sensitivity run whatever its result.
        return "comparison", f"maliyet çarpanı {tr_number(factor, 4)} (1,0 değil)"
    if inputs.expected_oos_start_utc is None:
        return "doubtful", EXPECTED_OOS_MISSING
    if _stamp(bt.oos_start_utc) != _stamp(inputs.expected_oos_start_utc):
        # A boundary picked after seeing results would let G2/G3 be judged on a chosen
        # split: a deliberate what-if like --cost-factor, so a comparison run. It neither
        # decides nor hides an older run on the configured boundary.
        return "comparison", (
            f"OOS başlangıcı {bt.oos_start_utc} (config/backtest.yaml: "
            f"{inputs.expected_oos_start_utc})"
        )
    risk, _ = _recorded_risk(inputs, bt)
    if risk is not None and risk != inputs.planned_risk_pct:
        # Sized at another risk the run made other trades (floored sizes, skipped trades,
        # the scripts' percent breakers): a deliberate what-if like --cost-factor, so a
        # comparison run. It neither decides nor hides an older run at the planned risk.
        # An unknown risk is not decided here: it makes the run unconfirmable below.
        return "comparison", (
            f"işlem başı risk {risk_shown(risk)} (planlanan risk "
            f"{risk_shown(inputs.planned_risk_pct)}, config/risk.yaml)"
        )
    if factor is None:
        return "doubtful", (
            "maliyet çarpanı kaydedilmemiş (eski ya da elle yazılmış rapor): karar koşusu "
            "olduğu doğrulanamaz; backtest'i yeniden koşun"
        )
    if inputs.expected_costs is None:
        return "doubtful", EXPECTED_COSTS_MISSING
    symbol = bt.instrument.symbol
    expected = inputs.expected_costs.get(symbol)
    if expected is None:
        return "doubtful", f"'{symbol}' enstrümanı için beklenen maliyet profili yok"
    differences = _cost_differences(bt.costs, expected)
    if differences:
        return "doubtful", (
            "kayıtlı maliyetler config/backtest.yaml'daki maliyetlerle aynı değil: "
            + ", ".join(differences)
        )
    if inputs.expected_instruments is None:
        return "doubtful", EXPECTED_INSTRUMENTS_MISSING
    instrument = inputs.expected_instruments.get(symbol)
    if instrument is None:
        return "doubtful", f"'{symbol}' için beklenen enstrüman profili yok"
    different = _instrument_differences(bt.instrument, instrument)
    if different:
        return "doubtful", (
            "kayıtlı enstrüman config/backtest.yaml'daki profille aynı değil: "
            + ", ".join(different)
        )
    sizing = _sizing_differences(inputs, bt.initial_capital, bt.min_gap_seconds)
    if sizing:
        return "doubtful", "; ".join(sizing)
    return _params_kind(inputs, bt)


def _sizing_differences(inputs: GoNoGoInputs, capital: float | None, gap: int | None) -> list[str]:
    """How a run's capital and session gap differ from today's venue and config.

    Neither is a cost, yet both change the trades: sizes are whole contracts and a size
    below min_qty is skipped (so R is not size-free), and the session gap moves Pine's
    session.islastbar, which gates entries and forces exits (gate attack K, L).
    """
    found: list[str] = []
    balance = float(inputs.venue.initial_balance)
    if capital is None:
        found.append("başlangıç sermayesi kaydedilmemiş (eski rapor)")
    elif capital != balance:
        found.append(
            f"başlangıç sermayesi {tr_number(capital, 2)}, venue profilinin initial_balance "
            f"değeri {tr_number(balance, 2)}: boyutlar (tam kontrat, min_qty) başka, işlemler "
            "başka"
        )
    if inputs.expected_min_gap_seconds is None:
        found.append(EXPECTED_SESSION_MISSING)
    elif gap is None:
        found.append(
            "seans sonu boşluğu kaydedilmemiş (eski rapor): seansların nerede bittiği "
            "bilinmiyor; yeniden koşun"
        )
    elif gap != inputs.expected_min_gap_seconds:
        found.append(
            f"seans sonu boşluğu {tr_number(gap / 60, 2)} dk, config/backtest.yaml "
            f"session.min_gap_minutes {tr_number(inputs.expected_min_gap_seconds / 60, 2)} dk: "
            "seans sonları (session.islastbar) başka, işlemler başka"
        )
    return found


def _params_kind(inputs: GoNoGoInputs, bt: BacktestArtifact) -> tuple[RunKind, str]:
    """A run is a decision run only with the parameters config/strategies.yaml gives today
    and the planned risk in its risk input."""
    risk, why = _recorded_risk(inputs, bt)
    if risk is None:
        # Unknown is never "at the planned risk" (a known other risk was a comparison run).
        return "doubtful", why
    if inputs.expected_params is None:
        return "doubtful", EXPECTED_PARAMS_MISSING
    wanted = inputs.expected_params.get(bt.strategy_id)
    if wanted is None:
        return "doubtful", f"'{bt.strategy_id}' için beklenen parametreler verilmedi"
    different = _param_differences(bt.params, wanted)
    if different:
        return "doubtful", (
            "kayıtlı parametreler config/strategies.yaml'ın bugünkü değerleriyle aynı değil: "
            + different
        )
    return "decision", ""


def _recorded_risk(inputs: GoNoGoInputs, bt: BacktestArtifact) -> tuple[float | None, str]:
    """The per-trade risk (percent) a backtest recorded; None and the reason when unknown."""
    if inputs.risk_params is None:
        return None, RISK_PARAMS_MISSING
    name = inputs.risk_params.get(bt.strategy_id)
    if name is None:
        return None, f"'{bt.strategy_id}' için işlem başı risk girdisinin adı verilmedi"
    risk = _finite_number(bt.params.get(name))
    if risk is None:
        return None, (
            f"kayıtlı parametrelerde işlem başı risk ({_clip(name, 64)}) yok ya da sayı değil: "
            "koşunun planlanan riskle boyutlandırıldığı doğrulanamaz"
        )
    return risk, ""


def _finite_number(value: Scalar) -> float | None:
    """A recorded JSON number as a finite float; None for anything else.

    A JSON integer has no upper bound, a float has: an integer beyond it (a damaged or
    hand-edited file) is no value a run could have been sized at. It is unknown, like a
    missing one, and never an OverflowError that ends the report in a traceback.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def decision_risk_checks(
    inputs: GoNoGoInputs, runs: Sequence[LoadedArtifact]
) -> list[tuple[Status, str]]:
    """G4: every decision run behind the trade list was sized at the planned risk.

    `_run_kind` already keeps a run at another risk from being chosen; this states the
    same fact where the chain is judged (decision runs -> trade list -> simulation, all at
    `planned_risk_pct`), so the chain does not rest on a check made elsewhere. Exact
    equality: the runner writes the planned value itself into the risk input.
    """
    checks: list[tuple[Status, str]] = []
    for item in runs:
        risk, why = _recorded_risk(inputs, _backtest(item))
        if risk is None:
            checks.append(("NOT_EVALUABLE", f"{item.file_name}: {why}"))
        elif risk != inputs.planned_risk_pct:
            checks.append(
                (
                    "NOT_EVALUABLE",
                    f"{item.file_name}: karar koşusunun işlem başı riski {risk_shown(risk)}, "
                    f"planlanan risk {risk_shown(inputs.planned_risk_pct)}: işlem listesi "
                    "planlanan riskle boyutlandırılmamış; backtest'i config/risk.yaml riskiyle "
                    "yeniden koşun",
                )
            )
    return checks


def _row_digests(bt: BacktestArtifact) -> dict[str, str] | None:
    """Row strategy id -> digest of the run's trade-list rows carrying it; None = unknown."""
    if bt.trades_digests is not None:
        return dict(bt.trades_digests)
    if bt.trades_digest is None:
        return None
    # Written before the per-id digests existed. Right for a run whose rows all carry its
    # own id (a single-strategy port, a Master leg); a combined view's rows carry its
    # legs' ids, so its one digest matches no row group and G4 stays NOT_EVALUABLE.
    return {bt.strategy_id: bt.trades_digest}


def _param_differences(recorded: Mapping[str, Scalar], wanted: Mapping[str, Scalar]) -> str:
    """The differing parameter names as one phrase; "" when they are equal."""
    different = sorted(
        name
        for name in set(recorded) | set(wanted)
        if name not in recorded
        or name not in wanted
        or not _same_scalar(recorded[name], wanted[name])
    )
    if not different:
        return ""
    return ", ".join(_clip(name, 64) for name in different[:10]) + (
        f" (+{len(different) - 10} daha)" if len(different) > 10 else ""
    )


def _instrument_differences(recorded: InstrumentInfo, wanted: Instrument) -> list[str]:
    # Exact equality, as for the costs: slippage in ticks and commission per unit become R
    # through tick size and point value, so a smaller tick is a cheaper run. The size grid
    # too: a larger min_qty skips the wide-stop trades (gate attack M).
    return [
        name if value is not None else f"{name} (kaydedilmemiş, eski rapor)"
        for name, value, expected in (
            ("kind", recorded.kind, wanted.kind.value),
            ("tick_size", recorded.tick_size, wanted.tick_size),
            ("point_value", recorded.point_value, wanted.point_value),
            ("qty_step", recorded.qty_step, wanted.qty_step),
            ("min_qty", recorded.min_qty, wanted.min_qty),
        )
        if value != expected
    ]


def _stamp(text: str) -> dt.datetime | str:
    """A UTC stamp as a moment, so "…T00:00:00Z" and "…T00:00:00.000Z" are one boundary."""
    try:
        return dt.datetime.fromisoformat(text).astimezone(dt.UTC)
    except ValueError:
        return text


def _same_scalar(recorded: Scalar, wanted: Scalar) -> bool:
    """Exact equality of JSON scalars, type included: 1, 1.0 and True are three values."""
    return type(recorded) is type(wanted) and recorded == wanted


def _cost_differences(recorded: CostInfo, expected: CostModel) -> list[str]:
    """Names of the recorded cost fields that differ from the configured ones.

    Exact float equality on purpose: both sides are the same YAML numbers, and JSON keeps
    a float bit for bit (shortest repr round-trips), so a real decision run matches
    exactly; any tolerance would let a slightly cheaper run count.
    """
    different = [
        name
        for name in ("commission_per_unit_side", "slippage_ticks", "spread_points")
        if getattr(recorded, name) != getattr(expected, name)
    ]
    windows = tuple(
        (w.start_minute, w.end_minute, w.spread_points) for w in recorded.spread_windows
    )
    wanted = tuple((w.start_minute, w.end_minute, w.spread_points) for w in expected.spread_windows)
    if windows != wanted:
        different.append("spread_windows")
    if recorded.spread_tz is None:
        different.append("spread_tz (kaydedilmemiş, eski rapor)")
    elif recorded.spread_tz != expected.spread_tz:
        # The same window minutes in another zone are other hours (gate attack L).
        different.append("spread_tz")
    return different


def _run_names(inputs: GoNoGoInputs, runs: Sequence[LoadedArtifact]) -> str:
    return ", ".join(f"{item.file_name} ({_run_kind(inputs, _backtest(item))[1]})" for item in runs)


def _blocked_part(inputs: GoNoGoInputs, sid: str, selection: _Selection) -> _Part:
    blocker = selection.blocker
    assert blocker is not None
    checks: list[tuple[Status, str]] = [
        (
            "NOT_EVALUABLE",
            f"en yeni olası karar koşusu {blocker.file_name}: {selection.why} (daha eski bir "
            "koşuya geçilmez)",
        )
    ]
    unusable = _unusable(inputs, blocker)
    if unusable is not None and unusable[0] == "FAIL":
        checks.append(unusable)
    return _part(
        sid,
        checks,
        (blocker,),
        {"backtest_file": blocker.file_name, "cost_factor": _backtest(blocker).cost_factor},
    )


def _no_decision_run(sid: str, runs: Sequence[LoadedArtifact]) -> _Part:
    names = ", ".join(item.file_name for item in runs)
    return _part(
        sid,
        [
            (
                "NOT_EVALUABLE",
                "config maliyetleriyle (çarpan 1,0), config OOS başlangıcıyla ve planlanan "
                f"riskle 'pessimistic' karar koşusu yok; yalnızca karşılaştırma koşusu: {names}",
            )
        ],
        (),
        {"backtest_file": None},
    )


def _provenance_checks(
    inputs: GoNoGoInputs, mc: MonteCarloArtifact
) -> tuple[list[tuple[Status, str]], tuple[LoadedArtifact, ...]]:
    """G4: the trade list must come from the judged strategies' decision runs.

    Returns the checks and the decision runs they were compared with.
    """
    provenance = mc.provenance
    if provenance is None:
        return [("NOT_EVALUABLE", UNKNOWN_TRADES_SOURCE)], ()
    checks: list[tuple[Status, str]] = []
    if provenance.cost_free:
        checks.append(("NOT_EVALUABLE", f"işlem listesi maliyetsiz bir koşudan ({COST_FREE_TAG})"))
    if provenance.fill_model != "pessimistic":
        checks.append(
            (
                "NOT_EVALUABLE",
                f"işlem listesi '{provenance.fill_model}' dolum modeliyle üretilmiş; "
                "'pessimistic' olmalı",
            )
        )
    if provenance.cost_factor != 1.0:
        checks.append(
            (
                "NOT_EVALUABLE",
                f"işlem listesi maliyet çarpanı {tr_number(provenance.cost_factor, 4)} ile "
                "üretilmiş; 1,0 olmalı",
            )
        )
    marked = [sid for sid in provenance.strategy_ids if sid.endswith(PINE_RISK_SUFFIX)]
    if marked:
        # Written by a run at the script's own risk (runner.trade_list_id). Refused for
        # the mark, before any comparison of rows: when no trade is floored away or
        # skipped the rows equal the decision run's, and the list would pass as its own.
        checks.append(
            (
                "NOT_EVALUABLE",
                "işlem listesi betiğin kendi riskiyle (--pine-risk) yapılmış koşudan ("
                + ", ".join(_clip(sid, 64) for sid in marked)
                + "): planlanan hesabın işlemleri değildir; listeyi --pine-risk olmadan, "
                "karar koşusundan üretin",
            )
        )
    strategies = list(dict.fromkeys(inputs.strategy_ids))
    if not strategies:
        checks.append(("NOT_EVALUABLE", "değerlendirilecek strateji listesi boş"))
        return checks, ()
    runs: list[LoadedArtifact] = []
    without: list[str] = []
    # The ids the list's rows must carry: a strategy's own id, or for a portfolio judged
    # by its combined id the leg ids its decision run says its rows carry (finding 72).
    row_ids: set[str] = set()
    for sid in strategies:
        chosen = _backtest_for(inputs, sid).chosen
        digests = None if chosen is None else _row_digests(_backtest(chosen))
        row_ids |= set(digests) if digests else {sid}
        if chosen is None:
            without.append(sid)
        else:
            runs.append(chosen)
    missing = sorted(row_ids - set(provenance.strategy_ids))
    extra = sorted(set(provenance.strategy_ids) - row_ids)
    # Which ids a strategy's rows carry is known from its decision run. Without one the
    # fallback above (its own id) is a guess, wrong for a portfolio judged by its combined
    # id: comparing would report the list's legs as "not judged". The check below names
    # the real cause instead.
    if (missing or extra) and not without:
        parts = []
        if missing:
            parts.append("listede olmayan: " + ", ".join(missing))
        if extra:
            parts.append("değerlendirilmeyen: " + ", ".join(extra))
        checks.append(
            (
                "NOT_EVALUABLE",
                "işlem listesinin stratejileri değerlendirilen stratejilerle aynı değil ("
                + "; ".join(parts)
                + ")",
            )
        )
    if without:
        checks.append(
            (
                "NOT_EVALUABLE",
                "karar koşusu olmayan strateji: "
                + ", ".join(without)
                + "; işlem listesi bir karar koşusuna bağlanamaz",
            )
        )
    if runs and not without:
        # Every complete trade of the decision runs is one row of their list: a list with
        # rows removed (the losers, say) keeps its line but not its count.
        expected_n = sum(_backtest(item).stats_all.n for item in runs)
        if mc.inputs.n_trades != expected_n:
            checks.append(
                (
                    "NOT_EVALUABLE",
                    f"simülasyonun işlem sayısı {tr_int(mc.inputs.n_trades)}, karar koşularının "
                    f"tamamlanmış işlem sayısı {tr_int(expected_n)}: liste eksik ya da fazla "
                    "(elle düzenlenmiş olabilir)",
                )
            )
        # The count alone keeps a list whose R values were all rewritten: the sums tie
        # every row's value to the decision runs.
        checks += _sum_checks(mc, runs)
        # The sums keep a list whose times were moved: the digests tie every row.
        checks += _digest_checks(mc, runs)
    if not provenance.data_sha256:
        checks.append(("NOT_EVALUABLE", "işlem listesinin veri sha256'sı yok"))
    else:
        other = [
            f"{item.file_name} ({_short_sha(_data_sha(item))})"
            for item in runs
            if _data_sha(item) != provenance.data_sha256
        ]
        if other:
            checks.append(
                (
                    "NOT_EVALUABLE",
                    f"işlem listesinin verisi ({provenance.data_file}, sha256 "
                    f"{_short_sha(provenance.data_sha256)}) karar koşularının verisiyle aynı "
                    "değil: " + ", ".join(other),
                )
            )
    made = dt.datetime.fromisoformat(provenance.created_utc).astimezone(dt.UTC)
    if runs:
        newest = max(runs, key=lambda item: _created(item.artifact))
        if made < _created(newest.artifact):
            checks.append(
                (
                    "NOT_EVALUABLE",
                    f"işlem listesi ({provenance.created_utc}) en yeni karar koşusundan "
                    f"({newest.file_name}) eski: listeyi o koşudan yeniden üretin",
                )
            )
    if made > _created(mc):
        checks.append(
            (
                "NOT_EVALUABLE",
                f"işlem listesi ({provenance.created_utc}) Monte Carlo koşusundan sonra "
                "üretilmiş: köken zamanı tutarsız",
            )
        )
    for item in runs:
        unusable = _unusable(inputs, item)
        if unusable is not None:
            checks.append(unusable)
    return checks, tuple(runs)


def _sum_checks(mc: MonteCarloArtifact, runs: Sequence[LoadedArtifact]) -> list[tuple[Status, str]]:
    """G4: the list's R and mae_r sums must equal the decision runs' (review F)."""
    checks: list[tuple[Status, str]] = []
    totals = [_backtest(item).stats_all.total_r for item in runs]
    if mc.inputs.sum_r is None:
        checks.append(
            (
                "NOT_EVALUABLE",
                "simülasyon işlem listesinin R toplamını kaydetmiyor (eski rapor): listenin karar "
                "koşularından değiştirilmeden geldiği doğrulanamaz; Monte Carlo'yu yeniden koşun",
            )
        )
    elif not _same_sum(mc.inputs.sum_r, totals):
        checks.append(
            (
                "NOT_EVALUABLE",
                f"işlem listesinin R toplamı {tr_number(mc.inputs.sum_r, 6)}, karar koşularının "
                f"{_sum_text(totals)}: R değerleri değiştirilmiş (elle düzenlenmiş olabilir)",
            )
        )
    maes = [_backtest(item).mae_total_r for item in runs]
    known = [value for value in maes if value is not None]
    if len(known) != len(maes):
        checks.append(
            (
                "NOT_EVALUABLE",
                "karar koşusu mae_r toplamını kaydetmiyor (eski rapor): listenin yüzen zararları "
                "doğrulanamaz; backtest'i yeniden koşun",
            )
        )
    elif mc.inputs.sum_mae_r is None:
        checks.append(
            (
                "NOT_EVALUABLE",
                "işlem listesinde mae_r yok: karar koşularının ölçülmüş yüzen zararları "
                "listeden çıkarılmış",
            )
        )
    elif not _same_sum(mc.inputs.sum_mae_r, known):
        checks.append(
            (
                "NOT_EVALUABLE",
                f"işlem listesinin mae_r toplamı {tr_number(mc.inputs.sum_mae_r, 6)}, karar "
                f"koşularının {_sum_text(known)}: yüzen zararlar değiştirilmiş",
            )
        )
    return checks


def _digest_checks(
    mc: MonteCarloArtifact, runs: Sequence[LoadedArtifact]
) -> list[tuple[Status, str]]:
    """G4: every row of the list (times included) must be a row of its decision run.

    Count and sums do not see a moved time, yet the times decide which trades share a
    prop day and so the daily-loss breaches (gate attack P).
    """
    digests = mc.inputs.trades_digests
    if digests is None:
        return [
            (
                "NOT_EVALUABLE",
                "simülasyon işlem listesinin satır parmak izini kaydetmiyor (eski rapor): "
                "satırların (zamanlar dahil) karar koşularından değiştirilmeden geldiği "
                "doğrulanamaz; Monte Carlo'yu yeniden koşun",
            )
        ]
    groups = [(item, _row_digests(_backtest(item))) for item in runs]
    old = [item.file_name for item, rows in groups if rows is None]
    if old:
        return [
            (
                "NOT_EVALUABLE",
                "karar koşusu işlem satırlarının parmak izini kaydetmiyor (eski rapor): "
                + ", ".join(old)
                + "; backtest'i yeniden koşun",
            )
        ]
    wanted: dict[str, str] = {}
    twice: set[str] = set()
    for _, rows in groups:
        assert rows is not None
        twice |= set(rows) & set(wanted)
        wanted.update(rows)
    if twice:
        # A portfolio's combined run and one of its legs both judged: the leg's rows are
        # in both, so count, sums and digests would compare the list with itself twice.
        return [
            (
                "NOT_EVALUABLE",
                "aynı işlemler birden fazla karar koşusunda: "
                + ", ".join(_clip(sid or "(stratejisiz)", 64) for sid in sorted(twice))
                + "; bir portföy ya birleşik kimliğiyle ya da bacak kimlikleriyle "
                "değerlendirilir, ikisi birlikte işlemleri iki kez sayar",
            )
        ]
    different = sorted(
        sid for sid in set(wanted) | set(digests) if digests.get(sid) != wanted.get(sid)
    )
    if not different:
        return []
    names = ", ".join(_clip(sid or "(stratejisiz)", 64) for sid in different)
    return [
        (
            "NOT_EVALUABLE",
            "işlem listesinin satırları (giriş/çıkış zamanı, R, mae_r) karar koşusununkilerle "
            f"aynı değil: {names}; liste elle düzenlenmiş olabilir, backtest --trades-out ile "
            "yeniden üretin",
        )
    ]


def _same_sum(recorded: float, parts: Sequence[float]) -> bool:
    """`recorded` equals the sum of `parts` up to the rounding of the sums themselves.

    analytics.write_trades_csv writes R and mae_r with repr() (the shortest string that
    reads back as the same float), so the CSV adds no rounding: the "n times half a unit
    in the last written place" of a fixed-decimal file is zero here. What is left is the
    rounding of the sums: each recorded total is a math.fsum (correctly rounded, at most
    half an ulp off the exact sum) and adding the per-run totals rounds once more, so the
    two sides differ by at most a few ulps of the magnitudes involved. Twice the machine
    epsilon times those magnitudes covers that with room and nothing more: one rewritten
    row moves the sum by far more.
    """
    try:
        expected = math.fsum(parts)
        scale = math.fsum(abs(part) for part in parts) + abs(recorded)
    except OverflowError:
        # Totals near the float limit (hand-edited files) add up to no float at all:
        # not the list's sum, and not an error that ends the report.
        return False
    return abs(recorded - expected) <= 2.0 * _EPS * scale + 1e-300


def _sum_text(parts: Sequence[float]) -> str:
    """The sum of the decision runs' totals for a refusal; words when it is no float."""
    try:
        return tr_number(math.fsum(parts), 6)
    except OverflowError:
        return "toplamı sayı sınırının dışında"


def _data_sha(item: LoadedArtifact) -> str:
    data = item.artifact.data
    return "" if data is None else data.sha256


def _short_sha(sha: str) -> str:
    return f"{sha[:12]}…" if sha else "yok"


def _validation_for(inputs: GoNoGoInputs, used: DataInfo) -> LoadedArtifact | str:
    """The newest validation of exactly these bars, or the reason there is none.

    The same content (sha256) is not enough: an MT5 file validated as NY+7 and backtested
    as UTC is clean seven hours away from the bars the backtest used. So the validation
    must also have read the file the same way (bar time zone, bar length, first and last
    bar). A reason starting with "!" is a FAIL (a validation of another content of the
    same file name, or of the same content read otherwise), otherwise NOT_EVALUABLE.
    """
    file_name, sha256 = used.file_name, used.sha256
    reports = _newest_first(
        item for item in inputs.artifacts if isinstance(item.artifact, DataValidationArtifact)
    )
    same = [item for item in reports if (data := item.artifact.data) and data.sha256 == sha256]
    # A validation of another reading is about other bars: it neither decides nor hides one.
    matching = [item for item in same if not _reading_differences(_data_of(item), used)]
    if matching:
        first = matching[0]
        twins = [
            item for item in matching[1:] if _created(item.artifact) == _created(first.artifact)
        ]
        if twins:
            # The file name would pick a clean one over an unclean one: neither decides.
            names = ", ".join(item.file_name for item in (first, *twins))
            return f"{file_name} için aynı saniyede birden fazla doğrulama raporu: {names}"
        return first
    if same:
        newest = same[0]
        differences = "; ".join(_reading_differences(_data_of(newest), used))
        return (
            f"!{file_name} için doğrulama raporu {newest.file_name} aynı dosyayı başka biçimde "
            f"okumuş ({differences}): doğrulanan barlar backtest'in barları değil; veriyi "
            "backtest'teki ayarlarla (--bar-tz, --tf) yeniden doğrulayın"
        )
    other = [
        item
        for item in reports
        if (data := item.artifact.data) is not None and data.file_name == file_name
    ]
    if other:
        names = ", ".join(item.file_name for item in other)
        return (
            f"!{file_name} için doğrulama raporu başka bir içeriğe (sha256 farklı) ait: {names}; "
            "veri doğrulamadan sonra değişmiş"
        )
    return f"{file_name} için veri doğrulama raporu yok"


def _data_of(item: LoadedArtifact) -> DataInfo:
    data = item.artifact.data
    assert data is not None
    return data


def _reading_differences(validated: DataInfo, used: DataInfo) -> list[str]:
    """How the validation's reading of a file differs from the backtest's (both named)."""
    return [
        f"{label}: doğrulamada {_shown(a)}, backtest'te {_shown(b)}"
        for label, a, b in (
            ("bar saat dilimi", validated.bar_tz, used.bar_tz),
            ("bar uzunluğu (sn)", validated.tf_seconds, used.tf_seconds),
            ("ilk bar (UTC)", validated.first_utc, used.first_utc),
            ("son bar (UTC)", validated.last_utc, used.last_utc),
        )
        if a != b
    ]


def _shown(value: str | int | None) -> str:
    if value is None:
        return "yok"
    return tr_int(value) if isinstance(value, int) else value


def _unusable(inputs: GoNoGoInputs, item: LoadedArtifact) -> tuple[Status, str] | None:
    """Synthetic -> FAIL; dated after the report or too old -> NOT_EVALUABLE."""
    artifact = item.artifact
    if artifact.synthetic or (artifact.data is not None and artifact.data.synthetic):
        return "FAIL", f"{SYNTHETIC_TAG}: {item.file_name} sentetik veriyle üretildi (ADR-0007)"
    created = _created(artifact)
    if created > inputs.created.astimezone(dt.UTC):
        return "NOT_EVALUABLE", f"{item.file_name} rapordan ileri tarihli ({artifact.created_utc})"
    age = (inputs.today - created.date()).days
    limit = inputs.criteria.max_artifact_age_days
    if age > limit:
        return (
            "NOT_EVALUABLE",
            f"{item.file_name} {age} gün eski (sınır {limit} gün): yeniden üretin",
        )
    return None


def _rules_mismatch(mc: MonteCarloArtifact, venue: VenueProfile) -> list[str]:
    """Every way the simulated rules differ from today's profile (empty when none).

    Each rule the simulator applies is compared, not only the numbers: a profile edited
    to a trailing loss or another reset after the run makes the run describe other rules.
    """
    rules = mc.inputs.rules
    found: list[str] = []
    targets = [venue.profit_target_pct.phase1]
    if venue.profit_target_pct.phase2 is not None:
        targets.append(venue.profit_target_pct.phase2)
    same = (
        len(rules.targets_pct) == len(targets)
        and all(_close(a, b) for a, b in zip(rules.targets_pct, targets, strict=True))
        and _close(rules.daily_loss_limit_pct, venue.daily_loss_limit_pct)
        and _close(rules.max_loss_pct, venue.max_loss_pct)
        and venue.min_trading_days is not None
        and rules.min_trading_days == venue.min_trading_days
    )
    if not same:
        found.append(
            "simülasyonun kuralları venue profiliyle uyuşmuyor (hedefler, günlük kayıp, toplam "
            "kayıp ya da minimum işlem günü)"
        )
    elif rules.min_trading_days_assumed:
        found.append(
            "minimum işlem günü varsayılarak simüle edilmiş (profilde doğrulanmış değer yok)"
        )
    missing = [
        name
        for name, value in (
            ("max_loss_type", rules.max_loss_type),
            ("daily_loss_basis", rules.daily_loss_basis),
            ("daily_reset", rules.daily_reset_tz and rules.daily_reset_time),
        )
        if value is None
    ]
    if missing:
        found.append(
            "simülasyon şu venue kurallarını kaydetmiyor (eski rapor): "
            + ", ".join(missing)
            + "; profil o zamandan beri değişmiş olabilir, Monte Carlo'yu yeniden koşun"
        )
    else:
        if rules.max_loss_type != venue.max_loss_type.value:
            found.append(
                f"max_loss_type: simülasyonda {rules.max_loss_type}, profilde "
                f"{venue.max_loss_type.value}"
            )
        if rules.daily_loss_basis != venue.daily_loss_basis.value:
            found.append(
                f"daily_loss_basis: simülasyonda {rules.daily_loss_basis}, profilde "
                f"{venue.daily_loss_basis.value}"
            )
        reset = venue.daily_reset
        if (rules.daily_reset_tz, rules.daily_reset_time) != (reset.tz, reset.time):
            found.append(
                f"daily_reset: simülasyonda {rules.daily_reset_time} {rules.daily_reset_tz}, "
                f"profilde {reset.time} {reset.tz}"
            )
    if venue.max_loss_type is not MaxLossType.STATIC:
        # The simulator refuses this rule (ChallengeRules.from_venue): an artifact that
        # claims it was not made by it, and no run describes the profile's rule.
        found.append(
            f"profilde max_loss_type '{venue.max_loss_type.value}': simülatör yalnızca 'static' "
            "toplam kaybı modeller; bu kuralla G4 değerlendirilemez"
        )
    return found


def decision_paths(criteria: GoNoGoCriteria) -> int:
    """The path count G4 judges on: the simulator's default, or a stricter configured one."""
    return max(DEFAULT_PATHS, criteria.min_mc_paths)


def _chosen_knobs(mc: MonteCarloArtifact, criteria: GoNoGoCriteria) -> list[str]:
    """The simulation choices that differ from the reproducible run (gate attack N).

    With 5,000 paths one run's P(pass) has a standard error near 0.7 points, so the best
    of 40 seeds (or path counts: each count draws other days) lies about 1.2 points above
    the typical one, enough to lift a strategy over the floor by picking. The horizon and
    the skip probability move both probabilities (a short horizon leaves breaches
    undecided). Exact values, no tolerance: the CLI records them as given.
    """
    params = mc.inputs
    found: list[str] = []
    if params.seed != DEFAULT_SEED:
        found.append(f"tohum {params.seed}, varsayılan {DEFAULT_SEED}")
    wanted = decision_paths(criteria)
    if params.n_paths != wanted and params.n_paths >= criteria.min_mc_paths:
        # A count below the minimum has its own sentence.
        found.append(f"yol sayısı {tr_int(params.n_paths)}, karar değeri {tr_int(wanted)}")
    if params.max_days != DEFAULT_MAX_DAYS:
        found.append(
            f"ufuk {tr_int(params.max_days)} piyasa günü, varsayılan {tr_int(DEFAULT_MAX_DAYS)}"
        )
    if params.skip_prob != 0.0:
        found.append(f"işlem atlama olasılığı {tr_number(params.skip_prob, 4)}, karar değeri 0")
    return found


def _probabilities_inconsistent(mc: MonteCarloArtifact) -> str | None:
    """A hand-edited probability no longer adds up with the others of the same run."""
    phases = mc.p_pass_phase
    if any(later > earlier for earlier, later in itertools.pairwise(phases)):
        return "olasılıklar tutarsız: tüm fazları geçme olasılığı ilk fazınkinden büyük"
    total = phases[-1] + mc.p_breach + mc.p_undecided
    if abs(total - 1.0) > _PROBABILITY_TOL:
        return (
            "olasılıklar tutarsız: P(tüm fazlar) + P(ihlal) + P(sonuçlanmadı) = "
            f"{tr_number(total, 4)} ≠ 1"
        )
    shares = math.fsum(share.probability for share in mc.breach_shares)
    if abs(shares - mc.p_breach) > _PROBABILITY_TOL:
        return "olasılıklar tutarsız: ihlal payları toplamı P(ihlal) ile uyuşmuyor"
    return None


def _problem_parts(inputs: GoNoGoInputs, gate_id: str) -> list[_Part]:
    parts = []
    if inputs.artifacts_truncated is not False and gate_id in _ALL_DATA_GATES:
        # Not every report file was read (or the caller did not say): an unread one may be
        # the run that decides.
        text = (
            TRUNCATION_UNKNOWN
            if inputs.artifacts_truncated is None
            else f"rapor dizininde {tr_int(MAX_EXAMINED_FILES)} dosyadan fazlası var; yalnızca "
            f"en yeni {tr_int(MAX_EXAMINED_FILES)} dosya incelendi, okunmayan bir rapor kararı "
            "değiştirebilir: eski raporları reports/ dışına taşıyın"
        )
        parts.append(_Part("rapor dizini", "NOT_EVALUABLE", (text,), (), {"problem": text}))
    for problem in inputs.problems:
        kind = next((k for k in KINDS if problem.file_name.startswith(f"{k}_")), None)
        gates = _ALL_DATA_GATES if kind is None else _PROBLEM_GATES.get(kind, ())
        if gate_id in gates:
            parts.append(
                _Part(
                    problem.file_name,
                    "NOT_EVALUABLE",
                    (f"okunamayan rapor dosyası {problem.file_name}: {problem.message}",),
                    (),
                    {"problem": f"{problem.file_name}: {problem.message}"},
                )
            )
    return parts


def _check_inputs(inputs: GoNoGoInputs) -> None:
    if inputs.created.utcoffset() is None:
        raise GoNoGoError("rapor zamanı saat dilimi içermeli")
    if inputs.created.astimezone(dt.UTC).date() != inputs.today:
        raise GoNoGoError(
            f"bugünün tarihi ({inputs.today}) rapor zamanının UTC tarihiyle "
            f"({inputs.created.astimezone(dt.UTC).date()}) aynı olmalı"
        )
    if not (math.isfinite(inputs.planned_risk_pct) and inputs.planned_risk_pct > 0.0):
        raise GoNoGoError("planlanan işlem riski pozitif bir sayı olmalı")
    if inputs.planned_risk_pct > MAX_RISK_PER_TRADE_PCT:
        # The config loader refuses it first; a caller that built the inputs itself must
        # not get a report whose decision runs are sized above the ceiling.
        raise GoNoGoError(
            f"planlanan işlem riski {risk_shown(inputs.planned_risk_pct)} kod tavanını "
            f"({risk_shown(MAX_RISK_PER_TRADE_PCT)}, SPEC §8.1) aşıyor"
        )
    if not inputs.venue_name:
        raise GoNoGoError("venue adı boş olamaz")
    # Evidence and the input list name artifacts by file name, and gates de-duplicate by
    # it: a name that does not follow from the content, or two artifacts under one name,
    # would attribute evidence to the wrong file or drop one (and its synthetic flag).
    seen: set[str] = set()
    for item in inputs.artifacts:
        expected = artifact_file_name(item.artifact)
        if item.file_name != expected:
            raise GoNoGoError(
                f"{_line(item.file_name)}: dosya adı içerikle uyuşmuyor (beklenen {expected})"
            )
        if item.file_name in seen:
            raise GoNoGoError(
                f"{item.file_name}: aynı adla birden fazla rapor verildi; hangisinin "
                "kullanılacağı belirsiz"
            )
        seen.add(item.file_name)


# --- gate assembly ------------------------------------------------------------------------


def _per_strategy_gate(
    gate_id: str,
    inputs: GoNoGoInputs,
    judge: Callable[[str], _Part],
    criteria: dict[str, JsonValue],
    *,
    manual: tuple[str, ...] = (),
) -> GateResult:
    strategies = list(dict.fromkeys(inputs.strategy_ids))
    if not strategies:
        parts = [
            _Part(
                "stratejiler",
                "NOT_EVALUABLE",
                ("değerlendirilecek strateji listesi boş",),
                (),
                {},
            )
        ]
    else:
        parts = [judge(sid) for sid in strategies]
    parts += _problem_parts(inputs, gate_id)
    return _gate(gate_id, parts, criteria, manual=manual)


def _gate(
    gate_id: str,
    parts: Sequence[_Part],
    criteria: dict[str, JsonValue],
    *,
    manual: tuple[str, ...] = (),
) -> GateResult:
    status: Status = max(
        (part.status for part in parts), key=_RANK.__getitem__, default="NOT_EVALUABLE"
    )
    # Keyed by file name: the artifacts hold dicts and are not hashable.
    used = tuple({item.file_name: item for part in parts for item in part.used}.values())
    # Worst parts first, so the evidence (and the verdict's reason) opens with what decides it.
    ordered = sorted(parts, key=lambda part: -_RANK[part.status])
    lines = [
        f"{part.subject}: {STATUS_TEXT[part.status]} — " + "; ".join(part.reasons)
        for part in ordered
    ]
    files = sorted({item.file_name for item in used})
    lines.append("dosyalar: " + (", ".join(files) if files else "yok"))
    if manual:
        lines.append("elle teyit: " + " ".join(manual))
    details: dict[str, JsonValue] = {
        "criteria": criteria,
        "parts": [
            {
                "subject": part.subject,
                "status": part.status,
                "reasons": list[JsonValue](part.reasons),
                "values": part.values,
            }
            for part in parts
        ],
        "files": [
            {
                "file": item.file_name,
                "kind": item.artifact.kind,
                "created_utc": item.artifact.created_utc,
                "synthetic": item.artifact.synthetic,
            }
            for item in used
        ],
        "manual": list[JsonValue](manual),
        "problems": [part.values["problem"] for part in parts if "problem" in part.values],
    }
    gate = Gate(
        id=gate_id,  # type: ignore[arg-type]
        title=GATE_TITLES[gate_id],
        status=status,
        evidence=_clip("\n".join(lines), _EVIDENCE_LIMIT),
        details=details,
    )
    return GateResult(gate=gate, used=used)


def _part(
    subject: str,
    checks: Sequence[tuple[Status, str]],
    used: Sequence[LoadedArtifact],
    values: dict[str, JsonValue],
) -> _Part:
    status: Status = max((s for s, _ in checks), key=_RANK.__getitem__, default="NOT_EVALUABLE")
    # The reasons that decide the status come first, so the evidence opens with them.
    ordered = sorted(checks, key=lambda check: -_RANK[check[0]])
    return _Part(subject, status, tuple(text for _, text in ordered), tuple(used), values)


# --- small helpers ------------------------------------------------------------------------


def _newest_first(items: Iterable[LoadedArtifact]) -> tuple[LoadedArtifact, ...]:
    return tuple(
        sorted(items, key=lambda item: (_created(item.artifact), item.file_name), reverse=True)
    )


def _created(artifact: AnyArtifact) -> dt.datetime:
    return dt.datetime.fromisoformat(artifact.created_utc).astimezone(dt.UTC)


def _backtest(item: LoadedArtifact) -> BacktestArtifact:
    assert isinstance(item.artifact, BacktestArtifact)
    return item.artifact


def _validation(item: LoadedArtifact) -> DataValidationArtifact:
    assert isinstance(item.artifact, DataValidationArtifact)
    return item.artifact


def _monte_carlo(item: LoadedArtifact) -> MonteCarloArtifact:
    assert isinstance(item.artifact, MonteCarloArtifact)
    return item.artifact


def _parity(item: LoadedArtifact) -> ParityArtifact:
    assert isinstance(item.artifact, ParityArtifact)
    return item.artifact


def _t_stat(mean: float | None, stdev: float | None, n: int) -> float | None:
    if mean is None or stdev is None or not stdev > 0.0 or n < 2:
        return None
    error = stdev / math.sqrt(n)
    # A deviation near the smallest float underflows to zero here (a hand-edited file):
    # then there is no t-statistic to compare the stored one with, not a division error.
    return mean / error if error > 0.0 else None


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_CONSISTENCY_REL_TOL, abs_tol=_CONSISTENCY_ABS_TOL)


def _pct(fraction: float) -> str:
    return "%" + tr_number(100.0 * fraction, 1)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 30].rstrip() + " … (devamı ayrıntılarda)"


def _line(text: str) -> str:
    """One Markdown line: whitespace collapsed, HTML openers and images defused."""
    return _defuse(" ".join(text.split()))


def _cell(text: str) -> str:
    """One table cell: as `_line`, plus backslashes and pipes escaped (GFM splits a cell at
    a pipe preceded by an escaped backslash, so the backslash is escaped first)."""
    return "<br>".join(
        _defuse(" ".join(part.split()).replace("\\", "\\\\").replace("|", "\\|"))
        for part in text.splitlines()
        if part.strip()
    )


def _defuse(text: str) -> str:
    return _HTML_OPENER.sub("&lt;", text).replace("![", "!\\[")


def _non_passing_parts(gate: Gate) -> int:
    parts = gate.details.get("parts")
    if not isinstance(parts, list):
        return 0
    return sum(1 for part in parts if isinstance(part, dict) and part.get("status") != "PASS")


def _g7_venue(gate: Gate) -> str | None:
    parts = gate.details.get("parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        return None
    values = parts[0].get("values")
    venue = values.get("venue") if isinstance(values, dict) else None
    return venue if isinstance(venue, str) and venue else None


_VALUE_LABELS: Final[dict[str, str]] = {
    "backtest_file": "backtest dosyası",
    "data_file": "veri dosyası",
    "sha256": "sha256",
    "tf_seconds": "bar uzunluğu (sn)",
    "bars": "bar sayısı",
    "first_utc": "ilk bar (UTC)",
    "last_utc": "son bar (UTC)",
    "span_years": "kapsam (yıl)",
    "validation_file": "doğrulama raporu",
    "validation_clean": "doğrulama temiz",
    "fill_model": "dolum modeli",
    "cost_free": "maliyetsiz",
    "oos_start_utc": "OOS başlangıcı (UTC)",
    "oos_n": "OOS işlem sayısı",
    "oos_mean_r": "OOS beklenti (R)",
    "oos_stdev_r": "OOS standart sapma (R)",
    "oos_t_stat": "OOS t-istatistiği",
    "g3_ratio": "OOS/IS oranı",
    "is_mean_r": "IS beklenti (R)",
    "g3_note": "oran notu",
    "file": "dosya",
    "venue": "venue",
    "trades_file": "işlem dosyası",
    "n_trades": "işlem sayısı",
    "risk_pct": "risk (%)",
    "cost_r": "maliyet (R)",
    "n_paths": "yol sayısı",
    "seed": "tohum",
    "max_days": "ufuk (piyasa günü)",
    "skip_prob": "işlem atlama olasılığı",
    "experimental_risk_steps": "deneysel risk basamakları",
    "p_pass_all_phases": "P(tüm fazlar)",
    "p_breach": "P(ihlal)",
    "p_undecided": "P(sonuçlanmadı)",
    "match_rate": "eşleşme oranı",
    "passed": "geçti",
    "matched": "eşleşen",
    "python_only": "yalnız Python",
    "tradingview_only": "yalnız TradingView",
    "rules_verified_at": "rules_verified_at",
    "rules_state": "kural durumu",
    "age_days": "yaş (gün)",
    "automation_allowed": "otomasyon izinli",
    "compliance_files": "docs/compliance/ dosyaları",
    "problem": "sorun",
    "min_data_years": "asgari veri (yıl)",
    "required_tf_seconds": "azami bar uzunluğu (sn)",
    "min_oos_t_stat": "asgari OOS t-istatistiği",
    "min_oos_to_is_ratio": "asgari OOS/IS oranı",
    "min_p_pass_all_phases": "asgari P(tüm fazlar)",
    "max_p_breach": "azami P(ihlal)",
    "min_paths": "asgari yol sayısı",
    "min_trades": "asgari işlem sayısı",
    "planned_risk_pct": "planlanan risk (%)",
    "min_parity_rate": "asgari parite oranı",
    "min_parity_trades": "asgari parite işlem sayısı",
    "compared": "karşılaştırılan işlem",
    "parity_data": "parite verisi",
    "intratrade": "işlem içi model",
    "sum_r": "listenin R toplamı",
    "sum_mae_r": "listenin mae_r toplamı",
    "max_loss_type": "toplam kayıp türü",
    "daily_loss_basis": "günlük kayıp bazı",
    "daily_reset": "günlük reset",
    "compliance_dir": "uyum belgeleri klasörü",
    "compliance_dir_found": "klasör bulundu",
    "bar_tz": "bar saat dilimi",
    "rules_max_age_days": "kuralların azami yaşı (gün)",
    "cost_factor": "maliyet çarpanı",
    "provenance": "işlem listesinin kökeni",
    "synthetic": "sentetik",
    "data_sha256": "veri sha256",
    "strategy_ids": "stratejiler",
    "created_utc": "oluşturulma (UTC)",
    "min_bar_hours_per_year": "yıllık asgari bar saati",
    "max_artifact_age_days": "raporların azami yaşı (gün)",
    "min_oos_trades": "asgari OOS/IS işlem sayısı",
}


def _render_details(details: dict[str, JsonValue]) -> list[str]:
    lines: list[str] = []
    criteria = details.get("criteria")
    if isinstance(criteria, dict) and criteria:
        lines.append("- Eşikler: " + _render_mapping(criteria))
    parts = details.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            status = part.get("status")
            status_text = STATUS_TEXT.get(status, "?") if isinstance(status, str) else "?"
            lines.append(f"- **{_cell(_render_value(part.get('subject')))}** — {status_text}")
            reasons = part.get("reasons")
            if isinstance(reasons, list):
                lines += [f"  - {_line(_render_value(reason))}" for reason in reasons]
            values = part.get("values")
            if isinstance(values, dict) and values:
                lines.append("  - Değerler: " + _render_mapping(values))
    return lines or ["- ayrıntı yok"]


def _render_mapping(values: dict[str, JsonValue]) -> str:
    return "; ".join(
        f"{_VALUE_LABELS.get(key, key)}: {_render_value(value)}" for key, value in values.items()
    )


def _render_value(value: JsonValue) -> str:
    if value is None:
        return "yok"
    if isinstance(value, bool):
        return "evet" if value else "hayır"
    if isinstance(value, int):
        return tr_int(value)
    if isinstance(value, float):
        return tr_number(value, 4) if math.isfinite(value) else "geçersiz"
    if isinstance(value, str):
        return _line(value) or "—"
    if isinstance(value, list):
        return ", ".join(_render_value(item) for item in value) or "yok"
    return _render_mapping(value)


def _collect_strings(artifact: GoNoGoArtifact, key: str) -> list[str]:
    found: dict[str, None] = {}
    for gate in artifact.gates:
        items = gate.details.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str):
                    found[item] = None
    return list(found)


def _collect_files(artifact: GoNoGoArtifact) -> list[tuple[str, str, str, bool]]:
    found: dict[str, tuple[str, str, str, bool]] = {}
    for gate in artifact.gates:
        items = gate.details.get("files")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name, kind, stamp = item.get("file"), item.get("kind"), item.get("created_utc")
            synthetic = item.get("synthetic")
            if isinstance(name, str) and isinstance(kind, str) and isinstance(stamp, str):
                found[name] = (name, kind, stamp, synthetic is True)
    return [found[name] for name in sorted(found)]


# --- files --------------------------------------------------------------------------------


def _check_replaceable(path: Path, artifact: GoNoGoArtifact) -> None:
    """Refuse to replace a Markdown file this module did not write or a newer report."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ArtifactError(
            f"{path.name}: dosya açılamadı ({exc.strerror}); üzerine yazılmadı"
        ) from None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ArtifactError(f"{path.name}: normal bir dosya değil; üzerine yazılmadı")
            first = handle.readline(512).decode("utf-8", errors="replace").strip()
    except OSError as exc:
        raise ArtifactError(f"{path.name}: dosya okunamadı ({exc.strerror})") from None
    match = _MARKER.fullmatch(first)
    if match is None:
        raise ArtifactError(
            f"{path.name}: bu modülün yazmadığı bir dosya; üzerine yazılmadı (taşıyın ya da silin)"
        )
    try:
        existing = dt.datetime.fromisoformat(match.group("stamp"))
    except ValueError:
        raise ArtifactError(f"{path.name}: işaret satırı bozuk; üzerine yazılmadı") from None
    if existing.utcoffset() is None or existing >= _created(artifact):
        raise ArtifactError(
            f"{path.name}: aynı ya da daha yeni bir rapor var ({match.group('stamp')}); "
            "üzerine yazılmadı"
        )


def _replace_atomically(path: Path, payload: bytes) -> None:
    if len(payload) > _MARKDOWN_LIMIT_BYTES:
        raise ArtifactError(f"{path.name}: rapor çok büyük ({len(payload)} bayt)")
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
