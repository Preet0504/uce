"""Deterministic change-proposal gate.

Decides allow/warn/block for a proposed code change by comparing a caller's
*declared* plan (files it intends to touch, requirements it believes apply)
against the graph's *actual* blast radius and RBAC decision. No LLM is
involved anywhere in this module — every input is either a graph traversal
result or an explicit caller-supplied value.

The decision logic mirrors the protocol validated in
research/icmla_workshop/run_multi_repo_enforcement.py across 4 real repos
(100% catch rate, 0.9% false-gate rate): a gate fires when the declared plan
misses files the graph says are affected, misses a requirement the graph
says is violated, or when RBAC denies the operation.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable


def _normalize_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip().strip("/")


@dataclass(frozen=True)
class GateEvaluation:
    decision: str  # "allow" | "block" | "warn"
    strict: bool
    rbac_allowed: bool
    rbac_reason: str
    rbac_matched_rule_id: str | None
    declared_files: tuple[str, ...]
    actual_files: tuple[str, ...]
    missed_files: tuple[str, ...]
    declared_requirements: tuple[str, ...]
    violated_requirements: tuple[str, ...]
    silent_requirements: tuple[str, ...]
    enforced_policies: tuple[str, ...]

    @property
    def gate_fires(self) -> bool:
        return self.decision != "allow"


def evaluate_gate(
    *,
    rbac_allowed: bool,
    rbac_reason: str = "",
    rbac_matched_rule_id: str | None = None,
    declared_files: Iterable[str] = (),
    actual_files: Iterable[str] = (),
    declared_requirements: Iterable[str] = (),
    violated_requirements: Iterable[str] = (),
    enforced_policies: Iterable[str] = (),
    strict: bool = True,
) -> GateEvaluation:
    declared_files_set = {_normalize_path(f) for f in declared_files if f}
    actual_files_set = {_normalize_path(f) for f in actual_files if f}
    missed_files = tuple(sorted(actual_files_set - declared_files_set))

    declared_reqs_set = {str(r).strip() for r in declared_requirements if r}
    violated_reqs_set = {str(r).strip() for r in violated_requirements if r}
    silent_requirements = tuple(sorted(violated_reqs_set - declared_reqs_set))

    if not rbac_allowed:
        decision = "block"
    elif missed_files or silent_requirements:
        decision = "block" if strict else "warn"
    else:
        decision = "allow"

    return GateEvaluation(
        decision=decision,
        strict=strict,
        rbac_allowed=rbac_allowed,
        rbac_reason=rbac_reason,
        rbac_matched_rule_id=rbac_matched_rule_id,
        declared_files=tuple(sorted(declared_files_set)),
        actual_files=tuple(sorted(actual_files_set)),
        missed_files=missed_files,
        declared_requirements=tuple(sorted(declared_reqs_set)),
        violated_requirements=tuple(sorted(violated_reqs_set)),
        silent_requirements=silent_requirements,
        enforced_policies=tuple(sorted({str(p).strip() for p in enforced_policies if p})),
    )


@dataclass
class _GateTokenRecord:
    operation: str
    declared_files: frozenset[str]
    expires_at: float
    consumed: set[str] = field(default_factory=set)


class GateTokenStore:
    """In-process store binding a gate_token to the exact plan propose_change allowed.

    A token is minted only when evaluate_gate() returns decision == "allow". A mutation
    tool (write_file/delete_file) must present a valid, unexpired, matching, not-yet-consumed
    token for the exact (operation, path) it is about to execute — this is what makes calling
    the gate mandatory rather than advisory: there is no code path to a filesystem mutation
    that does not first pass through a successful propose_change() call.
    """

    def __init__(self, ttl_seconds: int = 900):
        self.ttl_seconds = max(int(ttl_seconds), 1)
        self._tokens: dict[str, _GateTokenRecord] = {}
        self._lock = threading.Lock()

    def issue(self, operation: str, declared_files: Iterable[str]) -> str:
        token = secrets.token_urlsafe(24)
        normalized = frozenset(_normalize_path(f) for f in declared_files if f)
        with self._lock:
            self._prune_expired_locked()
            self._tokens[token] = _GateTokenRecord(
                operation=operation,
                declared_files=normalized,
                expires_at=time.time() + self.ttl_seconds,
            )
        return token

    def consume(self, token: str, operation: str, file_path: str) -> tuple[bool, str]:
        """Validate and consume `token` for a single (operation, file_path). Returns (ok, error)."""
        normalized_path = _normalize_path(file_path)
        with self._lock:
            self._prune_expired_locked()
            record = self._tokens.get(token)
            if record is None:
                return False, (
                    "Invalid, expired, or already-used gate_token. "
                    "Call propose_change(...) first and use the token it returns."
                )
            if record.operation != operation:
                return False, (
                    f"gate_token was issued for operation '{record.operation}', not '{operation}'. "
                    "Call propose_change(...) again with the correct operation."
                )
            if normalized_path not in record.declared_files:
                return False, (
                    f"gate_token does not cover path '{normalized_path}'. "
                    "Call propose_change(...) with this file included in files_to_edit."
                )
            if normalized_path in record.consumed:
                return False, f"gate_token has already been used for '{normalized_path}'."

            record.consumed.add(normalized_path)
            if record.consumed >= record.declared_files:
                del self._tokens[token]
            return True, ""

    def _prune_expired_locked(self) -> None:
        now = time.time()
        expired = [tok for tok, rec in self._tokens.items() if rec.expires_at <= now]
        for tok in expired:
            del self._tokens[tok]
