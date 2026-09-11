"""Immutable allocation proposals and explicit approval."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence


class AllocationError(ValueError):
    """Raised before a proposal or sink invocation can partially succeed."""


@dataclass(frozen=True)
class Allocation:
    reference: str
    fraction: Decimal

    def as_dict(self) -> dict:
        return {"reference": self.reference, "fraction": float(self.fraction)}


def parse_allocations(arguments: Iterable[str]) -> tuple[Allocation, ...]:
    raw = []
    references = set()
    for argument in arguments:
        reference, separator, value = argument.partition("=")
        if not separator or not reference or not value:
            raise AllocationError(f"expected <reference>=<share>, got {argument!r}")
        if reference in references:
            raise AllocationError(f"reference {reference!r} occurs more than once")
        references.add(reference)
        try:
            share = Decimal(value.rstrip("%").replace(",", "."))
        except InvalidOperation as error:
            raise AllocationError(f"share for {reference!r} is not a number: {value}") from error
        if share < 0:
            raise AllocationError(f"share for {reference!r} must not be negative")
        raw.append((reference, share))
    if not raw:
        raise AllocationError("at least one allocation is required")
    total = sum((share for _, share in raw), Decimal("0"))
    if Decimal("99.9") <= total <= Decimal("100.1"):
        scale = Decimal("100")
    elif Decimal("0.999") <= total <= Decimal("1.001"):
        scale = Decimal("1")
    else:
        raise AllocationError(f"allocation totals {total}, expected 100% or 1.0")
    return tuple(Allocation(reference, share / scale) for reference, share in raw)


def _approval_id(session_id: str, allocations: Sequence[Allocation]) -> str:
    canonical = json.dumps(
        {
            "session": session_id,
            "allocations": [allocation.as_dict() for allocation in allocations],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Proposal:
    session: str
    allocations: tuple[Allocation, ...]
    approval_id: str
    created_at: str

    @classmethod
    def create(cls, session_id: str, allocations: Sequence[Allocation]) -> "Proposal":
        frozen = tuple(allocations)
        return cls(
            session=session_id,
            allocations=frozen,
            approval_id=_approval_id(session_id, frozen),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def as_dict(self) -> dict:
        return {
            "schema": "meq-proposal-v1",
            "session": self.session,
            "allocations": [allocation.as_dict() for allocation in self.allocations],
            "approval_id": self.approval_id,
            "created_at": self.created_at,
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.as_dict(), indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> "Proposal":
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise AllocationError(f"cannot read proposal {path}: {error}") from error
        if data.get("schema") != "meq-proposal-v1":
            raise AllocationError("unsupported proposal schema")
        allocations = tuple(
            Allocation(item["reference"], Decimal(str(item["fraction"])))
            for item in data.get("allocations", [])
        )
        expected = _approval_id(data.get("session", ""), allocations)
        if expected != data.get("approval_id"):
            raise AllocationError("proposal contents do not match its approval id")
        return cls(
            session=data["session"],
            allocations=allocations,
            approval_id=expected,
            created_at=data["created_at"],
        )
