"""Normalize persisted Claude Code and Codex token usage."""

from __future__ import annotations

import glob
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple


WEIGHTS = {"inp": 1.0, "out": 5.0, "cw": 1.25, "cr": 0.1}
TOKEN_FIELDS = {
    "inp": "input_tokens",
    "cr": "cached_input_tokens",
    "cw": "cache_write_tokens",
    "out": "output_tokens",
}


class MeasurementError(ValueError):
    """Raised when a session cannot be located or measured unambiguously."""


@dataclass(frozen=True)
class SessionLocation:
    agent: str
    path: Path
    project_dir: Optional[Path] = None

    def as_dict(self) -> dict:
        result = {"agent": self.agent, "path": str(self.path)}
        if self.project_dir is not None:
            result["project_dir"] = str(self.project_dir)
        return result


@dataclass(frozen=True)
class TranscriptStore:
    claude_projects: Path
    codex_sessions: Path

    @classmethod
    def from_environment(cls) -> "TranscriptStore":
        claude = (
            os.environ.get("MEQ_CLAUDE_PROJECTS")
            or os.environ.get("PLEMP_CLAUDE_PROJECTS")
            or os.environ.get("CLAUDE_PROJECTS_DIR")
            or "~/.claude/projects"
        )
        codex = (
            os.environ.get("MEQ_CODEX_SESSIONS")
            or os.environ.get("PLEMP_CODEX_SESSIONS")
            or "~/.codex/sessions"
        )
        return cls(Path(claude).expanduser(), Path(codex).expanduser())

    def project_dir(self, workdir: os.PathLike | str) -> Path:
        escaped = re.sub(r"[/.]", "-", str(Path(workdir).resolve()))
        return self.claude_projects / escaped

    def locate(
        self,
        session_id: str,
        *,
        workdir: Optional[os.PathLike | str] = None,
        project_dir: Optional[os.PathLike | str] = None,
    ) -> SessionLocation:
        """Find exactly one persisted transcript across both supported agents.

        An explicit Claude project is authoritative. Without one, all Claude project
        directories and the Codex rollout tree are searched. Ambiguity is reported
        instead of selecting a transcript and silently dropping another one.
        """
        explicit = Path(project_dir) if project_dir else (
            self.project_dir(workdir) if workdir is not None else None
        )
        if explicit is not None:
            path = explicit / f"{session_id}.jsonl"
            if path.is_file():
                return SessionLocation("claude", path, explicit)

        claude_matches = [] if explicit is not None else [
            Path(path)
            for path in glob.glob(str(self.claude_projects / "*" / f"{session_id}.jsonl"))
        ]
        codex_matches = [
            Path(path)
            for path in glob.glob(
                str(self.codex_sessions / "**" / f"rollout-*-{session_id}.jsonl"),
                recursive=True,
            )
        ]
        matches = [
            SessionLocation("claude", path, path.parent) for path in claude_matches
        ] + [SessionLocation("codex", path) for path in codex_matches]
        if not matches:
            raise MeasurementError(f"no transcript for session {session_id}")
        if len(matches) > 1:
            paths = ", ".join(str(match.path) for match in matches)
            raise MeasurementError(
                f"session {session_id} has {len(matches)} transcripts: {paths}"
            )
        return matches[0]


def _empty_slot() -> dict:
    return {"inp": 0, "out": 0, "cw": 0, "cr": 0, "calls": 0}


def _meq(slot: dict) -> float:
    return sum(slot[key] * weight for key, weight in WEIGHTS.items()) / 1e6


def _external_slot(slot: dict) -> dict:
    result = {"meq": round(_meq(slot), 3), "calls": slot["calls"]}
    result.update({name: slot[key] for key, name in TOKEN_FIELDS.items()})
    return result


def _short(models: dict) -> str:
    parts = [
        f"{model.removeprefix('claude-')} "
        f"{format(slot['meq'], 'g').replace('.', ',')}"
        for model, slot in models.items()
        if slot["meq"] > 0
    ]
    return ", ".join(parts)


def _tally_claude(path: Path) -> dict:
    per_model = defaultdict(_empty_slot)
    with path.open(errors="ignore") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message") or {}
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            model = message.get("model") or record.get("model") or "unknown"
            slot = per_model[model]
            slot["inp"] += usage.get("input_tokens", 0) or 0
            slot["out"] += usage.get("output_tokens", 0) or 0
            slot["cw"] += usage.get("cache_creation_input_tokens", 0) or 0
            slot["cr"] += usage.get("cache_read_input_tokens", 0) or 0
            slot["calls"] += 1
    return per_model


def _result(agent: str, session_id: str, per_model: dict, *, subagents: int = 0,
            main_meq: Optional[float] = None, subagent_meq: float = 0.0) -> dict:
    models = {
        model: _external_slot(slot) for model, slot in sorted(per_model.items())
    }
    total = round(sum(item["meq"] for item in models.values()), 3)
    return {
        "agent": agent,
        "session": session_id,
        "meq": total,
        "meq_hoofdsessie": round(total if main_meq is None else main_meq, 3),
        "meq_subagents": round(subagent_meq, 3),
        "subagents": subagents,
        "modellen": models,
        "modellen_kort": _short(models),
    }


def measure_claude(location: SessionLocation, session_id: str) -> dict:
    project_dir = location.project_dir or location.path.parent
    subagent_paths = sorted(
        (project_dir / session_id / "subagents").glob("*.jsonl")
    )
    per_model = defaultdict(_empty_slot)
    main_meq = subagent_meq = 0.0
    for index, path in enumerate([location.path, *subagent_paths]):
        found = _tally_claude(path)
        total = sum(_meq(slot) for slot in found.values())
        if index == 0:
            main_meq += total
        else:
            subagent_meq += total
        for model, slot in found.items():
            for key, value in slot.items():
                per_model[model][key] += value
    return _result(
        "claude",
        session_id,
        per_model,
        subagents=len(subagent_paths),
        main_meq=main_meq,
        subagent_meq=subagent_meq,
    )


def measure_codex(location: SessionLocation, session_id: str) -> dict:
    """Split Codex calls by active turn model without double-counting cache input."""
    per_model = defaultdict(_empty_slot)
    model = "unknown"
    with location.path.open(errors="ignore") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") or {}
            if record.get("type") == "turn_context":
                model = payload.get("model") or model
                continue
            if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            usage = (payload.get("info") or {}).get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            cached = usage.get("cached_input_tokens", 0) or 0
            written = usage.get("cache_write_input_tokens", 0) or 0
            total_input = usage.get("input_tokens", 0) or 0
            slot = per_model[model]
            slot["inp"] += max(0, total_input - cached - written)
            slot["out"] += usage.get("output_tokens", 0) or 0
            slot["cw"] += written
            slot["cr"] += cached
            slot["calls"] += 1
    if not per_model:
        raise MeasurementError(f"no token_count event in {location.path}")
    return _result("codex", session_id, per_model)


def measure_session(
    session_id: str,
    *,
    store: Optional[TranscriptStore] = None,
    workdir: Optional[os.PathLike | str] = None,
    project_dir: Optional[os.PathLike | str] = None,
) -> dict:
    store = store or TranscriptStore.from_environment()
    location = store.locate(session_id, workdir=workdir, project_dir=project_dir)
    if location.agent == "claude":
        return measure_claude(location, session_id)
    return measure_codex(location, session_id)


def combine(measures: Sequence[dict]) -> dict:
    if not measures:
        raise MeasurementError("no sessions to measure")
    per_model = defaultdict(
        lambda: {"meq": 0.0, "calls": 0, **{name: 0 for name in TOKEN_FIELDS.values()}}
    )
    for measurement in measures:
        for model, slot in measurement["modellen"].items():
            for field in per_model[model]:
                per_model[model][field] += slot.get(field, 0)
    models = {
        model: {**slot, "meq": round(slot["meq"], 3)}
        for model, slot in sorted(per_model.items())
    }
    return {
        "session": measures[0]["session"],
        "sessions": [item["session"] for item in measures],
        "agents": sorted({item["agent"] for item in measures}),
        "meq": round(sum(item["meq"] for item in measures), 3),
        "meq_hoofdsessie": round(sum(item["meq_hoofdsessie"] for item in measures), 3),
        "meq_subagents": round(sum(item["meq_subagents"] for item in measures), 3),
        "subagents": sum(item["subagents"] for item in measures),
        "modellen": models,
        "modellen_kort": _short(models),
    }


def refs_from_log(path: os.PathLike | str) -> list[Tuple[str, Optional[str]]]:
    """Return persisted sessions named by a Claude/Codex batch log."""
    refs = []
    workdir = ""
    agent = ""
    seen = set()
    with Path(path).open(errors="ignore") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            match = re.match(r"^  workdir\s+(.+)$", line)
            if match:
                workdir = match.group(1)
                continue
            match = re.match(r"^  agent\s+(claude|codex)\s*$", line)
            if match:
                agent = match.group(1)
                continue
            match = re.match(r"^  session\s+([0-9a-f-]{36})", line)
            if match and agent == "claude":
                ref = (match.group(1), workdir)
            else:
                match = re.match(r"^session id:\s*([0-9a-f-]{36})", line)
                if not match:
                    continue
                ref = (match.group(1), None)
            if ref[0] not in seen:
                refs.append(ref)
                seen.add(ref[0])
    return refs


def recent_sessions(
    days: int,
    *,
    minimum_meq: float = 0.0,
    store: Optional[TranscriptStore] = None,
) -> list[dict]:
    """Measure recent top-level transcripts from both supported agents.

    This is deliberately a transcript inventory, not a statement about whether usage
    was booked. Duplicate rollout files for the same Codex id collapse to the newest
    one, matching the persisted session the interactive tool would resume.
    """
    if days < 0:
        raise MeasurementError("days must not be negative")
    store = store or TranscriptStore.from_environment()
    cutoff = time.time() - days * 86400
    candidates: dict[tuple[str, str], SessionLocation] = {}
    for path in store.claude_projects.glob("*/*.jsonl"):
        if path.stat().st_mtime >= cutoff:
            candidates[("claude", path.stem)] = SessionLocation(
                "claude", path, path.parent
            )
    codex_pattern = re.compile(
        r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
    )
    for path in store.codex_sessions.glob("**/rollout-*.jsonl"):
        if path.stat().st_mtime < cutoff:
            continue
        match = codex_pattern.match(path.name)
        if not match:
            continue
        key = ("codex", match.group(1))
        previous = candidates.get(key)
        if previous is None or path.stat().st_mtime > previous.path.stat().st_mtime:
            candidates[key] = SessionLocation("codex", path)

    results = []
    ordered = sorted(candidates.items(), key=lambda item: item[1].path.stat().st_mtime,
                     reverse=True)
    for (agent, session_id), location in ordered:
        try:
            measurement = (
                measure_claude(location, session_id)
                if agent == "claude"
                else measure_codex(location, session_id)
            )
        except (MeasurementError, OSError):
            continue
        if measurement["meq"] < minimum_meq:
            continue
        results.append({
            "modified": location.path.stat().st_mtime,
            "location": location.as_dict(),
            "measurement": measurement,
        })
    return results
