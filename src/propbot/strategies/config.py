"""`config/strategies.yaml`: per-strategy overrides of the Pine input defaults.

The ports start from the defaults of the Pine scripts they mirror (ADR-0008 item 9).
This file can change a parameter, and each change is a deviation from the TradingView
script that the user must make there too for parity to hold. Fail-closed like the other
config files: an unknown strategy id, an unknown parameter or a refused value is a load
error that names every problem.

One input cannot be changed here: the per-trade risk (`StrategySpec.risk_param`). Its
single source is config/risk.yaml (user decision of 2026-11-19, open finding 82): a
second value in this file would let the decision runs and the Monte Carlo simulation be
sized at different risks again.

The risk BASE (`risk_base`, `fixed_bal`) may be set here, because parity and the
`--pine-risk` comparison run are made at the script's inputs as configured. It is a
second source for the risk all the same, so the registry refuses it wherever the planned
risk is used: with another base in this file the strategy has no decision run, and
`propbot backtest` (without --pine-risk) and `propbot gonogo` stop and say so.
"""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from pydantic import ValidationError

from propbot.risk.errors import ConfigError, ConfigIssue, issues_from_validation_error
from propbot.risk.loader import read_yaml_mapping
from propbot.strategies.registry import STRATEGIES

STRATEGIES_FILE_NAME: Final = "strategies.yaml"
_TOP_KEY: Final = "strategies"
RISK_OVERRIDE_REFUSED: Final = (
    "işlem başı risk burada değiştirilemez: tek kaynak config/risk.yaml (risk_per_trade_pct); "
    "betiğin kendi değeriyle karşılaştırma koşusu için propbot backtest --pine-risk"
)

type Overrides = Mapping[str, Mapping[str, object]]


def load_strategy_overrides(path: Path) -> Overrides:
    """Read the file; return the overrides of every registered strategy (empty = defaults).

    A strategy the file does not mention runs on the Pine defaults.
    """
    source = str(path)
    data = read_yaml_mapping(path)
    issues: list[ConfigIssue] = [
        ConfigIssue(source, str(key), "bilinmeyen anahtar") for key in data if key != _TOP_KEY
    ]
    section = data.get(_TOP_KEY)
    if not isinstance(section, dict):
        issues.append(
            ConfigIssue(source, _TOP_KEY, "strateji kimliği: parametreler eşlemesi olmalı")
        )
        raise ConfigError(issues)

    overrides: dict[str, Mapping[str, object]] = {name: MappingProxyType({}) for name in STRATEGIES}
    for name, values in section.items():
        location = f"{_TOP_KEY}.{name}"
        spec = STRATEGIES.get(name) if isinstance(name, str) else None
        if spec is None:
            known = ", ".join(sorted(STRATEGIES))
            issues.append(
                ConfigIssue(source, location, f"bilinmeyen strateji (tanımlılar: {known})")
            )
            continue
        if values is None:
            values = {}
        if not isinstance(values, dict) or not all(isinstance(key, str) for key in values):
            issues.append(ConfigIssue(source, location, "parametre adı: değer eşlemesi olmalı"))
            continue
        checked: dict[str, Any] = dict(values)
        # Refused whatever its value, the Pine default included: the key itself is the
        # second source. Taken out so the other keys' mistakes are still all reported.
        risk_given = spec.risk_param in checked
        if risk_given:
            del checked[spec.risk_param]
            issues.append(
                ConfigIssue(source, f"{location}.{spec.risk_param}", RISK_OVERRIDE_REFUSED)
            )
        try:
            spec.params_model.model_validate(checked)
        except ValidationError as exc:
            issues.extend(
                ConfigIssue(source, f"{location}.{issue.location}", issue.message)
                for issue in issues_from_validation_error(exc, source)
            )
            continue
        if not risk_given:
            overrides[name] = MappingProxyType(checked)

    if issues:
        raise ConfigError(issues)
    return MappingProxyType(overrides)
