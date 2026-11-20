"""Reads the input declarations of a Pine script: `name = input.<kind>(<default>, "<title>", ...)`.

The Python ports must start from the same defaults as the Pine scripts they mirror
(CLAUDE.md: input defaults stay as they are; ADR-0008 item 9). Pine cannot be run here,
so the tests of each port compare its parameter defaults with the script's source text.
Only the forms the scripts in `pine/` actually use are understood; anything else raises,
so a new form cannot slip through unchecked.
"""

import datetime as dt
import re
from dataclasses import dataclass
from typing import Final

from support.paths import REPO_ROOT

PINE_DIR: Final = REPO_ROOT / "pine"

type PineValue = bool | int | float | str | dt.datetime

_DECLARATION: Final = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*input\.(?P<kind>[a-z]+)\((?P<args>.*)$"
)
_STRING: Final = re.compile(r'"((?:[^"\\]|\\.)*)"')
_NUMBER: Final = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_TIMESTAMP: Final = re.compile(r'timestamp\("([^"]+)"\)')
_TIMESTAMP_FORMAT: Final = "%d %b %Y %H:%M %z"


class PineInputError(ValueError):
    """An input declaration is written in a form this reader does not know."""


@dataclass(frozen=True, slots=True)
class PineInput:
    name: str
    kind: str  # "bool", "int", "float", "string", "time", "timeframe"
    default: PineValue
    title: str


def pine_inputs(file_name: str) -> dict[str, PineInput]:
    """Every input of `pine/<file_name>`, by the variable it is assigned to."""
    inputs: dict[str, PineInput] = {}
    for number, line in enumerate(
        (PINE_DIR / file_name).read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = _DECLARATION.match(line)
        if match is None:
            if "input." in line and not line.lstrip().startswith("//"):
                raise PineInputError(f"{file_name}:{number}: unrecognised input line")
            continue
        name = match["name"]
        if name in inputs:
            raise PineInputError(f"{file_name}:{number}: input {name} is declared twice")
        default, rest = _default(match["kind"], match["args"], f"{file_name}:{number}")
        title = _STRING.search(rest)
        inputs[name] = PineInput(
            name=name,
            kind=match["kind"],
            default=default,
            title=title.group(1) if title is not None else "",
        )
    return inputs


def pine_defaults(file_name: str) -> dict[str, PineValue]:
    return {name: declared.default for name, declared in pine_inputs(file_name).items()}


def _default(kind: str, args: str, where: str) -> tuple[PineValue, str]:
    """Return the first argument as a Python value and the text after it."""
    if kind == "bool":
        for text, value in (("true", True), ("false", False)):
            if re.match(rf"{text}\s*[,)]", args):
                return value, args[len(text) :]
    elif kind in ("int", "float"):
        number = _NUMBER.match(args)
        if number is not None and re.match(r"\s*[,)]", args[number.end() :]):
            text = number.group(0)
            if kind == "int":
                if "." in text:
                    raise PineInputError(f"{where}: input.int with a fractional default")
                return int(text), args[number.end() :]
            return float(text), args[number.end() :]
    elif kind in ("string", "timeframe"):
        string = _STRING.match(args)
        if string is not None:
            return string.group(1), args[string.end() :]
    elif kind == "time":
        stamp = _TIMESTAMP.match(args)
        if stamp is not None:
            moment = dt.datetime.strptime(stamp.group(1), _TIMESTAMP_FORMAT)
            return moment.astimezone(dt.UTC), args[stamp.end() :]
    raise PineInputError(f"{where}: cannot read the default of input.{kind}")
