import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from meq.measurement import (
    MeasurementError,
    TranscriptStore,
    measure_session,
    recent_sessions,
)


FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = json.loads((FIXTURES / "contract.json").read_text())
CLAUDE_SID = CONTRACT["fixtures"]["claude"]["session"]
CODEX_SID = CONTRACT["fixtures"]["codex"]["session"]


class MeasurementContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.claude = root / "claude"
        self.codex = root / "codex"
        project = self.claude / "-tmp-project"
        project.mkdir(parents=True)
        shutil.copy(FIXTURES / "claude.jsonl", project / f"{CLAUDE_SID}.jsonl")
        rollout = self.codex / "2026" / "09" / "01"
        rollout.mkdir(parents=True)
        shutil.copy(
            FIXTURES / "codex.jsonl",
            rollout / f"rollout-2026-09-01T10-00-00-{CODEX_SID}.jsonl",
        )
        self.store = TranscriptStore(self.claude, self.codex)

    def test_claude_fixture_matches_normalized_contract(self):
        result = measure_session(CLAUDE_SID, store=self.store)
        expected = CONTRACT["fixtures"]["claude"]

        self.assertEqual(result["agent"], "claude")
        self.assertEqual(result["meq"], expected["meq"])
        for model, counters in expected["models"].items():
            actual = dict(result["modellen"][model])
            self.assertEqual(actual.pop("meq"), expected["meq"])
            self.assertEqual(actual, counters)

    def test_codex_fixture_matches_the_same_normalized_contract(self):
        result = measure_session(CODEX_SID, store=self.store)
        expected = CONTRACT["fixtures"]["codex"]

        self.assertEqual(result["agent"], "codex")
        self.assertEqual(result["meq"], expected["meq"])
        for model, counters in expected["models"].items():
            actual = dict(result["modellen"][model])
            self.assertEqual(actual.pop("meq"), expected["meq"])
            self.assertEqual(actual, counters)
        self.assertEqual(result["modellen_kort"], "gpt-5.6-terra 0,003")

    def test_total_is_rounded_after_raw_model_values_are_summed(self):
        session = "44444444-4444-4444-4444-444444444444"
        transcript = self.claude / "-tmp-project" / f"{session}.jsonl"
        transcript.write_text(
            '{"message":{"model":"claude-opus-5","usage":{"input_tokens":490}}}\n'
            '{"message":{"model":"claude-sonnet-5","usage":{"input_tokens":490}}}\n'
        )

        result = measure_session(session, store=self.store)

        self.assertEqual(result["modellen"]["claude-opus-5"]["meq"], 0.0)
        self.assertEqual(result["modellen"]["claude-sonnet-5"]["meq"], 0.0)
        self.assertEqual(result["meq"], 0.001)
        self.assertEqual(result["meq_hoofdsessie"], 0.001)

    def test_global_lookup_rejects_duplicate_session_ids(self):
        duplicate = self.claude / "-other" / f"{CLAUDE_SID}.jsonl"
        duplicate.parent.mkdir()
        shutil.copy(FIXTURES / "claude.jsonl", duplicate)

        with self.assertRaisesRegex(MeasurementError, "2 transcripts"):
            self.store.locate(CLAUDE_SID)

    def test_explicit_project_disambiguates_claude(self):
        project = self.claude / "-tmp-project"
        location = self.store.locate(CLAUDE_SID, project_dir=project)

        self.assertEqual(location.agent, "claude")
        self.assertEqual(location.path, project / f"{CLAUDE_SID}.jsonl")

    def test_recent_inventory_includes_claude_and_codex(self):
        results = recent_sessions(7, minimum_meq=0.001, store=self.store)

        self.assertEqual(
            {item["measurement"]["agent"] for item in results},
            {"claude", "codex"},
        )
        self.assertEqual(
            {item["measurement"]["session"] for item in results},
            {CLAUDE_SID, CODEX_SID},
        )


if __name__ == "__main__":
    unittest.main()
