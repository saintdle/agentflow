"""Access immutable files shipped inside the Agentflow distribution."""

from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from typing import Iterator


def item(*parts: str):
    resource = files("agentflow").joinpath("resources")
    for part in parts:
        resource = resource.joinpath(part)
    return resource


@contextmanager
def path(*parts: str) -> Iterator[Path]:
    with as_file(item(*parts)) as resolved:
        yield resolved


def text(*parts: str) -> str:
    return item(*parts).read_text(encoding="utf-8")


def names(*parts: str) -> tuple[str, ...]:
    """Return deterministic direct-child names for a packaged directory."""

    return tuple(sorted(child.name for child in item(*parts).iterdir()))
