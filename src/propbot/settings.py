"""Secrets from the environment and the `.env` file (CLAUDE.md invariant 6).

Exactly four values are read: the Telegram bot token, the single allowed chat id, the
TradingView webhook token and the PIN. Fail-closed: a missing, empty or malformed value, or
an env file that other users can read, stops the start-up with a `SettingsError` that lists
every problem at once in Turkish. Neither the error nor anything chained to it carries a
value, so it is safe to print and to log. Broker and prop-firm passwords never enter Python.
"""

import os
import re
import shlex
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Final, Self

from pydantic import AfterValidator, BeforeValidator, SecretStr, ValidationError, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

# Permission bits of group and others: all of them must be clear on the env file.
_GROUP_OTHER_BITS: Final = 0o077

# Shape of a BotFather token: numeric bot id, colon, key.
_BOT_TOKEN: Final = re.compile(r"[0-9]+:[A-Za-z0-9_-]{20,}")
# Characters that need no escaping inside the JSON string literal the Pine scripts build.
_WEBHOOK_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{32,128}")
_PIN: Final = re.compile(r"[0-9]{6,12}")
# ASCII digits only, no sign other than a leading minus, no leading zeros, no separators.
_CHAT_ID: Final = re.compile(r"-?(?:0|[1-9][0-9]{0,18})")

# Types of the errors raised below. Their messages are written here, in Turkish, and never
# contain the offending value; messages of any other pydantic error are not passed on.
_EMPTY: Final = "propbot_empty"
_FORMAT: Final = "propbot_format"
_CHAT_ID_NOT_INTEGER: Final = "propbot_chat_id_not_integer"
_CHAT_ID_NOT_PRIVATE: Final = "propbot_chat_id_not_private"
_SECRETS_IDENTICAL: Final = "propbot_secrets_identical"
_OWN_ERROR_TYPES: Final = frozenset(
    {_EMPTY, _FORMAT, _CHAT_ID_NOT_INTEGER, _CHAT_ID_NOT_PRIVATE, _SECRETS_IDENTICAL}
)

_MISSING_MESSAGE: Final = "tanımlı değil (.env dosyasına ya da ortam değişkenlerine ekle)"
_EMPTY_MESSAGE: Final = "boş bırakılmış"
_BOT_TOKEN_MESSAGE: Final = (
    "biçim uymuyor: BotFather'ın verdiği token '<rakamlar>:<en az 20 karakter; harf, rakam, "
    "- ya da _>' biçimindedir"
)
_WEBHOOK_TOKEN_MESSAGE: Final = (
    "biçim uymuyor: 32-128 karakter olmalı; yalnızca harf (A-Z, a-z), rakam, - ve _ içerebilir. "
    'Üretmek için: python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
)
_PIN_MESSAGE: Final = "biçim uymuyor: 6-12 haneli olmalı ve yalnızca rakam içerebilir"
_CHAT_ID_NOT_INTEGER_MESSAGE: Final = (
    "tam sayı olmalı (yalnızca rakam; boşluk, tırnak ya da başka karakter olmadan)"
)
_CHAT_ID_NOT_PRIVATE_MESSAGE: Final = (
    "pozitif olmalı: yalnızca kişisel sohbet kimliği kabul edilir "
    "(grup ve kanal kimlikleri negatiftir; sinyaller tek kişiye gider)"
)


class SettingsError(Exception):
    """The secrets could not be loaded. Carries every problem found; none contains a value."""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues: tuple[str, ...] = tuple(issues)
        super().__init__("\n".join(self.issues))


def _matching(value: SecretStr, pattern: re.Pattern[str], message: str) -> SecretStr:
    text = value.get_secret_value()
    if not text:
        raise PydanticCustomError(_EMPTY, _EMPTY_MESSAGE)
    if pattern.fullmatch(text) is None:
        raise PydanticCustomError(_FORMAT, message)
    return value


def _bot_token(value: SecretStr) -> SecretStr:
    return _matching(value, _BOT_TOKEN, _BOT_TOKEN_MESSAGE)


def _webhook_token(value: SecretStr) -> SecretStr:
    return _matching(value, _WEBHOOK_TOKEN, _WEBHOOK_TOKEN_MESSAGE)


def _pin(value: SecretStr) -> SecretStr:
    return _matching(value, _PIN, _PIN_MESSAGE)


def _chat_id(value: object) -> int:
    """Strict integer: pydantic's lax parsing would also take " 5", "5.0", "+5" and "1_000"."""
    if isinstance(value, str):
        if not value:
            raise PydanticCustomError(_EMPTY, _EMPTY_MESSAGE)
        if _CHAT_ID.fullmatch(value) is None:
            raise PydanticCustomError(_CHAT_ID_NOT_INTEGER, _CHAT_ID_NOT_INTEGER_MESSAGE)
        number = int(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        number = value
    else:
        raise PydanticCustomError(_CHAT_ID_NOT_INTEGER, _CHAT_ID_NOT_INTEGER_MESSAGE)
    if number <= 0:
        # CLAUDE.md invariant 7: one private chat. Group and channel ids are negative.
        raise PydanticCustomError(_CHAT_ID_NOT_PRIVATE, _CHAT_ID_NOT_PRIVATE_MESSAGE)
    return number


class Settings(BaseSettings):
    """The four secrets. Environment variable names are the field names in upper case.

    Use `load_settings`; it checks the file permissions and reports problems without values.
    Constructing the class directly (tests) reads nothing but the keyword arguments and the
    process environment, never a `.env` in the working directory.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        # Keys this class does not know are left alone, in the file and in the environment.
        extra="ignore",
        dotenv_filtering="only_existing",
        frozen=True,
        hide_input_in_errors=True,
    )

    telegram_bot_token: Annotated[SecretStr, AfterValidator(_bot_token)]
    telegram_allowed_chat_id: Annotated[int, BeforeValidator(_chat_id)]
    tv_webhook_token: Annotated[SecretStr, AfterValidator(_webhook_token)]
    propbot_pin: Annotated[SecretStr, AfterValidator(_pin)]

    @model_validator(mode="after")
    def _secrets_are_pairwise_different(self) -> Self:
        # The three formats cannot overlap today (colon, length), so this guards a later
        # change of a pattern: one leaked value must never open two doors.
        secrets = (
            ("telegram_bot_token", self.telegram_bot_token),
            ("tv_webhook_token", self.tv_webhook_token),
            ("propbot_pin", self.propbot_pin),
        )
        for index, (first_name, first) in enumerate(secrets):
            for second_name, second in secrets[index + 1 :]:
                if first.get_secret_value() == second.get_secret_value():
                    raise PydanticCustomError(
                        _SECRETS_IDENTICAL,
                        "{first} ve {second} aynı değer olamaz",
                        {"first": first_name.upper(), "second": second_name.upper()},
                    )
        return self

    @classmethod
    def _settings_log_debug(cls, states: dict[str, dict[str, Any]]) -> None:
        # pydantic-settings dumps the raw value of every source to its logger when
        # PYDANTIC_SETTINGS_DEBUG is set. That would write the secrets to the log.
        return None


def check_env_file_permissions(path: Path) -> None:
    """Raise `SettingsError` unless `path` is a regular file only its owner can access."""
    try:
        info = os.stat(path)
    except FileNotFoundError as exc:
        raise SettingsError(
            [
                f"{path}: dosya bulunamadı "
                "(oluşturmak için: cp .env.example .env && chmod 600 .env, sonra değerleri yaz)"
            ]
        ) from exc
    except OSError as exc:
        raise SettingsError([f"{path}: dosyaya erişilemedi ({type(exc).__name__})"]) from exc

    if not stat.S_ISREG(info.st_mode):
        raise SettingsError([f"{path}: normal bir dosya değil"])
    mode = stat.S_IMODE(info.st_mode)
    if mode & _GROUP_OTHER_BITS:
        raise SettingsError(
            [
                f"{path}: dosya izinleri {mode:03o}; yalnızca sahibi okuyabilmeli. "
                f"Düzeltmek için: chmod 600 {shlex.quote(str(path))}"
            ]
        )


def load_settings(env_file: Path | None) -> Settings:
    """Read the secrets from `env_file` (when given) and the process environment.

    Real environment variables override the file. The file must exist and be owner-only;
    it is not read otherwise. Every value problem is reported at once.
    """
    if env_file is not None:
        check_env_file_permissions(env_file)

    # The SettingsError is raised outside the `except` blocks on purpose: pydantic's error
    # keeps the raw input values (`errors()`, `json()`) and the decode error keeps the file
    # content, so neither may travel along as `__cause__` or `__context__`.
    issues: list[str]
    # pydantic-settings takes its per-call source options as underscore keywords. They are
    # passed as a mapping because the type checker only knows the four fields as parameters.
    source_options: dict[str, Any] = {"_env_file": env_file, "_env_file_encoding": "utf-8"}
    try:
        return Settings(**source_options)
    except ValidationError as exc:
        issues = _issues_from_validation_error(exc)
    except UnicodeDecodeError:
        issues = [f"{env_file}: UTF-8 olarak okunamadı"]
    except OSError as exc:
        issues = [f"{env_file}: dosya okunamadı ({type(exc).__name__})"]
    raise SettingsError(issues)


def _issues_from_validation_error(exc: ValidationError) -> list[str]:
    issues: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        kind = error["type"]
        if kind == "missing":
            message = _MISSING_MESSAGE
        elif kind in _OWN_ERROR_TYPES:
            message = error["msg"]
        else:
            message = f"geçersiz değer ({kind})"
        name = ".".join(str(part) for part in error["loc"]).upper()
        issues.append(f"{name}: {message}" if name else message)
    return issues
