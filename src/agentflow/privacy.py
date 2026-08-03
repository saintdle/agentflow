"""Small, conservative privacy gates for local observability records.

Observability data is deliberately metadata-only.  This module is shared by the
audit and knowledge-index implementations so a caller cannot accidentally turn
an index or an audit trail into a transcript archive.
"""

from __future__ import annotations

import re
from typing import Any


class PrivacyError(ValueError):
    """Raised when a value crosses the metadata-only privacy boundary."""


SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\b\s*[:=]"),
    re.compile(r"(?i)\b(?:aws_)?secret_access_key\b\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{16,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|glpat|xox[baprs]|npm|pypi)[-_][A-Za-z0-9_-]{12,}\b", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.I),
    re.compile(r"\b(?:A3T|AKIA|ASIA|AGPA|AIDA|ANPA|ANVA|AROA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
TRANSCRIPT_PATTERNS = (
    re.compile(r"(?im)^\s*(?:user|assistant|system|developer|tool)\s*:"),
    re.compile(r"(?i)\b(?:begin|end) (?:transcript|raw log|system prompt)\b"),
    re.compile(r"(?i)\bignore (?:all |the )?(?:previous|prior|above) instructions\b"),
    re.compile(r"(?i)\b(?:system|developer) (?:prompt|message|instructions?)\b"),
    re.compile(r"(?i)\b(?:raw\s+)?(?:prompt|transcript|response)\b"),
    re.compile(r"(?i)</?(?:system|developer|assistant|tool)(?:\s|>)"),
    re.compile(r"```"),
)
FORBIDDEN_KEYS = frozenset(
    {
        "messages", "prompt", "prompts", "response", "responses", "reasoning",
        "thinking", "tools", "tool_inputs", "tool_results", "transcript", "logs",
    }
)


def unsafe_text(value: Any) -> bool:
    """Return whether *value* resembles a secret or unbounded provider content."""
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in SECRET_PATTERNS + TRANSCRIPT_PATTERNS)


def require_safe_text(value: Any, field: str, *, limit: int = 2_000, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PrivacyError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise PrivacyError(f"{field} exceeds the {limit}-character metadata limit")
    if any(ord(char) < 32 and char not in "\n\t" or ord(char) == 127 for char in value):
        raise PrivacyError(f"{field} contains control characters")
    if not multiline and "\n" in value:
        raise PrivacyError(f"{field} may not contain multiline content")
    if unsafe_text(value):
        raise PrivacyError(f"{field} looks like a secret, prompt, or transcript")
    return value


def require_safe_mapping(value: Any, field: str = "metadata", *, limit: int = 2_000) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PrivacyError(f"{field} must be an object")
    for key, item in value.items():
        lowered_key = key.lower() if isinstance(key, str) else ""
        if not isinstance(key, str) or lowered_key in FORBIDDEN_KEYS or any(token in lowered_key for token in ("prompt", "transcript", "response", "reasoning", "tool_input", "tool_result")):
            raise PrivacyError(f"{field} contains a forbidden content field")
        if isinstance(item, str):
            require_safe_text(item, f"{field}.{key}", limit=limit)
        elif isinstance(item, (dict, list, tuple)):
            # Nested evidence is allowed only as bounded metadata; recurse through
            # dicts and reject strings in sequences that could hide transcripts.
            if isinstance(item, dict):
                require_safe_mapping(item, f"{field}.{key}", limit=limit)
            else:
                for index, nested in enumerate(item):
                    if isinstance(nested, str):
                        require_safe_text(nested, f"{field}.{key}[{index}]", limit=limit)
                    elif isinstance(nested, dict):
                        require_safe_mapping(nested, f"{field}.{key}[{index}]", limit=limit)
                    elif not isinstance(nested, (int, float, bool)) and nested is not None:
                        raise PrivacyError(f"{field}.{key}[{index}] is not metadata")
        elif not isinstance(item, (int, float, bool)) and item is not None:
            raise PrivacyError(f"{field}.{key} is not metadata")
    return dict(value)


def assert_safe(value: Any, field: str = "value") -> Any:
    """Validate a JSON-like metadata value and return it unchanged."""
    if isinstance(value, str):
        return require_safe_text(value, field)
    if isinstance(value, dict):
        return require_safe_mapping(value, field)
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_safe(item, f"{field}[{index}]")
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise PrivacyError(f"{field} is not metadata")
