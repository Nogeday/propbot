"""Read and validate `config/risk.yaml` and `config/venues/*.yaml`.

Fail-closed: any problem (unreadable file, duplicate key, schema error, cross-file
rule) raises `ConfigError` carrying every issue found.
"""

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import ValidationError

from propbot.risk.config import RiskConfig
from propbot.risk.errors import ConfigError, ConfigIssue, issues_from_validation_error
from propbot.risk.rules import cross_check
from propbot.risk.venue import VenueProfile

RISK_FILE_NAME = "risk.yaml"
VENUES_DIR_NAME = "venues"


class _DuplicateKeyError(yaml.constructor.ConstructorError):
    def __init__(self, key: object, mark: yaml.Mark) -> None:
        super().__init__(None, None, f"duplicate key {key!r}", mark)
        self.key = key


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                continue  # the parent implementation reports unhashable keys
            if key in seen:
                raise _DuplicateKeyError(key, key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def read_yaml_mapping(path: Path) -> dict[Any, Any]:
    """Parse a YAML file whose top level must be a mapping."""
    source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError([ConfigIssue(source, "", "dosya bulunamadı")]) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError([ConfigIssue(source, "", f"dosya okunamadı: {exc}")]) from exc

    try:
        # _UniqueKeyLoader derives from SafeLoader: no arbitrary object construction.
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except _DuplicateKeyError as exc:
        line = _line_of(exc)
        raise ConfigError(
            [ConfigIssue(source, "", f"yinelenen anahtar {exc.key!r} (satır {line})")]
        ) from exc
    except yaml.MarkedYAMLError as exc:
        detail = exc.problem or "sözdizimi hatası"
        raise ConfigError(
            [ConfigIssue(source, "", f"YAML okunamadı (satır {_line_of(exc)}): {detail}")]
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError([ConfigIssue(source, "", f"YAML okunamadı: {exc}")]) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            [ConfigIssue(source, "", "en üst düzey anahtar: değer eşlemesi olmalı (dosya boş mu?)")]
        )
    return data


def _line_of(exc: yaml.MarkedYAMLError) -> str:
    mark = exc.problem_mark
    return str(mark.line + 1) if mark is not None else "?"


def load_risk_config(path: Path) -> RiskConfig:
    data = read_yaml_mapping(path)
    try:
        return RiskConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(issues_from_validation_error(exc, str(path))) from exc


def load_venue_profile(path: Path) -> VenueProfile:
    data = read_yaml_mapping(path)
    try:
        return VenueProfile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(issues_from_validation_error(exc, str(path))) from exc


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    risk: RiskConfig
    venues: Mapping[str, VenueProfile]  # keyed by file stem, e.g. "ftmo_2step_100k"


def load_config_dir(config_dir: Path) -> LoadedConfig:
    """Load `risk.yaml` and every venue profile, then apply the cross-file rules.

    All files are read even when an earlier one fails, so one run reports everything.
    """
    issues: list[ConfigIssue] = []

    risk_path = config_dir / RISK_FILE_NAME
    risk: RiskConfig | None = None
    try:
        risk = load_risk_config(risk_path)
    except ConfigError as exc:
        issues.extend(exc.issues)

    venues_dir = config_dir / VENUES_DIR_NAME
    venue_paths = sorted(venues_dir.glob("*.yaml"))
    if not venue_paths:
        issues.append(ConfigIssue(str(venues_dir), "", "venue profili yok (*.yaml)"))

    venues: dict[str, VenueProfile] = {}
    for path in venue_paths:
        try:
            venues[path.stem] = load_venue_profile(path)
        except ConfigError as exc:
            issues.extend(exc.issues)

    if risk is not None:
        for path in venue_paths:
            venue = venues.get(path.stem)
            if venue is not None:
                issues.extend(
                    cross_check(risk, venue, risk_source=str(risk_path), venue_source=str(path))
                )

    if issues or risk is None:
        raise ConfigError(issues)
    return LoadedConfig(risk=risk, venues=MappingProxyType(venues))
