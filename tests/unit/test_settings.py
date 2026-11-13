"""Secrets loading: env file permissions, precedence, every format rule, value-free errors."""

import logging
import re
import secrets as stdlib_secrets
import traceback
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

import propbot.settings as settings_module
from propbot.settings import Settings, SettingsError, check_env_file_permissions, load_settings
from support.paths import REPO_ROOT

ENV_NAMES = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_ID", "TV_WEBHOOK_TOKEN", "PROPBOT_PIN")

# Made-up values in the real formats.
BOT_TOKEN = "7000000001:AAF-fake_bot_key_0123456789abcdefghi"
CHAT_ID = "987654321"
WEBHOOK_TOKEN = "wh_0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ_abc"
PIN = "424242"
VALID = {
    "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    "TELEGRAM_ALLOWED_CHAT_ID": CHAT_ID,
    "TV_WEBHOOK_TOKEN": WEBHOOK_TOKEN,
    "PROPBOT_PIN": PIN,
}

MISSING = "tanımlı değil"
MISSING_ISSUE = f"{MISSING} (.env dosyasına ya da ortam değişkenlerine ekle)"
EMPTY = "boş bırakılmış"
BAD_FORMAT = "biçim uymuyor"
NOT_INTEGER = "tam sayı olmalı"
NOT_PRIVATE = "pozitif olmalı"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may see the developer's variables or a `.env` in the working directory."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.delenv("PYDANTIC_SETTINGS_DEBUG", raising=False)
    monkeypatch.chdir(tmp_path)


def write_env(
    directory: Path,
    values: dict[str, str] | None = None,
    *,
    mode: int = 0o600,
    name: str = "test.env",
) -> Path:
    path = directory / name
    entries = VALID if values is None else values
    path.write_text("".join(f"{key}={value}\n" for key, value in entries.items()), "utf-8")
    path.chmod(mode)
    return path


def rendered(exc: BaseException) -> str:
    """Everything a logger or an uncaught-exception hook would print for `exc`."""
    return "".join(traceback.format_exception(exc)) + repr(exc) + str(exc) + repr(exc.args)


def load_error(env_file: Path | None) -> SettingsError:
    with pytest.raises(SettingsError) as excinfo:
        load_settings(env_file)
    return excinfo.value


# ----------------------------------------------------------------------------- happy path


def test_valid_env_file_is_loaded(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path))

    assert settings.telegram_bot_token.get_secret_value() == BOT_TOKEN
    assert settings.telegram_allowed_chat_id == int(CHAT_ID)
    assert settings.tv_webhook_token.get_secret_value() == WEBHOOK_TOKEN
    assert settings.propbot_pin.get_secret_value() == PIN


def test_environment_alone_is_enough_without_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in VALID.items():
        monkeypatch.setenv(name, value)

    settings = load_settings(None)

    assert settings.tv_webhook_token.get_secret_value() == WEBHOOK_TOKEN
    assert settings.telegram_allowed_chat_id == int(CHAT_ID)


OVERRIDES = {
    "TELEGRAM_BOT_TOKEN": "7000000002:BBG-other_bot_key_0123456789abcdefgh",
    "TELEGRAM_ALLOWED_CHAT_ID": "1122334455",
    "TV_WEBHOOK_TOKEN": "other_0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "PROPBOT_PIN": "135790",
}


@pytest.mark.parametrize("name", ENV_NAMES)
def test_real_environment_variable_overrides_the_file(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_env(tmp_path)
    monkeypatch.setenv(name, OVERRIDES[name])

    settings = load_settings(path)

    loaded = {
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token.get_secret_value(),
        "TELEGRAM_ALLOWED_CHAT_ID": str(settings.telegram_allowed_chat_id),
        "TV_WEBHOOK_TOKEN": settings.tv_webhook_token.get_secret_value(),
        "PROPBOT_PIN": settings.propbot_pin.get_secret_value(),
    }
    assert loaded == VALID | {name: OVERRIDES[name]}


def test_file_fills_what_the_environment_does_not_define(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_env(tmp_path, {"PROPBOT_PIN": PIN, "TELEGRAM_ALLOWED_CHAT_ID": CHAT_ID})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TV_WEBHOOK_TOKEN", WEBHOOK_TOKEN)

    settings = load_settings(path)

    assert settings.propbot_pin.get_secret_value() == PIN
    assert settings.telegram_bot_token.get_secret_value() == BOT_TOKEN


def test_unknown_keys_comments_quotes_and_export_are_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "test.env"
    path.write_text(
        "# a comment line\n"
        "\n"
        f"export TELEGRAM_BOT_TOKEN={BOT_TOKEN}\n"
        f"TELEGRAM_ALLOWED_CHAT_ID = {CHAT_ID}   # trailing comment\n"
        f'TV_WEBHOOK_TOKEN="{WEBHOOK_TOKEN}"\n'
        f"PROPBOT_PIN='{PIN}'\n"
        "BROKER_SERVER=somewhere\n"
        "LOG_LEVEL=DEBUG\n",
        "utf-8",
    )
    path.chmod(0o600)

    settings = load_settings(path)

    assert settings.telegram_bot_token.get_secret_value() == BOT_TOKEN
    assert settings.telegram_allowed_chat_id == int(CHAT_ID)
    assert settings.tv_webhook_token.get_secret_value() == WEBHOOK_TOKEN
    assert settings.propbot_pin.get_secret_value() == PIN
    assert set(Settings.model_fields) == {name.lower() for name in ENV_NAMES}
    assert settings.model_extra is None


def test_unknown_environment_variables_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROPBOT_SOMETHING_ELSE", "1")

    assert load_settings(write_env(tmp_path)).propbot_pin.get_secret_value() == PIN


def test_env_file_in_the_working_directory_is_not_read_implicitly(tmp_path: Path) -> None:
    # The fixture made tmp_path the working directory.
    write_env(tmp_path, name=".env")

    error = load_error(None)

    assert error.issues == tuple(f"{name}: {MISSING_ISSUE}" for name in ENV_NAMES)
    with pytest.raises(ValidationError):
        Settings()


def test_unparsable_line_is_reported_by_line_number_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_env(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("this line is LEAKED-JUNK-CONTENT without an equals sign !\n")

    with caplog.at_level(logging.DEBUG):
        settings = load_settings(path)

    assert settings.propbot_pin.get_secret_value() == PIN
    assert "LEAKED-JUNK-CONTENT" not in caplog.text
    for value in VALID.values():
        assert value not in caplog.text


# ---------------------------------------------------------------------- file permissions


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700, 0o200])
def test_owner_only_file_passes_the_permission_check(mode: int, tmp_path: Path) -> None:
    path = write_env(tmp_path, mode=mode)

    check_env_file_permissions(path)


@pytest.mark.parametrize(
    "mode",
    [0o640, 0o620, 0o610, 0o604, 0o602, 0o601, 0o644, 0o660, 0o664, 0o666, 0o755, 0o777, 0o477],
    ids=lambda mode: f"{mode:03o}",
)
def test_group_or_other_access_is_refused(mode: int, tmp_path: Path) -> None:
    path = write_env(tmp_path, mode=mode)

    with pytest.raises(SettingsError) as excinfo:
        check_env_file_permissions(path)

    (issue,) = excinfo.value.issues
    assert issue.startswith(f"{path}: dosya izinleri {mode:03o}")
    assert issue.endswith(f"chmod 600 {path}")


def test_chmod_hint_quotes_a_path_with_spaces(tmp_path: Path) -> None:
    path = write_env(tmp_path, mode=0o644, name="my secrets.env")

    with pytest.raises(SettingsError) as excinfo:
        check_env_file_permissions(path)

    assert str(excinfo.value).endswith(f"chmod 600 '{path}'")


def test_missing_file_fails_the_permission_check(tmp_path: Path) -> None:
    path = tmp_path / "absent.env"

    with pytest.raises(SettingsError) as excinfo:
        check_env_file_permissions(path)

    (issue,) = excinfo.value.issues
    assert issue.startswith(f"{path}: dosya bulunamadı")
    assert "chmod 600" in issue


def test_directory_is_not_an_env_file(tmp_path: Path) -> None:
    directory = tmp_path / "envdir"
    directory.mkdir(mode=0o700)

    with pytest.raises(SettingsError) as excinfo:
        check_env_file_permissions(directory)

    assert excinfo.value.issues == (f"{directory}: normal bir dosya değil",)


def test_path_below_a_file_is_reported_as_inaccessible(tmp_path: Path) -> None:
    path = write_env(tmp_path) / "child.env"

    with pytest.raises(SettingsError) as excinfo:
        check_env_file_permissions(path)

    assert excinfo.value.issues == (f"{path}: dosyaya erişilemedi (NotADirectoryError)",)


@pytest.mark.parametrize(("target_mode", "accepted"), [(0o600, True), (0o644, False)])
def test_symlink_is_judged_by_its_target(target_mode: int, accepted: bool, tmp_path: Path) -> None:
    target = write_env(tmp_path, mode=target_mode, name="target.env")
    link = tmp_path / "link.env"
    link.symlink_to(target)

    if accepted:
        assert load_settings(link).propbot_pin.get_secret_value() == PIN
    else:
        assert "chmod 600" in str(load_error(link))


def test_load_refuses_a_readable_file_without_reading_it(tmp_path: Path) -> None:
    # Every value in the file is wrong as well; only the permission problem is reported.
    path = write_env(tmp_path, dict.fromkeys(ENV_NAMES, "x"), mode=0o644)

    error = load_error(path)

    assert len(error.issues) == 1
    assert f"chmod 600 {path}" in error.issues[0]


def test_load_requires_the_named_file_even_when_the_environment_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in VALID.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "absent.env"

    error = load_error(path)

    assert len(error.issues) == 1
    assert error.issues[0].startswith(f"{path}: dosya bulunamadı")


def test_file_that_is_not_utf8_is_refused_without_its_content(tmp_path: Path) -> None:
    path = tmp_path / "test.env"
    path.write_bytes(b"PROPBOT_PIN=\xff\xfe" + PIN.encode() + b"\n")
    path.chmod(0o600)

    error = load_error(path)

    assert error.issues == (f"{path}: UTF-8 olarak okunamadı",)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert PIN not in rendered(error)


# ------------------------------------------------------------------------- value rules


@pytest.mark.parametrize("name", ENV_NAMES)
def test_each_missing_variable_is_named(name: str, tmp_path: Path) -> None:
    path = write_env(tmp_path, {key: value for key, value in VALID.items() if key != name})

    error = load_error(path)

    assert error.issues == (f"{name}: {MISSING_ISSUE}",)


@pytest.mark.parametrize("name", ENV_NAMES)
def test_empty_value_in_the_file_is_refused(name: str, tmp_path: Path) -> None:
    error = load_error(write_env(tmp_path, VALID | {name: ""}))

    assert error.issues == (f"{name}: {EMPTY}",)


@pytest.mark.parametrize("name", ENV_NAMES)
def test_empty_environment_variable_still_overrides_the_file(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: an empty override is a mistake to report, not a reason to fall back.
    path = write_env(tmp_path)
    monkeypatch.setenv(name, "")

    assert load_error(path).issues == (f"{name}: {EMPTY}",)


ACCEPTED: list[tuple[str, str]] = [
    ("TELEGRAM_BOT_TOKEN", "1:" + "a" * 20),
    ("TELEGRAM_BOT_TOKEN", "123456789:AAH_-" + "Z9" * 16),
    ("TELEGRAM_ALLOWED_CHAT_ID", "1"),
    ("TELEGRAM_ALLOWED_CHAT_ID", "5123456789"),
    ("TELEGRAM_ALLOWED_CHAT_ID", "4503599627370495"),
    ("TV_WEBHOOK_TOKEN", "a" * 32),
    ("TV_WEBHOOK_TOKEN", "Z" * 128),
    ("TV_WEBHOOK_TOKEN", "-_" * 16),
    ("TV_WEBHOOK_TOKEN", "0123456789-abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    ("PROPBOT_PIN", "000000"),
    ("PROPBOT_PIN", "123456789012"),
]


@pytest.mark.parametrize(("name", "value"), ACCEPTED)
def test_value_inside_the_format_is_accepted(
    name: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)

    settings = load_settings(write_env(tmp_path))

    loaded = getattr(settings, name.lower())
    assert (loaded.get_secret_value() if isinstance(loaded, SecretStr) else str(loaded)) == value


def test_generated_webhook_token_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The command printed in .env.example and in the error message.
    for _ in range(50):
        token = stdlib_secrets.token_urlsafe(32)
        monkeypatch.setenv("TV_WEBHOOK_TOKEN", token)

        assert load_settings(write_env(tmp_path)).tv_webhook_token.get_secret_value() == token


# (variable, offending value, fragment of the Turkish issue)
REJECTED: list[tuple[str, str, str]] = [
    # Bot token: <digits>:<20+ of A-Z a-z 0-9 _ ->
    ("TELEGRAM_BOT_TOKEN", "AAF-fake_bot_key_0123456789abcdefghi", BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", ":AAF-fake_bot_key_0123456789abcdefghi", BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", "bot7000000001:AAF-fake_bot_key_0123456789", BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", "7000000001:" + "k" * 19, BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", "7000000001:AAF-fake.bot.key.0123456789abc", BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", "7000000001:AAF-fake_bot_key:0123456789abc", BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", " " + BOT_TOKEN, BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", BOT_TOKEN + " ", BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", BOT_TOKEN + "\n", BAD_FORMAT),
    ("TELEGRAM_BOT_TOKEN", f"https://api.telegram.org/bot{BOT_TOKEN}", BAD_FORMAT),
    # Webhook token: 32-128 of A-Z a-z 0-9 _ - (must be safe inside Pine's JSON string)
    ("TV_WEBHOOK_TOKEN", "short-LEAKME-token", BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "b" * 31, BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "c" * 129, BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "d" * 31 + '"', BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "e" * 31 + "\\", BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "f" * 16 + " " + "f" * 16, BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "g" * 31 + ".", BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "h" * 31 + "+", BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "i" * 31 + "/", BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "j" * 31 + "=", BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", "k" * 31 + "ü", BAD_FORMAT),
    ("TV_WEBHOOK_TOKEN", WEBHOOK_TOKEN + "\n", BAD_FORMAT),
    # PIN: 6-12 ASCII digits
    ("PROPBOT_PIN", "12345", BAD_FORMAT),
    ("PROPBOT_PIN", "1234567890123", BAD_FORMAT),
    ("PROPBOT_PIN", "12345a", BAD_FORMAT),
    ("PROPBOT_PIN", "123 456", BAD_FORMAT),
    ("PROPBOT_PIN", "+123456", BAD_FORMAT),
    ("PROPBOT_PIN", "-123456", BAD_FORMAT),
    ("PROPBOT_PIN", "1234.56", BAD_FORMAT),
    ("PROPBOT_PIN", "١٢٣٤٥٦", BAD_FORMAT),
    ("PROPBOT_PIN", "１２３４５６", BAD_FORMAT),
    ("PROPBOT_PIN", "424242\n", BAD_FORMAT),
    # Chat id: a positive integer, written with ASCII digits only
    ("TELEGRAM_ALLOWED_CHAT_ID", "0", NOT_PRIVATE),
    ("TELEGRAM_ALLOWED_CHAT_ID", "-1", NOT_PRIVATE),
    ("TELEGRAM_ALLOWED_CHAT_ID", "-987654321", NOT_PRIVATE),
    ("TELEGRAM_ALLOWED_CHAT_ID", "-1001234567890", NOT_PRIVATE),
    ("TELEGRAM_ALLOWED_CHAT_ID", "98765x4321", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "987654321.0", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "987_654_321", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "+987654321", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", " 987654321", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "987654321 ", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "0987654321", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "９８７６５４３２１", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "9.87654321e8", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "0x3ade68b1", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "@my_channel", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "true", NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "9" * 20, NOT_INTEGER),
    ("TELEGRAM_ALLOWED_CHAT_ID", "--987654321", NOT_INTEGER),
]


@pytest.mark.parametrize(("name", "value", "fragment"), REJECTED)
def test_value_outside_the_format_is_refused_without_echoing_it(
    name: str, value: str, fragment: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)

    error = load_error(write_env(tmp_path))

    (issue,) = error.issues
    assert issue.startswith(f"{name}: {fragment}")
    assert error.__cause__ is None
    assert error.__context__ is None
    if len(value.strip()) >= 6:  # shorter ones ("0", "-1", "true") occur in any text
        assert value.strip() not in rendered(error)


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("TELEGRAM_BOT_TOKEN", "not-a-bot-token-LEAKME", BAD_FORMAT),
        ("TELEGRAM_ALLOWED_CHAT_ID", "-1001234567890", NOT_PRIVATE),
        ("TV_WEBHOOK_TOKEN", "short-LEAKME-token", BAD_FORMAT),
        ("PROPBOT_PIN", "12345", BAD_FORMAT),
    ],
)
def test_format_rules_apply_to_values_read_from_the_file(
    name: str, value: str, fragment: str, tmp_path: Path
) -> None:
    error = load_error(write_env(tmp_path, VALID | {name: value}))

    (issue,) = error.issues
    assert issue.startswith(f"{name}: {fragment}")


def test_webhook_token_issue_tells_how_to_generate_one(tmp_path: Path) -> None:
    error = load_error(write_env(tmp_path, VALID | {"TV_WEBHOOK_TOKEN": "tooshort"}))

    assert 'python3 -c "import secrets; print(secrets.token_urlsafe(32))"' in error.issues[0]


def test_every_problem_is_reported_at_once_in_field_order(tmp_path: Path) -> None:
    path = write_env(
        tmp_path,
        {
            "PROPBOT_PIN": "12ab",
            "TELEGRAM_ALLOWED_CHAT_ID": "-1001234567890",
            "TELEGRAM_BOT_TOKEN": "",
        },
    )

    error = load_error(path)

    assert [issue.split(": ")[0] for issue in error.issues] == list(ENV_NAMES)
    assert EMPTY in error.issues[0]
    assert NOT_PRIVATE in error.issues[1]
    assert MISSING in error.issues[2]
    assert BAD_FORMAT in error.issues[3]
    assert str(error) == "\n".join(error.issues)


# -------------------------------------------------------------- nothing leaks a value

SHORT_WEBHOOK_TOKEN = "LEAKME-short-webhook-token"


def test_too_short_webhook_token_appears_nowhere_in_the_error(tmp_path: Path) -> None:
    path = write_env(tmp_path, VALID | {"TV_WEBHOOK_TOKEN": SHORT_WEBHOOK_TOKEN})

    error = load_error(path)

    assert SHORT_WEBHOOK_TOKEN not in str(error)
    assert SHORT_WEBHOOK_TOKEN not in repr(error)
    assert SHORT_WEBHOOK_TOKEN not in repr(error.args)
    assert all(SHORT_WEBHOOK_TOKEN not in issue for issue in error.issues)
    # Nothing chained: pydantic's own error keeps the raw input in errors() and json().
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is False
    assert "LEAKME" not in rendered(error)
    for value in VALID.values():
        assert value not in rendered(error)


def test_logged_settings_error_carries_no_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_env(
        tmp_path,
        VALID | {"TV_WEBHOOK_TOKEN": SHORT_WEBHOOK_TOKEN, "PROPBOT_PIN": "LEAKME-pin"},
    )

    try:
        load_settings(path)
    except SettingsError:
        logging.getLogger("propbot.test").exception("settings_failed")

    assert "SettingsError" in caplog.text
    assert "TV_WEBHOOK_TOKEN: biçim uymuyor" in caplog.text
    assert "LEAKME" not in caplog.text
    assert BOT_TOKEN not in caplog.text


def test_settings_object_never_prints_its_secrets(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path))

    printed = "\n".join(
        [
            repr(settings),
            str(settings),
            f"{settings}",
            repr(settings.model_dump()),
            settings.model_dump_json(),
            repr(settings.tv_webhook_token),
            str(settings.propbot_pin),
        ]
    )

    for secret in (BOT_TOKEN, WEBHOOK_TOKEN, PIN):
        assert secret not in printed
    assert CHAT_ID in repr(settings)  # the chat id is not a secret


def test_settings_are_immutable(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path))

    with pytest.raises(ValidationError):
        settings.telegram_allowed_chat_id = 1


def test_pydantic_settings_debug_dump_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # With this variable pydantic-settings logs the raw content of every source.
    monkeypatch.setenv("PYDANTIC_SETTINGS_DEBUG", "1")
    path = write_env(tmp_path)

    with caplog.at_level(logging.DEBUG):
        load_settings(path)

    assert "Loading env file" in caplog.text  # the debug mode really was active
    for secret in (BOT_TOKEN, WEBHOOK_TOKEN, PIN):
        assert secret not in caplog.text


# ------------------------------------------------------------ secrets pairwise different


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            {"TV_WEBHOOK_TOKEN": BOT_TOKEN},
            "TELEGRAM_BOT_TOKEN ve TV_WEBHOOK_TOKEN aynı değer olamaz",
        ),
        (
            {"PROPBOT_PIN": BOT_TOKEN},
            "TELEGRAM_BOT_TOKEN ve PROPBOT_PIN aynı değer olamaz",
        ),
        (
            {"PROPBOT_PIN": WEBHOOK_TOKEN},
            "TV_WEBHOOK_TOKEN ve PROPBOT_PIN aynı değer olamaz",
        ),
    ],
    ids=["bot=webhook", "bot=pin", "webhook=pin"],
)
def test_two_identical_secrets_are_refused(
    values: dict[str, str], expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real formats cannot collide (colon, length), so the formats are opened up here to
    # reach the rule that guards a future change of a pattern.
    anything = re.compile(r".+")
    monkeypatch.setattr(settings_module, "_WEBHOOK_TOKEN", anything)
    monkeypatch.setattr(settings_module, "_PIN", anything)

    error = load_error(write_env(tmp_path, VALID | values))

    assert error.issues == (expected,)
    assert BOT_TOKEN not in rendered(error)
    assert WEBHOOK_TOKEN not in rendered(error)


@pytest.mark.parametrize(
    ("name", "other"),
    [
        ("TV_WEBHOOK_TOKEN", BOT_TOKEN),
        ("TV_WEBHOOK_TOKEN", PIN),
        ("PROPBOT_PIN", BOT_TOKEN),
        ("PROPBOT_PIN", WEBHOOK_TOKEN),
        ("TELEGRAM_BOT_TOKEN", WEBHOOK_TOKEN),
        ("TELEGRAM_BOT_TOKEN", PIN),
    ],
)
def test_real_formats_already_keep_the_secrets_apart(name: str, other: str, tmp_path: Path) -> None:
    error = load_error(write_env(tmp_path, VALID | {name: other}))

    assert error.issues[0].startswith(f"{name}: {BAD_FORMAT}")


# ------------------------------------------------------------------ direct construction


def test_direct_construction_takes_keyword_values() -> None:
    settings = Settings(
        telegram_bot_token=BOT_TOKEN,
        telegram_allowed_chat_id=int(CHAT_ID),
        tv_webhook_token=SecretStr(WEBHOOK_TOKEN),
        propbot_pin=PIN,
    )

    assert settings.telegram_allowed_chat_id == int(CHAT_ID)
    assert settings.tv_webhook_token.get_secret_value() == WEBHOOK_TOKEN


@pytest.mark.parametrize("chat_id", [True, False, 1.0, 987654321.5, None, b"987654321", [1]])
def test_chat_id_keyword_must_be_a_real_integer(chat_id: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            telegram_bot_token=BOT_TOKEN,
            telegram_allowed_chat_id=chat_id,
            tv_webhook_token=WEBHOOK_TOKEN,
            propbot_pin=PIN,
        )

    assert [error["loc"] for error in excinfo.value.errors()] == [("telegram_allowed_chat_id",)]


def test_validation_error_of_direct_construction_hides_the_input() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            telegram_bot_token=BOT_TOKEN,
            telegram_allowed_chat_id=int(CHAT_ID),
            tv_webhook_token=SHORT_WEBHOOK_TOKEN,
            propbot_pin=PIN,
        )

    assert "LEAKME" not in rendered(excinfo.value)
    assert BOT_TOKEN not in rendered(excinfo.value)


# ------------------------------------------------------------------------ .env.example

ENV_EXAMPLE = REPO_ROOT / ".env.example"


def example_entries() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in ENV_EXAMPLE.read_text("utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            entries.append((key, value))
    return entries


def test_env_example_lists_exactly_the_four_keys_without_values() -> None:
    assert example_entries() == [(name, "") for name in ENV_NAMES]


def test_env_example_explains_permissions_and_token_generation() -> None:
    text = ENV_EXAMPLE.read_text("utf-8")

    assert "chmod 600 .env" in text
    assert 'python3 -c "import secrets; print(secrets.token_urlsafe(32))"' in text


def test_unfilled_copy_of_env_example_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "copy.env"
    path.write_bytes(ENV_EXAMPLE.read_bytes())
    path.chmod(0o600)

    assert load_error(path).issues == tuple(f"{name}: {EMPTY}" for name in ENV_NAMES)


# ---------------------------------------------------------------------------- the error


def test_settings_error_keeps_issues_as_a_tuple_and_joins_them() -> None:
    error = SettingsError(issue for issue in ["birinci", "ikinci"])

    assert error.issues == ("birinci", "ikinci")
    assert str(error) == "birinci\nikinci"
