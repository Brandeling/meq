import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from meq.allocation import AllocationError, Proposal, parse_allocations


class AllocationTests(unittest.TestCase):
    def test_percentages_and_fractions_normalize_to_the_same_contract(self):
        percentages = parse_allocations(["ISSUE-1=60", "category:coordination=40"])
        fractions = parse_allocations(["ISSUE-1=0.6", "category:coordination=0.4"])

        self.assertEqual(percentages, fractions)
        self.assertEqual(percentages[0].fraction, Decimal("0.6"))

    def test_invalid_total_is_rejected_before_a_proposal_exists(self):
        with self.assertRaisesRegex(AllocationError, "expected 100%"):
            parse_allocations(["ISSUE-1=60", "ISSUE-2=20"])

    def test_duplicate_reference_is_rejected(self):
        with self.assertRaisesRegex(AllocationError, "more than once"):
            parse_allocations(["ISSUE-1=50", "ISSUE-1=50"])

    def test_tampered_proposal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proposal.json"
            Proposal.create("session", parse_allocations(["ISSUE-1=100"])).write(path)
            data = json.loads(path.read_text())
            data["allocations"][0]["reference"] = "ISSUE-2"
            path.write_text(json.dumps(data))

            with self.assertRaisesRegex(AllocationError, "approval id"):
                Proposal.read(path)


class ApprovalRoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claude = self.root / "claude" / "-project"
        self.claude.mkdir(parents=True)
        self.sid = "33333333-3333-3333-3333-333333333333"
        (self.claude / f"{self.sid}.jsonl").write_text(
            '{"message":{"model":"claude-opus-5","usage":{"input_tokens":1000}}}\n'
        )
        self.proposal = self.root / "proposal.json"
        self.sink_log = self.root / "sink.json"
        self.sink = self.root / "sink.py"
        self.sink.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "pathlib.Path(os.environ['SINK_LOG']).write_text(sys.stdin.read())\n"
        )
        self.sink.chmod(0o755)
        self.environment = {
            **os.environ,
            "MEQ_CLAUDE_PROJECTS": str(self.root / "claude"),
            "MEQ_CODEX_SESSIONS": str(self.root / "codex"),
            "SINK_LOG": str(self.sink_log),
        }

    def run_meq(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "meq.cli", *arguments],
            cwd=Path(__file__).parents[1],
            env=self.environment,
            capture_output=True,
            text=True,
        )

    def test_propose_never_calls_a_sink_and_apply_requires_exact_approval(self):
        proposed = self.run_meq(
            "propose", self.sid, "ISSUE-1=100", "--output", str(self.proposal)
        )
        self.assertEqual(proposed.returncode, 0, proposed.stderr)
        self.assertFalse(self.sink_log.exists())
        approval_id = json.loads(self.proposal.read_text())["approval_id"]

        rejected = self.run_meq(
            "apply", str(self.proposal), "--approve", "wrong", "--sink", str(self.sink)
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(self.sink_log.exists())

        applied = self.run_meq(
            "apply", str(self.proposal), "--approve", approval_id,
            "--sink", str(self.sink),
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        payload = json.loads(self.sink_log.read_text())
        self.assertEqual(payload["schema"], "meq-sink-v1")
        self.assertEqual(payload["measurement"]["session"], self.sid)


if __name__ == "__main__":
    unittest.main()
