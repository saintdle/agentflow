"""Deterministic quality checks for opt-in reader-facing prose artifacts.

The module never rewrites a file and never calls a model.  It decides whether
an artifact needs a bounded editing pass and verifies that an edited sibling
preserves machine-checkable technical material from the original.
"""

from __future__ import annotations

from collections import Counter
import dataclasses
from pathlib import Path
import re
from typing import Any, Iterable


SCHEMA = "agentflow.prose-report@1"
VERIFY_SCHEMA = "agentflow.prose-verification@1"


class ProseError(ValueError):
    """Raised for an unsafe path, unsupported profile, or malformed text."""


@dataclasses.dataclass(frozen=True)
class ProseProfile:
    name: str
    max_sentence_words: int
    max_paragraph_sentences: int
    max_paragraph_words: int
    filler_phrases: tuple[str, ...]


COMMON_FILLER = (
    "it is worth noting",
    "it is important to note",
    "this is worth highlighting",
    "the key takeaway is",
    "at the end of the day",
    "in order to",
    "the important thing to understand is",
    "this provides a robust foundation",
    "this is a well-defined seam",
)


PROFILES: dict[str, ProseProfile] = {
    "technical-blog": ProseProfile(
        name="technical-blog",
        max_sentence_words=32,
        max_paragraph_sentences=5,
        max_paragraph_words=130,
        filler_phrases=COMMON_FILLER,
    ),
    "instruqt": ProseProfile(
        name="instruqt",
        max_sentence_words=28,
        max_paragraph_sentences=4,
        max_paragraph_words=100,
        filler_phrases=COMMON_FILLER,
    ),
}


@dataclasses.dataclass(frozen=True)
class ProseFinding:
    code: str
    severity: str
    line: int
    message: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ProseReport:
    path: str
    profile: str
    words: int
    prose_words: int
    findings: tuple[ProseFinding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "path": self.path,
            "profile": self.profile,
            "passed": self.passed,
            "words": self.words,
            "prose_words": self.prose_words,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclasses.dataclass(frozen=True)
class VerificationReport:
    source: str
    edited: str
    profile: str
    source_prose_words: int
    edited_prose_words: int
    findings: tuple[ProseFinding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": VERIFY_SCHEMA,
            "source": self.source,
            "edited": self.edited,
            "profile": self.profile,
            "passed": self.passed,
            "source_prose_words": self.source_prose_words,
            "edited_prose_words": self.edited_prose_words,
            "findings": [finding.to_dict() for finding in self.findings],
        }


_WORD = re.compile(r"\b[\w][\w'’.-]*\b", re.UNICODE)
_SENTENCE = re.compile(r"(?<=[.!?])(?:[\"'’”)]*)\s+")
_INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_URL = re.compile(r"https?://[^\s)>\]}]+")
_MARKDOWN_DEST = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_NUMBER = re.compile(r"(?<![\w])\d+(?:[.,:]\d+)*(?:%|[A-Za-z]+)?(?![\w])")
_PATH = re.compile(
    r"(?<![\w])(?:[.~]?/[A-Za-z0-9._@%+~/-]+|(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._@%+~-]+)(?![\w])"
)
_STRUCTURE = re.compile(r"^\s*(?:#{1,6}\s|>|\||---\s*$)")
_LIST = re.compile(r"^\s*(?:[-+*]\s|\d+[.)]\s)")


def profile(name: str) -> ProseProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ProseError(f"unsupported prose profile: {name}") from exc


def read_markdown(path: Path) -> str:
    """Read one regular UTF-8 Markdown file without following a final symlink."""

    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ProseError(f"prose input must be a regular file: {candidate}")
    if candidate.suffix.lower() not in {".md", ".markdown"}:
        raise ProseError("prose input must be Markdown (.md or .markdown)")
    if candidate.stat().st_size > 2 * 1024 * 1024:
        raise ProseError("prose input exceeds the 2 MiB limit")
    try:
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProseError(f"cannot read UTF-8 prose input: {candidate}") from exc
    if "\x00" in text:
        raise ProseError("prose input contains a NUL byte")
    return text


def _fenced_blocks(text: str) -> tuple[str, ...]:
    blocks: list[str] = []
    current: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        marker = match.group(1) if match else ""
        if not fence and marker:
            fence = marker[0]
            current = [line]
        elif fence:
            current.append(line)
            if marker and marker[0] == fence:
                blocks.append("".join(current))
                current = []
                fence = ""
    if current:
        blocks.append("".join(current))
    return tuple(blocks)


def prose_lines(text: str) -> tuple[tuple[int, str], ...]:
    """Return prose outside YAML frontmatter and fenced code with line numbers."""

    selected: list[tuple[int, str]] = []
    in_fence = False
    fence_char = ""
    in_frontmatter = text.startswith("---\n") or text.startswith("---\r\n")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if in_frontmatter:
            if number > 1 and stripped == "---":
                in_frontmatter = False
            continue
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            char = marker.group(1)[0]
            if not in_fence:
                in_fence, fence_char = True, char
            elif char == fence_char:
                in_fence, fence_char = False, ""
            continue
        if not in_fence:
            selected.append((number, line))
    return tuple(selected)


def _plain_line(line: str) -> str:
    line = _INLINE_CODE.sub(" ", line)
    line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"<https?://[^>]+>", " ", line)
    line = re.sub(r"^\s*(?:#{1,6}\s+|[-+*]\s+|\d+[.)]\s+|>\s*)", "", line)
    return line.strip()


def _paragraphs(lines: Iterable[tuple[int, str]]) -> tuple[tuple[int, str], ...]:
    paragraphs: list[tuple[int, str]] = []
    current: list[str] = []
    start = 0
    for number, raw in lines:
        if not raw.strip() or _STRUCTURE.match(raw):
            if current:
                paragraphs.append((start, " ".join(current)))
                current, start = [], 0
            continue
        plain = _plain_line(raw)
        if not plain:
            continue
        if _LIST.match(raw):
            if current:
                paragraphs.append((start, " ".join(current)))
                current, start = [], 0
            paragraphs.append((number, plain))
            continue
        if not current:
            start = number
        current.append(plain)
    if current:
        paragraphs.append((start, " ".join(current)))
    return tuple(paragraphs)


def _sentences(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _SENTENCE.split(value) if part.strip())


def _normal_sentence(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())


def check_text(text: str, *, path: str, profile_name: str) -> ProseReport:
    rules = profile(profile_name)
    lines = prose_lines(text)
    paragraphs = _paragraphs(lines)
    findings: list[ProseFinding] = []
    seen_sentences: dict[str, int] = {}

    for line, paragraph in paragraphs:
        words = _WORD.findall(paragraph)
        sentences = _sentences(paragraph)
        if len(words) > rules.max_paragraph_words:
            findings.append(ProseFinding(
                "paragraph-too-long", "warning", line,
                f"paragraph has {len(words)} words; profile limit is {rules.max_paragraph_words}",
                paragraph[:160],
            ))
        if len(sentences) > rules.max_paragraph_sentences:
            findings.append(ProseFinding(
                "paragraph-too-many-sentences", "warning", line,
                f"paragraph has {len(sentences)} sentences; profile limit is {rules.max_paragraph_sentences}",
                paragraph[:160],
            ))
        for sentence in sentences:
            sentence_words = _WORD.findall(sentence)
            if len(sentence_words) > rules.max_sentence_words:
                findings.append(ProseFinding(
                    "sentence-too-long", "warning", line,
                    f"sentence has {len(sentence_words)} words; profile limit is {rules.max_sentence_words}",
                    sentence[:160],
                ))
            normalized = _normal_sentence(sentence)
            if len(sentence_words) >= 8:
                if normalized in seen_sentences:
                    findings.append(ProseFinding(
                        "repeated-sentence", "warning", line,
                        f"sentence repeats prose first seen on line {seen_sentences[normalized]}",
                        sentence[:160],
                    ))
                else:
                    seen_sentences[normalized] = line

    for number, raw in lines:
        plain = _plain_line(raw).casefold()
        for phrase in rules.filler_phrases:
            if phrase in plain:
                findings.append(ProseFinding(
                    "filler-phrase", "advice", number,
                    f"remove or replace the filler phrase {phrase!r}",
                    raw.strip()[:160],
                ))

    prose = "\n".join(_plain_line(line) for _, line in lines)
    return ProseReport(
        path=path,
        profile=profile_name,
        words=len(_WORD.findall(text)),
        prose_words=len(_WORD.findall(prose)),
        findings=tuple(findings),
    )


def check_file(path: Path, *, profile_name: str) -> ProseReport:
    return check_text(read_markdown(path), path=str(path.resolve()), profile_name=profile_name)


def _protected_values(text: str) -> dict[str, Counter[str]]:
    without_fences = text
    for block in _fenced_blocks(text):
        without_fences = without_fences.replace(block, "", 1)
    urls = [*(_URL.findall(without_fences)), *(_MARKDOWN_DEST.findall(without_fences))]
    structure: list[str] = []
    for _line, raw in prose_lines(text):
        stripped = raw.lstrip()
        kind = ""
        if match := re.match(r"^(#{1,6})\s", stripped):
            kind = f"heading-{len(match.group(1))}"
        elif re.match(r"^[-+*]\s", stripped):
            kind = "unordered-list"
        elif re.match(r"^\d+[.)]\s", stripped):
            kind = "ordered-list"
        elif stripped.startswith(">"):
            kind = "blockquote"
        elif stripped.startswith("|"):
            kind = "table-row"
        if kind:
            structure.append(f"{len(structure)}:{kind}")
    return {
        "fenced-code": Counter(_fenced_blocks(text)),
        "inline-code": Counter(_INLINE_CODE.findall(without_fences)),
        "links": Counter(urls),
        "numbers": Counter(_NUMBER.findall(without_fences)),
        "paths": Counter(_PATH.findall(without_fences)),
        "markdown-structure": Counter(structure),
    }


def verify_texts(
    source_text: str,
    edited_text: str,
    *,
    source_path: str,
    edited_path: str,
    profile_name: str,
) -> VerificationReport:
    edited_report = check_text(edited_text, path=edited_path, profile_name=profile_name)
    source_report = check_text(source_text, path=source_path, profile_name=profile_name)
    findings = list(edited_report.findings)
    source_values = _protected_values(source_text)
    edited_values = _protected_values(edited_text)
    for kind in ("fenced-code", "inline-code", "links", "numbers", "paths", "markdown-structure"):
        missing = source_values[kind] - edited_values[kind]
        added = edited_values[kind] - source_values[kind]
        if missing or added:
            details = []
            if missing:
                details.append(f"missing {sum(missing.values())}")
            if added:
                details.append(f"added {sum(added.values())}")
            findings.append(ProseFinding(
                f"protected-{kind}-changed", "error", 0,
                f"protected {kind} changed ({', '.join(details)})",
                "review the source and edited artifact directly",
            ))
    if edited_report.prose_words > source_report.prose_words:
        findings.append(ProseFinding(
            "prose-expanded", "warning", 0,
            f"edited prose grew from {source_report.prose_words} to {edited_report.prose_words} words",
            "the plain-language pass must not expand the artifact",
        ))
    return VerificationReport(
        source=source_path,
        edited=edited_path,
        profile=profile_name,
        source_prose_words=source_report.prose_words,
        edited_prose_words=edited_report.prose_words,
        findings=tuple(findings),
    )


def verify_files(source: Path, edited: Path, *, profile_name: str) -> VerificationReport:
    if source.expanduser().resolve() == edited.expanduser().resolve():
        raise ProseError("source and edited paths must be different")
    return verify_texts(
        read_markdown(source),
        read_markdown(edited),
        source_path=str(source.expanduser().resolve()),
        edited_path=str(edited.expanduser().resolve()),
        profile_name=profile_name,
    )


__all__ = [
    "PROFILES",
    "ProseError",
    "ProseFinding",
    "ProseProfile",
    "ProseReport",
    "VerificationReport",
    "check_file",
    "check_text",
    "profile",
    "prose_lines",
    "read_markdown",
    "verify_files",
    "verify_texts",
]
