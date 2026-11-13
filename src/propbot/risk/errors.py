"""Config validation errors with user-facing (Turkish) messages."""

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import ValidationError


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """One problem found in a config file."""

    source: str
    location: str
    message: str

    def __str__(self) -> str:
        where = f"{self.source}: {self.location}" if self.location else self.source
        return f"{where}: {self.message}"


class ConfigError(Exception):
    """Config files failed validation. Carries every issue found, not only the first."""

    def __init__(self, issues: Iterable[ConfigIssue]) -> None:
        self.issues: tuple[ConfigIssue, ...] = tuple(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


# pydantic error type -> Turkish template. Placeholders come from the error's `ctx`.
# Errors raised by our own validators (PydanticCustomError) already carry a Turkish message.
_TEMPLATES: dict[str, str] = {
    "missing": "zorunlu alan eksik",
    "extra_forbidden": "bilinmeyen anahtar",
    "float_type": "sayı olmalı (tırnaksız; true/false ya da metin olamaz)",
    "int_type": "tam sayı olmalı",
    "bool_type": "true ya da false olmalı",
    "string_type": "metin olmalı",
    "finite_number": "sonlu bir sayı olmalı",
    "greater_than": "{gt} değerinden büyük olmalı",
    "greater_than_equal": "en az {ge} olmalı",
    "less_than": "{lt} değerinden küçük olmalı",
    "less_than_equal": "en fazla {le} olabilir",
    "enum": "geçersiz değer; izin verilenler: {expected}",
    "string_pattern_mismatch": "biçim uymuyor (beklenen desen: {pattern})",
    "string_too_short": "boş olamaz",
    "model_type": "anahtar: değer eşlemesi olmalı",
    "tuple_type": "liste olmalı",
}

# Error types where echoing the offending value adds nothing (or there is no value).
_NO_INPUT_ECHO = frozenset({"missing", "extra_forbidden", "model_type"})


def issues_from_validation_error(exc: ValidationError, source: str) -> list[ConfigIssue]:
    """Convert a pydantic ValidationError into ConfigIssues with Turkish messages."""
    issues: list[ConfigIssue] = []
    for error in exc.errors(include_url=False):
        kind = error["type"]
        template = _TEMPLATES.get(kind)
        if template is None:
            message = error["msg"]
        else:
            context = {key: _show(value) for key, value in (error.get("ctx") or {}).items()}
            message = template.format(**context)
            value = error.get("input")
            if value is None and kind.endswith("_type"):
                message = "boş (null) bırakılamaz"
            elif kind not in _NO_INPUT_ECHO and isinstance(value, str | int | float | bool):
                message = f"{message}; okunan: {value!r}"
        location = ".".join(str(part) for part in error["loc"])
        issues.append(ConfigIssue(source=source, location=location, message=message))
    return issues


def _show(value: object) -> str:
    # pydantic renders enum choices as "'a', 'b' or 'c'"; keep the list language-neutral.
    return str(value).replace(" or ", ", ")
