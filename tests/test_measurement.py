import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from meq.measurement import (
    MeasurementError,
    MissingUsageError,
    TranscriptStore,
    measure_session,
    recent_sessions,
)


FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = json.loads((FIXTURES / "contract.json").read_text())
CLAUDE_SID = CONTRACT["fixtures"]["claude"]["session"]
CODEX_SID = CONTRACT["fixtures"]["codex"]["session"]
CURSOR_SID = "33333333-3333-3333-3333-333333333333"
CURSOR_SUBAGENT_SID = "55555555-5555-5555-5555-555555555555"
SPLIT_SID = "66666666-6666-6666-6666-666666666666"
ENTRYPOINT = Path(__file__).parents[1] / "meq.py"


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
        self.cursor = root / "cursor"
        conversation = self.cursor / "tmp-project" / "agent-transcripts" / CURSOR_SID
        (conversation / "subagents").mkdir(parents=True)
        self.cursor_transcript = conversation / f"{CURSOR_SID}.jsonl"
        shutil.copy(FIXTURES / "cursor.jsonl", self.cursor_transcript)
        shutil.copy(
            FIXTURES / "cursor.jsonl",
            conversation / "subagents" / f"{CURSOR_SUBAGENT_SID}.jsonl",
        )
        self.store = TranscriptStore(self.claude, self.codex, self.cursor)

    def assert_contract(self, result, expected):
        self.assertEqual(result["meq"], expected["meq"])
        weighted = 0
        for model, counters in expected["models"].items():
            actual = dict(result["modellen"][model])
            self.assertEqual(actual.pop("meq"), expected["meq"])
            self.assertEqual(actual, counters)
            weighted += sum(
                counters[field] * weight
                for field, weight in CONTRACT["weights"].items()
                if field != "calls"
            )
        self.assertEqual(weighted, expected["weighted_tokens"])

    def test_claude_fixture_matches_normalized_contract(self):
        result = measure_session(CLAUDE_SID, store=self.store)
        expected = CONTRACT["fixtures"]["claude"]

        self.assertEqual(result["agent"], "claude")
        self.assert_contract(result, expected)

    def test_codex_fixture_matches_the_same_normalized_contract(self):
        result = measure_session(CODEX_SID, store=self.store)
        expected = CONTRACT["fixtures"]["codex"]

        self.assertEqual(result["agent"], "codex")
        self.assert_contract(result, expected)
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

    def test_identical_claude_transcripts_in_two_projects_count_once(self):
        duplicate = self.claude / "-other" / f"{CLAUDE_SID}.jsonl"
        duplicate.parent.mkdir()
        shutil.copy(FIXTURES / "claude.jsonl", duplicate)

        location = self.store.locate(CLAUDE_SID)
        result = measure_session(CLAUDE_SID, store=self.store)

        self.assertEqual(len(location.paths), 2)
        self.assert_contract(result, CONTRACT["fixtures"]["claude"])

    def test_explicit_project_disambiguates_claude(self):
        project = self.claude / "-tmp-project"
        location = self.store.locate(CLAUDE_SID, project_dir=project)

        self.assertEqual(location.agent, "claude")
        self.assertEqual(location.path, project / f"{CLAUDE_SID}.jsonl")

    def test_cursor_transcript_is_found_in_its_conversation_directory(self):
        location = self.store.locate(CURSOR_SID)

        self.assertEqual(location.agent, "cursor")
        self.assertEqual(location.path, self.cursor_transcript)

    def test_cursor_subagent_transcript_is_not_a_top_level_session(self):
        with self.assertRaisesRegex(MeasurementError, "no transcript"):
            self.store.locate(CURSOR_SUBAGENT_SID)

    def test_cursor_transcript_without_usage_is_never_a_zero_measurement(self):
        with self.assertRaises(MissingUsageError) as raised:
            measure_session(CURSOR_SID, store=self.store)

        self.assertIn("no token usage", str(raised.exception))
        self.assertNotIn("no transcript", str(raised.exception))

    def test_cursor_usage_in_an_unknown_format_is_not_guessed(self):
        with self.cursor_transcript.open("a") as handle:
            handle.write('{"role":"assistant","message":{"usage":{"tokens":7}}}\n')

        with self.assertRaises(MeasurementError) as raised:
            measure_session(CURSOR_SID, store=self.store)

        self.assertNotIsInstance(raised.exception, MissingUsageError)
        self.assertIn("unknown format", str(raised.exception))

    def test_same_id_in_claude_and_cursor_is_ambiguous(self):
        shutil.copy(
            FIXTURES / "claude.jsonl",
            self.claude / "-tmp-project" / f"{CURSOR_SID}.jsonl",
        )

        with self.assertRaisesRegex(MeasurementError, "2 transcripts"):
            self.store.locate(CURSOR_SID)

    def test_two_root_store_keeps_its_search_space(self):
        store = TranscriptStore(self.claude, self.codex)

        with self.assertRaisesRegex(MeasurementError, "no transcript"):
            store.locate(CURSOR_SID)

    def test_environment_names_the_cursor_root(self):
        previous = os.environ.get("MEQ_CURSOR_PROJECTS")
        os.environ["MEQ_CURSOR_PROJECTS"] = str(self.cursor)
        self.addCleanup(
            lambda: os.environ.pop("MEQ_CURSOR_PROJECTS", None)
            if previous is None
            else os.environ.__setitem__("MEQ_CURSOR_PROJECTS", previous)
        )

        self.assertEqual(TranscriptStore.from_environment().cursor_projects, self.cursor)

    def test_cli_measure_of_a_cursor_session_fails_loudly(self):
        environment = {
            **os.environ,
            "MEQ_CLAUDE_PROJECTS": str(self.claude),
            "MEQ_CODEX_SESSIONS": str(self.codex),
            "MEQ_CURSOR_PROJECTS": str(self.cursor),
        }

        located = subprocess.run(
            [sys.executable, str(ENTRYPOINT), "locate", CURSOR_SID],
            capture_output=True, text=True, env=environment, check=False,
        )
        measured = subprocess.run(
            [sys.executable, str(ENTRYPOINT), "measure", CURSOR_SID],
            capture_output=True, text=True, env=environment, check=False,
        )

        self.assertEqual(located.returncode, 0, located.stderr)
        self.assertEqual(json.loads(located.stdout)["agent"], "cursor")
        self.assertNotEqual(measured.returncode, 0)
        self.assertEqual(measured.stdout, "")
        self.assertIn("meq: no token usage in Cursor transcript", measured.stderr)

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


class SplitClaudeSessionTests(unittest.TestCase):
    """A Claude session that changed its working directory (meq#3).

    `old` holds a prefix of the main transcript and subagent `agent-a`; `new` holds the
    full transcript and subagents `agent-b` and `agent-c`.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.claude = root / "claude"
        self.codex = root / "codex"
        self.cursor = root / "cursor"
        self.old = self.claude / "-home-user-plemp"
        self.new = self.claude / "-home-user"
        shutil.copytree(FIXTURES / "split" / "old", self.old)
        shutil.copytree(FIXTURES / "split" / "new", self.new)
        self.codex.mkdir()
        self.store = TranscriptStore(self.claude, self.codex, self.cursor)

    def test_split_session_measures_as_the_union(self):
        result = measure_session(SPLIT_SID, store=self.store)

        # Main: msg_01..03 once each (10,000 weighted tokens); the prefix in `old`
        # adds nothing. Subagents: agent-a from `old`, agent-b and agent-c from `new`.
        self.assertEqual(result["meq_hoofdsessie"], 0.01)
        self.assertEqual(result["meq_subagents"], 0.06)
        self.assertEqual(result["meq"], 0.07)
        self.assertEqual(result["subagents"], 3)
        self.assertEqual(result["modellen"]["claude-opus-5"]["calls"], 3)
        self.assertEqual(result["modellen"]["claude-opus-5"]["input_tokens"], 6000)
        self.assertEqual(result["modellen"]["claude-opus-5"]["output_tokens"], 800)
        self.assertEqual(result["modellen"]["claude-sonnet-5"]["calls"], 3)
        self.assertEqual(result["modellen"]["claude-sonnet-5"]["input_tokens"], 60000)

    def test_locate_returns_every_path_with_the_fullest_first(self):
        location = self.store.locate(SPLIT_SID)

        self.assertEqual(location.agent, "claude")
        self.assertEqual(location.path, self.new / f"{SPLIT_SID}.jsonl")
        self.assertEqual(
            location.paths,
            (self.new / f"{SPLIT_SID}.jsonl", self.old / f"{SPLIT_SID}.jsonl"),
        )
        self.assertEqual(location.project_dirs, (self.new, self.old))

    def test_explicit_project_dir_measures_exactly_that_directory(self):
        old = measure_session(SPLIT_SID, store=self.store, project_dir=self.old)
        new = measure_session(SPLIT_SID, store=self.store, project_dir=self.new)

        self.assertEqual(self.store.locate(SPLIT_SID, project_dir=self.old).paths,
                         (self.old / f"{SPLIT_SID}.jsonl",))
        self.assertEqual(
            (old["meq_hoofdsessie"], old["meq_subagents"], old["subagents"]),
            (0.005, 0.01, 1),
        )
        self.assertEqual(
            (new["meq_hoofdsessie"], new["meq_subagents"], new["subagents"]),
            (0.01, 0.05, 2),
        )

    def test_split_claude_session_and_codex_with_the_same_id_refuse(self):
        rollout = self.codex / "2026" / "10" / "31"
        rollout.mkdir(parents=True)
        shutil.copy(
            FIXTURES / "codex.jsonl",
            rollout / f"rollout-2026-10-31T10-00-00-{SPLIT_SID}.jsonl",
        )

        with self.assertRaisesRegex(MeasurementError, "3 transcripts"):
            self.store.locate(SPLIT_SID)
        with self.assertRaises(MeasurementError):
            measure_session(SPLIT_SID, store=self.store)

    def test_split_claude_session_and_cursor_with_the_same_id_refuse(self):
        conversation = self.cursor / "tmp-project" / "agent-transcripts" / SPLIT_SID
        conversation.mkdir(parents=True)
        shutil.copy(FIXTURES / "cursor.jsonl", conversation / f"{SPLIT_SID}.jsonl")

        with self.assertRaisesRegex(MeasurementError, "3 transcripts"):
            self.store.locate(SPLIT_SID)

    def test_recent_inventory_lists_a_split_session_once(self):
        results = recent_sessions(7, store=self.store)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["measurement"]["meq"], 0.07)
        self.assertEqual(len(results[0]["location"]["paths"]), 2)

    def test_cli_locate_and_measure_keep_their_fields(self):
        environment = {
            **os.environ,
            "MEQ_CLAUDE_PROJECTS": str(self.claude),
            "MEQ_CODEX_SESSIONS": str(self.codex),
            "MEQ_CURSOR_PROJECTS": str(self.cursor),
        }

        def run(*arguments):
            completed = subprocess.run(
                [sys.executable, str(ENTRYPOINT), *arguments],
                capture_output=True, text=True, env=environment, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)

        located = run("locate", SPLIT_SID)
        measured = run("measure", SPLIT_SID)
        only_new = run("measure", SPLIT_SID, "--project-dir", str(self.new))

        self.assertEqual(located["path"], str(self.new / f"{SPLIT_SID}.jsonl"))
        self.assertEqual(located["project_dir"], str(self.new))
        self.assertEqual(len(located["paths"]), 2)
        self.assertEqual(located["project_dirs"], [str(self.new), str(self.old)])
        for field in ("meq", "modellen", "modellen_kort", "agents", "sessions",
                      "subagents", "meq_subagents"):
            self.assertIn(field, measured)
        self.assertEqual(measured["meq"], 0.07)
        self.assertEqual(measured["subagents"], 3)
        self.assertEqual(only_new["meq"], 0.06)
        self.assertEqual(only_new["subagents"], 2)


if __name__ == "__main__":
    unittest.main()
