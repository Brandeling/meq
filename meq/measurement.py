"""Normalize persisted Claude Code and Codex token usage, and locate Cursor transcripts."""

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


class MissingUsageError(MeasurementError):
    """Raised when a transcript was found but persists no token counters.

    This is a separate type so an integration can tell "we found the session and it
    cannot be measured" apart from "there is no such session", and never reports the
    former as a zero measurement.
    """


@dataclass(frozen=True)
class SessionLocation:
    agent: str
    path: Path
    project_dir: Optional[Path] = None
    # Further Claude transcripts of the same session in other project directories.
    # A session that changes its working directory is persisted under both.
    other_paths: Tuple[Path, ...] = ()

    @property
    def paths(self) -> Tuple[Path, ...]:
        return (self.path, *self.other_paths)

    @property
    def project_dirs(self) -> Tuple[Path, ...]:
        if self.project_dir is None:
            return ()
        return (self.project_dir, *(path.parent for path in self.other_paths))

    def as_dict(self) -> dict:
        result = {
            "agent": self.agent,
            "path": str(self.path),
            "paths": [str(path) for path in self.paths],
        }
        if self.project_dir is not None:
            result["project_dir"] = str(self.project_dir)
            result["project_dirs"] = [str(path) for path in self.project_dirs]
        return result


def _claude_location(paths: Iterable[Path]) -> SessionLocation:
    """One location for every Claude transcript of a session, largest first.

    The largest transcript is the most complete one, so it is the primary `path`.
    """
    ordered = sorted(paths, key=lambda path: (-path.stat().st_size, str(path)))
    return SessionLocation("claude", ordered[0], ordered[0].parent, tuple(ordered[1:]))


@dataclass(frozen=True)
class TranscriptStore:
    claude_projects: Path
    codex_sessions: Path
    # Optional so existing two-root callers keep their exact search space.
    cursor_projects: Optional[Path] = None

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
        cursor = os.environ.get("MEQ_CURSOR_PROJECTS") or "~/.cursor/projects"
        return cls(
            Path(claude).expanduser(),
            Path(codex).expanduser(),
            Path(cursor).expanduser(),
        )

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
        """Find the persisted transcript of a session across the supported agents.

        An explicit Claude project is authoritative. Without one, all Claude project
        directories, the Codex rollout tree and the Cursor agent-transcript tree are
        searched. Several Claude transcripts with the same id are one session that
        changed its working directory; they are returned together. Any other ambiguity,
        including the same id under different agents, is reported instead of selecting
        a transcript and silently dropping another one.
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
        # Cursor keeps one directory per conversation, with the top-level transcript
        # named after it and subagent transcripts in a `subagents/` directory beside it.
        cursor_matches = [] if self.cursor_projects is None else [
            Path(path)
            for path in glob.glob(
                str(
                    self.cursor_projects / "*" / "agent-transcripts" / session_id
                    / f"{session_id}.jsonl"
                )
            )
        ]
        matches = (
            [_claude_location(claude_matches)] if claude_matches else []
        ) + [SessionLocation("codex", path) for path in codex_matches] + [
            SessionLocation("cursor", path) for path in cursor_matches
        ]
        if not matches:
            raise MeasurementError(f"no transcript for session {session_id}")
        if len(matches) > 1:
            paths = ", ".join(str(path) for match in matches for path in match.paths)
            raise MeasurementError(
                f"session {session_id} has "
                f"{sum(len(match.paths) for match in matches)} transcripts: {paths}"
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


def _usage_key(record: dict, line: str) -> str:
    """Identify an API message across copies of the same transcript."""
    message = record.get("message") or {}
    if message.get("id"):
        return f"message:{message['id']}"
    if record.get("uuid"):
        return f"uuid:{record['uuid']}"
    return f"line:{line.strip()}"


def _tally_claude(path: Path, seen: Optional[set] = None) -> dict:
    """Sum usage per model in one transcript.

    `seen` holds the keys of messages already counted from another transcript of the
    same session; those are skipped. Within one transcript every line is counted as
    before, so a single transcript measures exactly as it always did.
    """
    per_model = defaultdict(_empty_slot)
    counted = set()
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
            if seen is not None:
                key = _usage_key(record, line)
                if key in seen:
                    continue
                counted.add(key)
            model = message.get("model") or record.get("model") or "unknown"
            slot = per_model[model]
            slot["inp"] += usage.get("input_tokens", 0) or 0
            slot["out"] += usage.get("output_tokens", 0) or 0
            slot["cw"] += usage.get("cache_creation_input_tokens", 0) or 0
            slot["cr"] += usage.get("cache_read_input_tokens", 0) or 0
            slot["calls"] += 1
    if seen is not None:
        seen |= counted
    return per_model


def _result(agent: str, session_id: str, per_model: dict, *, subagents: int = 0,
            main_meq: Optional[float] = None, subagent_meq: float = 0.0) -> dict:
    models = {
        model: _external_slot(slot) for model, slot in sorted(per_model.items())
    }
    # Round once at the public total boundary. Summing the already rounded model values
    # loses real usage when several small model slots each fall below half a milli-Meq.
    total = round(sum(_meq(slot) for slot in per_model.values()), 3)
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
    """Measure a Claude session, which may be split over several project directories.

    Main transcripts count each API message once across all copies. Subagents are the
    union of the `subagents/` directories by file name; when one name occurs in more
    than one directory, the largest file is measured.
    """
    project_dirs = location.project_dirs or (location.path.parent,)
    subagents: dict[str, Path] = {}
    for project_dir in project_dirs:
        for path in (project_dir / session_id / "subagents").glob("*.jsonl"):
            previous = subagents.get(path.name)
            if previous is None or path.stat().st_size > previous.stat().st_size:
                subagents[path.name] = path
    subagent_paths = [subagents[name] for name in sorted(subagents)]
    per_model = defaultdict(_empty_slot)
    main_meq = subagent_meq = 0.0
    seen: Optional[set] = set() if location.other_paths else None
    for path in location.paths:
        found = _tally_claude(path, seen)
        main_meq += sum(_meq(slot) for slot in found.values())
        for model, slot in found.items():
            for key, value in slot.items():
                per_model[model][key] += value
    for path in subagent_paths:
        found = _tally_claude(path)
        subagent_meq += sum(_meq(slot) for slot in found.values())
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


def measure_cursor(location: SessionLocation, session_id: str) -> dict:
    """Refuse to measure a Cursor transcript instead of reporting zero usage.

    Cursor agent transcripts persist the conversation (roles, text and tool calls) but
    no model name and no token counters. A transcript without counters is not a session
    that cost nothing, so this raises until a store that does persist usage is supported.
    """
    with location.path.open(errors="ignore") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if isinstance(record, dict) and (
                "usage" in record
                or (isinstance(message, dict) and "usage" in message)
            ):
                raise MeasurementError(
                    f"Cursor transcript {location.path} contains usage in an unknown "
                    "format; refusing to guess its token counters"
                )
    raise MissingUsageError(
        f"no token usage in Cursor transcript {location.path}: Cursor does not "
        "persist token counters in agent transcripts"
    )


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
    if location.agent == "cursor":
        return measure_cursor(location, session_id)
    return measure_codex(location, session_id)


def combine(measures: Sequence[dict]) -> dict:
    if not measures:
        raise MeasurementError("no sessions to measure")
    per_model = defaultdict(
        lambda: {"calls": 0, **{name: 0 for name in TOKEN_FIELDS.values()}}
    )
    for measurement in measures:
        for model, slot in measurement["modellen"].items():
            for field in per_model[model]:
                per_model[model][field] += slot.get(field, 0)
    models = {
        model: {
            "meq": round(
                sum(slot[TOKEN_FIELDS[key]] * weight for key, weight in WEIGHTS.items())
                / 1e6,
                3,
            ),
            **slot,
        }
        for model, slot in sorted(per_model.items())
    }
    raw_total = sum(
        sum(slot[TOKEN_FIELDS[key]] * weight for key, weight in WEIGHTS.items()) / 1e6
        for slot in per_model.values()
    )
    return {
        "session": measures[0]["session"],
        "sessions": [item["session"] for item in measures],
        "agents": sorted({item["agent"] for item in measures}),
        "meq": round(raw_total, 3),
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
    """Measure recent top-level Claude and Codex transcripts.

    Cursor transcripts are not listed: they persist no token counters, so an inventory
    entry for one could only be a fabricated zero.

    This is deliberately a transcript inventory, not a statement about whether usage
    was booked. Duplicate rollout files for the same Codex id collapse to the newest
    one, matching the persisted session the interactive tool would resume.
    """
    if days < 0:
        raise MeasurementError("days must not be negative")
    store = store or TranscriptStore.from_environment()
    cutoff = time.time() - days * 86400
    candidates: dict[tuple[str, str], SessionLocation] = {}
    claude_paths: dict[str, list[Path]] = defaultdict(list)
    for path in store.claude_projects.glob("*/*.jsonl"):
        claude_paths[path.stem].append(path)
    for session_id, paths in claude_paths.items():
        if any(path.stat().st_mtime >= cutoff for path in paths):
            candidates[("claude", session_id)] = _claude_location(paths)
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
