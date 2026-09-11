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


MINIMUM_TOTAL = Decimal("0.999")
MAXIMUM_TOTAL = Decimal("1.001")


def validate_allocations(allocations: Sequence[Allocation]) -> tuple[Allocation, ...]:
    """Return normalized allocations only when the public contract is complete.

    This validation deliberately lives below the CLI. Python callers and proposal files
    are just as capable of reaching a sink, so every route must enforce the same
    invariants before an approval id is accepted.
    """
    normalized = []
    references = set()
    for allocation in allocations:
        if not isinstance(allocation, Allocation):
            raise AllocationError("allocations must contain Allocation values")
        reference = allocation.reference
        if (
            not isinstance(reference, str)
            or not reference
            or reference != reference.strip()
            or "=" in reference
            or any(ord(character) < 32 for character in reference)
        ):
            raise AllocationError(
                "allocation reference must be a non-empty, single-line value "
                "without surrounding whitespace or '='"
            )
        if reference in references:
            raise AllocationError(f"reference {reference!r} occurs more than once")
        references.add(reference)
        try:
            fraction = Decimal(str(allocation.fraction))
        except (InvalidOperation, ValueError) as error:
            raise AllocationError(
                f"fraction for {reference!r} is not a decimal number"
            ) from error
        if not fraction.is_finite():
            raise AllocationError(f"fraction for {reference!r} must be finite")
        if fraction < 0:
            raise AllocationError(f"fraction for {reference!r} must not be negative")
        if fraction > MAXIMUM_TOTAL:
            raise AllocationError(f"fraction for {reference!r} must not exceed 100%")
        normalized.append(Allocation(reference, fraction))

    if not normalized:
        raise AllocationError("at least one allocation is required")
    total = sum((allocation.fraction for allocation in normalized), Decimal("0"))
    if not MINIMUM_TOTAL <= total <= MAXIMUM_TOTAL:
        raise AllocationError(f"allocation totals {total}, expected 100% or 1.0")
    return tuple(normalized)


def parse_allocations(arguments: Iterable[str]) -> tuple[Allocation, ...]:
    raw = []
    for argument in arguments:
        reference, separator, value = argument.partition("=")
        if not separator or not reference or not value:
            raise AllocationError(f"expected <reference>=<share>, got {argument!r}")
        try:
            share = Decimal(value.rstrip("%").replace(",", "."))
        except InvalidOperation as error:
            raise AllocationError(f"share for {reference!r} is not a number: {value}") from error
        if not share.is_finite():
            raise AllocationError(f"share for {reference!r} must be finite")
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
    return validate_allocations(
        tuple(Allocation(reference, share / scale) for reference, share in raw)
    )


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
        if not isinstance(session_id, str) or not session_id or session_id != session_id.strip():
            raise AllocationError("session id must be a non-empty value without whitespace")
        frozen = validate_allocations(tuple(allocations))
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
        self.validate()
        path.write_text(json.dumps(self.as_dict(), indent=2) + "\n")

    def validate(self) -> None:
        if not isinstance(self.session, str) or not self.session or self.session != self.session.strip():
            raise AllocationError("session id must be a non-empty value without whitespace")
        validate_allocations(self.allocations)
        if not isinstance(self.created_at, str) or not self.created_at:
            raise AllocationError("proposal created_at must be a non-empty string")
        if _approval_id(self.session, self.allocations) != self.approval_id:
            raise AllocationError("proposal contents do not match its approval id")

    @classmethod
    def read(cls, path: Path) -> "Proposal":
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise AllocationError(f"cannot read proposal {path}: {error}") from error
        if not isinstance(data, dict) or data.get("schema") != "meq-proposal-v1":
            raise AllocationError("unsupported proposal schema")
        try:
            raw_allocations = data["allocations"]
            if not isinstance(raw_allocations, list):
                raise TypeError("allocations is not a list")
            allocations = tuple(
                Allocation(item["reference"], Decimal(str(item["fraction"])))
                for item in raw_allocations
            )
            proposal = cls(
                session=data["session"],
                allocations=allocations,
                approval_id=data["approval_id"],
                created_at=data["created_at"],
            )
            proposal.validate()
            return proposal
        except (KeyError, TypeError, InvalidOperation, ValueError) as error:
            if isinstance(error, AllocationError):
                raise
            raise AllocationError(f"invalid proposal shape: {error}") from error
