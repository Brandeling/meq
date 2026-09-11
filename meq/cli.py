"""Command-line interface for measurement and approved allocation rounds."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .allocation import AllocationError, Proposal, parse_allocations
from .measurement import (
    MeasurementError,
    TranscriptStore,
    combine,
    measure_session,
    recent_sessions,
    refs_from_log,
)


def _json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _measure_legacy(arguments: list[str], store: TranscriptStore) -> dict:
    if len(arguments) >= 2 and arguments[0] == "--log":
        refs = refs_from_log(arguments[1])
        return combine([
            measure_session(session, store=store, workdir=workdir)
            for session, workdir in refs
        ])
    if len(arguments) >= 3 and arguments[0] == "--dir":
        return combine([
            measure_session(arguments[2], store=store, project_dir=arguments[1])
        ])
    if len(arguments) >= 2 and not arguments[0].startswith("-"):
        workdir, sessions = arguments[0], arguments[1:]
        return combine([
            measure_session(session, store=store, workdir=workdir)
            for session in sessions
        ])
    raise MeasurementError("expected a work directory and one or more session ids")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meq")
    commands = parser.add_subparsers(dest="command", required=True)

    measure = commands.add_parser("measure", help="measure one or more session ids")
    measure.add_argument("sessions", nargs="+")
    measure.add_argument("--workdir")
    measure.add_argument("--project-dir")

    locate = commands.add_parser("locate", help="locate a Claude or Codex transcript")
    locate.add_argument("session")

    recent = commands.add_parser("recent", help="list recently modified measured sessions")
    recent.add_argument("--days", type=int, default=7)
    recent.add_argument("--minimum", type=float, default=0.0)

    propose = commands.add_parser("propose", help="create a non-writing allocation proposal")
    propose.add_argument("session")
    propose.add_argument("allocations", nargs="+")
    propose.add_argument("--output", type=Path, required=True)

    apply = commands.add_parser("apply", help="apply an explicitly approved proposal")
    apply.add_argument("proposal", type=Path)
    apply.add_argument("--approve", required=True)
    apply.add_argument("--sink", action="append", default=[], required=True)
    return parser


def _run_modern(arguments: list[str], store: TranscriptStore) -> None:
    args = build_parser().parse_args(arguments)
    if args.command == "measure":
        _json(combine([
            measure_session(
                session,
                store=store,
                workdir=args.workdir,
                project_dir=args.project_dir,
            )
            for session in args.sessions
        ]))
        return
    if args.command == "locate":
        _json(store.locate(args.session).as_dict())
        return
    if args.command == "recent":
        print(json.dumps(
            recent_sessions(args.days, minimum_meq=args.minimum, store=store),
            ensure_ascii=False,
        ))
        return
    if args.command == "propose":
        proposal = Proposal.create(args.session, parse_allocations(args.allocations))
        proposal.write(args.output)
        print(f"Proposal written to {args.output}")
        print(f"Approval id: {proposal.approval_id}")
        for allocation in proposal.allocations:
            print(f"  {allocation.reference}: {allocation.fraction * 100:g}%")
        print("No sink was called.")
        return
    proposal = Proposal.read(args.proposal)
    if args.approve != proposal.approval_id:
        raise AllocationError("approval id does not match this proposal")
    payload = {
        "schema": "meq-sink-v1",
        "proposal": proposal.as_dict(),
        "measurement": measure_session(proposal.session, store=store),
    }
    encoded = json.dumps(payload, ensure_ascii=False) + "\n"
    for sink in args.sink:
        completed = subprocess.run(
            [sink], input=encoded, text=True, check=False
        )
        if completed.returncode:
            raise AllocationError(f"sink {sink!r} exited with {completed.returncode}")


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    store = TranscriptStore.from_environment()
    try:
        if arguments and arguments[0] in {
            "measure", "locate", "recent", "propose", "apply", "-h", "--help"
        }:
            _run_modern(arguments, store)
        else:
            _json(_measure_legacy(arguments, store))
    except (AllocationError, MeasurementError, OSError) as error:
        raise SystemExit(f"meq: {error}") from error


if __name__ == "__main__":
    main()
