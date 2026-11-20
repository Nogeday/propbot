"""The strategy ports the backtest can run, by the id their Pine script sends in its alerts.

One entry per port: the parameter model (its defaults are the Pine input defaults), the
class, the Pine file it mirrors and the ids its trade records carry. A single-strategy
script records its own id; the Master portfolio script runs two engines and records one
id per leg (user decision 47), so statistics and gates are kept per leg.

Each entry also names the script input that sets the per-trade risk (`risk_param`). The
port's default there is the Pine default (0.5 %); the account is planned at
config/risk.yaml's `risk_per_trade_pct` (user decision of 2026-11-19, open finding 82).
A decision run therefore writes the planned risk into that input (`with_planned_risk`),
and config/strategies.yaml may not set it: there is one source for the risk.

The percent is a percent of something: the scripts' `Risk base` input (`risk_base_param`).
The planned risk is a percent of the account's initial balance (SPEC §8.1), which is the
scripts' default base, "Initial Capital". On another base the same percent is another
risk (0.25 % of a fixed 200,000 is 0.5 % of a 100,000 account), so a run that is given
the planned risk refuses another base; the base stays an ordinary input for the runs at
the script's own inputs (parity, `--pine-risk`).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from pydantic import ValidationError

from propbot.risk.config import StrictModel
from propbot.strategies import (
    asian_sweep,
    ict_silver_bullet,
    master_portfolio,
    mean_reversion,
    mtf_trend,
    ny_orb_fvg,
)
from propbot.strategies.base import Strategy

# The option of the scripts' `Risk base` input under which the risk percent is a percent
# of the account's initial balance: what config/risk.yaml's percent means (SPEC §8.1).
ACCOUNT_RISK_BASE: Final = "Initial Capital"


class StrategyError(Exception):
    """An unknown strategy id or parameters a port refuses. User-facing (Turkish)."""


@dataclass(frozen=True, slots=True)
class StrategySpec[P: StrictModel]:
    strategy_id: str
    pine_file: str
    params_model: type[P]
    strategy_class: Callable[[P | None], Strategy]
    leg_ids: tuple[str, ...]  # the `strategy_id` values of the trade records it produces
    # The Pine input holding the per-trade risk in percent of the risk base. The scripts
    # do not agree on its name, so the registry says it instead of every caller guessing.
    risk_param: str
    # A second risk input the planned risk does NOT replace (the Master's cap on the risk
    # of both engines together); named so a run summary can say it is the script's own.
    risk_cap_param: str | None = None
    # The input choosing what the risk percent is a percent of (Pine `Risk base`). None:
    # the script has no such input and always sizes on the initial capital.
    risk_base_param: str | None = None

    def __post_init__(self) -> None:
        for name in (self.risk_param, self.risk_cap_param, self.risk_base_param):
            if name is not None and name not in self.params_model.model_fields:
                raise ValueError(
                    f"{self.strategy_id}: {self.params_model.__name__} has no field {name!r}"
                )
        if self.risk_base_param is not None:
            default = self.params_model.model_fields[self.risk_base_param].default
            if default != ACCOUNT_RISK_BASE:
                # "The script's own risk" is quoted as a percent of the account everywhere;
                # a port defaulting to another base would make that untrue.
                raise ValueError(
                    f"{self.strategy_id}: {self.risk_base_param} must default to "
                    f"{ACCOUNT_RISK_BASE!r}, not {default!r}"
                )

    @property
    def pine_risk_pct(self) -> float:
        """The script's own per-trade risk: the default of its risk input."""
        return float(self.params_model.model_fields[self.risk_param].default)

    def risk_base(self, values: Mapping[str, object] | None) -> str:
        """What the risk percent of a run with these values is a percent of."""
        if self.risk_base_param is None:
            return ACCOUNT_RISK_BASE
        return str((values or {}).get(self.risk_base_param, ACCOUNT_RISK_BASE))

    def sized_on_account(self, values: Mapping[str, object] | None) -> bool:
        """True when the run's risk percent is a percent of the account's initial balance."""
        return self.risk_base(values) == ACCOUNT_RISK_BASE

    def with_planned_risk(
        self, overrides: Mapping[str, object] | None, planned_risk_pct: float | None
    ) -> dict[str, object]:
        """The parameter values of a run: `overrides`, plus the planned risk when given.

        The one place where the planned risk becomes a parameter, used by the runner for
        the run and by the Go/No-Go command for what a decision run must have recorded,
        so the two cannot drift. `None` leaves the script's own risk (a comparison run).
        The risk input itself is refused in `overrides`: a second source for the risk
        would make "the script's own risk" and "the planned risk" both untrue. With a
        planned risk another risk base is refused as well: the planned risk is a percent
        of the account's initial balance, and on another base the recorded percent would
        read as the planned risk while the trades were sized at another.
        """
        values = dict(overrides or {})
        if self.risk_param in values:
            raise StrategyError(
                f"{self.strategy_id}: işlem başı risk ({self.risk_param}) parametre değişikliği "
                "olarak verilemez; tek kaynak config/risk.yaml (risk_per_trade_pct), betiğin "
                "kendi değeriyle koşmak için propbot backtest --pine-risk"
            )
        if planned_risk_pct is not None:
            if not self.sized_on_account(values):
                raise StrategyError(
                    f"{self.strategy_id}: planlanan işlem başı risk hesabın başlangıç "
                    f"bakiyesinin yüzdesidir (SPEC §8.1); {self.risk_base_param}="
                    f"{self.risk_base(values)!r} iken karar koşusu yapılamaz (boyutlar başka bir "
                    "tabandan hesaplanır). config/strategies.yaml'dan bu değişikliği kaldırın ya "
                    "da betiğin kendi girdileriyle karşılaştırma koşusu için propbot backtest "
                    "--pine-risk"
                )
            values[self.risk_param] = float(planned_risk_pct)
        return values

    def params(self, overrides: Mapping[str, object] | None = None) -> P:
        """The Pine defaults with `overrides` applied; unknown or invalid keys are refused."""
        try:
            return self.params_model.model_validate(dict(overrides or {}))
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
            )
            raise StrategyError(
                f"{self.strategy_id}: geçersiz parametre(ler): {', '.join(fields)}"
            ) from exc

    def build(self, overrides: Mapping[str, object] | None = None) -> Strategy:
        """A fresh strategy instance (instances hold state: one per run)."""
        return self.strategy_class(self.params(overrides))


def _single[P: StrictModel](
    module_id: str,
    pine_file: str,
    params_model: type[P],
    strategy_class: Callable[[P | None], Strategy],
    risk_param: str = "risk_pct",
) -> StrategySpec[P]:
    return StrategySpec(
        strategy_id=module_id,
        pine_file=pine_file,
        params_model=params_model,
        strategy_class=strategy_class,
        leg_ids=(module_id,),
        risk_param=risk_param,
        risk_base_param="risk_base",
    )


_SPECS: Final[tuple[StrategySpec[Any], ...]] = (
    _single(
        ny_orb_fvg.STRATEGY_ID, ny_orb_fvg.PINE_FILE, ny_orb_fvg.NyOrbFvgParams, ny_orb_fvg.NyOrbFvg
    ),
    _single(
        asian_sweep.STRATEGY_ID,
        asian_sweep.PINE_FILE,
        asian_sweep.AsianSweepParams,
        asian_sweep.AsianSweep,
    ),
    _single(
        ict_silver_bullet.STRATEGY_ID,
        ict_silver_bullet.PINE_FILE,
        ict_silver_bullet.IctSilverBulletParams,
        ict_silver_bullet.IctSilverBullet,
    ),
    _single(
        mean_reversion.STRATEGY_ID,
        mean_reversion.PINE_FILE,
        mean_reversion.MeanReversionParams,
        mean_reversion.MeanReversion,
    ),
    _single(
        mtf_trend.STRATEGY_ID,
        mtf_trend.PINE_FILE,
        mtf_trend.MtfTrendParams,
        mtf_trend.MtfTrend,
        # The MTF script calls the same input `riskPercent`.
        risk_param="risk_percent",
    ),
    StrategySpec(
        strategy_id=master_portfolio.STRATEGY_ID,
        pine_file=master_portfolio.PINE_FILE,
        params_model=master_portfolio.MasterPortfolioParams,
        strategy_class=master_portfolio.MasterPortfolio,
        leg_ids=tuple(master_portfolio.LEG_IDS),
        # One risk input shared by both engines; the cap on their sum stays the script's.
        risk_param="risk_pct",
        risk_cap_param="max_concurrent_risk_pct",
        risk_base_param="risk_base",
    ),
)

STRATEGIES: Final[Mapping[str, StrategySpec[Any]]] = MappingProxyType(
    {spec.strategy_id: spec for spec in _SPECS}
)


def strategy_spec(strategy_id: str) -> StrategySpec[Any]:
    spec = STRATEGIES.get(strategy_id)
    if spec is None:
        raise StrategyError(
            f"bilinmeyen strateji: {strategy_id} (tanımlılar: {', '.join(sorted(STRATEGIES))})"
        )
    return spec
