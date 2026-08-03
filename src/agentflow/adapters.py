"""Fail-closed adapter promotion and bounded routing gates."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from agentflow.privacy import PrivacyError, assert_safe, require_safe_mapping, require_safe_text


class AdapterError(ValueError):
    """An adapter cannot be trusted or routed."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class AdapterManifest:
    name: str
    version: str
    timeout_seconds: float = 5.0
    fallback: str = ""
    approved: bool = False
    capabilities: tuple[str, ...] = ()
    results_hash: str = ""
    manifest_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_safe_text(self.name, "name", limit=96))
        object.__setattr__(self, "version", require_safe_text(self.version, "version", limit=80))
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise AdapterError("timeout_seconds must be greater than 0 and at most 60")
        if self.fallback:
            object.__setattr__(self, "fallback", require_safe_text(self.fallback, "fallback", limit=96))
        object.__setattr__(self, "capabilities", tuple(sorted(set(require_safe_text(item, "capability", limit=80) for item in self.capabilities))))
        expected = _digest(self.unsigned_dict())
        if self.manifest_hash and self.manifest_hash != expected:
            raise AdapterError("adapter manifest hash does not match its immutable fields")
        object.__setattr__(self, "manifest_hash", expected)

    def unsigned_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "timeout_seconds": self.timeout_seconds, "fallback": self.fallback, "approved": self.approved, "capabilities": list(self.capabilities), "results_hash": self.results_hash}

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned_dict()
        value["manifest_hash"] = self.manifest_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdapterManifest":
        if not isinstance(value, Mapping):
            raise AdapterError("adapter manifest must be an object")
        return cls(**dict(value))


@dataclasses.dataclass(frozen=True)
class BenchmarkResult:
    case_id: str
    baseline_input_tokens: int
    adapter_input_tokens: int
    baseline_correct: bool
    adapter_correct: bool
    baseline_evidence_recall: float
    adapter_evidence_recall: float
    elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.baseline_input_tokens <= 0 or self.adapter_input_tokens < 0:
            raise AdapterError("input token counts are invalid")
        for field in ("baseline_evidence_recall", "adapter_evidence_recall"):
            value = getattr(self, field)
            if not 0 <= value <= 1:
                raise AdapterError(f"{field} must be between 0 and 1")
        if self.elapsed_seconds < 0:
            raise AdapterError("elapsed_seconds cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class GateReport:
    adapter: str
    passed: bool
    correctness: bool
    evidence_recall: bool
    token_reduction: bool
    median_reduction: float
    timeout_bounded: bool
    manifest_immutable: bool
    results_immutable: bool
    unsafe_reasons: tuple[str, ...] = ()
    manifest_hash: str = ""
    results_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _result(value: BenchmarkResult | Mapping[str, Any]) -> BenchmarkResult:
    if isinstance(value, BenchmarkResult):
        return value
    data = dict(value)
    aliases = {
        "id": "case_id", "baseline_tokens": "baseline_input_tokens", "adapter_tokens": "adapter_input_tokens",
        "baseline_recall": "baseline_evidence_recall", "adapter_recall": "adapter_evidence_recall",
    }
    for source, target in aliases.items():
        if target not in data and source in data:
            data[target] = data[source]
    return BenchmarkResult(**{key: data[key] for key in ("case_id", "baseline_input_tokens", "adapter_input_tokens", "baseline_correct", "adapter_correct", "baseline_evidence_recall", "adapter_evidence_recall", "elapsed_seconds") if key in data})


def compute_results_hash(results: Iterable[BenchmarkResult | Mapping[str, Any]]) -> str:
    """Return the stable SHA-256 identity of canonical benchmark results."""
    raw_results = tuple(_result(item) for item in results)
    if not raw_results:
        raise AdapterError("at least one benchmark result is required")
    encoded = [_canonical(item.to_dict()) for item in raw_results]
    return _digest(encoded)


def evaluate(manifest: AdapterManifest | Mapping[str, Any], results: Iterable[BenchmarkResult | Mapping[str, Any]], *, max_timeout_seconds: float = 60.0) -> GateReport:
    """Evaluate promotion gates; any malformed or unproven input fails closed."""
    try:
        checked_manifest = manifest if isinstance(manifest, AdapterManifest) else AdapterManifest(**dict(manifest))
        raw_results = tuple(_result(item) for item in results)
        result_hash = compute_results_hash(raw_results)
        supplied_hash = checked_manifest.results_hash
        results_immutable = bool(supplied_hash) and supplied_hash == result_hash
        reductions = [(item.baseline_input_tokens - item.adapter_input_tokens) / item.baseline_input_tokens for item in raw_results]
        median_reduction = statistics.median(reductions)
        # Every adapter benchmark case must be correct; this is intentionally
        # stricter than a baseline no-regression requirement.
        correctness = all(item.adapter_correct for item in raw_results)
        evidence_recall = all(item.adapter_evidence_recall >= item.baseline_evidence_recall for item in raw_results)
        token_reduction = median_reduction >= 0.30
        timeout_bounded = 0 < checked_manifest.timeout_seconds <= max_timeout_seconds and bool(checked_manifest.fallback)
        reasons = []
        if not checked_manifest.approved:
            reasons.append("manifest is not explicitly approved")
        if not correctness:
            reasons.append("correctness was not preserved")
        if not evidence_recall:
            reasons.append("evidence recall was not preserved")
        if not token_reduction:
            reasons.append("median input-token reduction is below 30 percent")
        if not timeout_bounded:
            reasons.append("timeout or explicit fallback is outside the bounded routing contract")
        if not supplied_hash:
            reasons.append("benchmark results hash is required")
        elif not results_immutable:
            reasons.append("benchmark results hash changed")
        return GateReport(
            checked_manifest.name, not reasons, correctness, evidence_recall,
            token_reduction, median_reduction, timeout_bounded, True,
            results_immutable, tuple(reasons), checked_manifest.manifest_hash,
            result_hash,
        )
    except (AdapterError, KeyError, TypeError, PrivacyError) as exc:
        name = str(manifest.get("name") if isinstance(manifest, Mapping) else getattr(manifest, "name", "untrusted"))
        return GateReport(name or "untrusted", False, False, False, False, 0.0, False, False, False, (str(exc),))


promote = evaluate


class AdapterRouter:
    def __init__(self) -> None:
        self._routes: dict[str, tuple[AdapterManifest, GateReport, Callable[[Any], Any]]] = {}

    def register(self, manifest: AdapterManifest, gate: GateReport, handler: Callable[[Any], Any]) -> None:
        if (
            not gate.passed
            or gate.adapter != manifest.name
            or gate.manifest_hash != manifest.manifest_hash
            or gate.results_hash != manifest.results_hash
            or manifest.manifest_hash != _digest(manifest.unsigned_dict())
        ):
            raise AdapterError("refusing to register an unproven or tampered adapter")
        self._routes[manifest.name] = (manifest, gate, handler)

    def route(self, name: str, request: Any, *, fallback: Callable[[Any], Any] | None = None) -> Any:
        route = self._routes.get(name)
        if route is None:
            raise AdapterError(f"adapter route is not proven: {name}")
        manifest, gate, handler = route
        assert_safe(request, "request")
        try:
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                value = pool.submit(handler, request).result(timeout=manifest.timeout_seconds)
            finally:
                # A timed-out provider call must not hold up the caller. The
                # worker may finish later, but its result is never observed.
                pool.shutdown(wait=False, cancel_futures=True)
            assert_safe(value, "adapter result")
            return value
        except Exception as exc:
            if fallback is None:
                raise AdapterError(f"adapter route failed closed: {name}") from exc
            value = fallback(request)
            assert_safe(value, "fallback result")
            return value


def immutable_write(path: Path | str, value: Mapping[str, Any]) -> None:
    """Write a manifest/result bundle once; never overwrite evidence."""
    path = Path(path)
    safe_value = require_safe_mapping(dict(value), "evidence")
    encoded = json.dumps(safe_value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise AdapterError(f"immutable evidence already exists with different content: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")
