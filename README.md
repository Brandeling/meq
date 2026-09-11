# meq

Measure and allocate AI agent session usage without coupling the measurement to your
time-tracking system.

`meq` reads persisted Claude Code and Codex transcripts, normalizes their token usage,
and reports input-equivalent tokens in millions:

```
input 1x + output 5x + cache write 1.25x + cache read 0.1x
```

The weights are a transparent comparison scale, not a claim about wall-clock time or
money. The raw token counters are always included so consumers can apply a different
price model later.

## Install

The project has no runtime dependencies beyond Python 3.9 or newer.

```sh
python3 -m pip install .
```

During development, the executable wrapper can be run directly:

```sh
./meq.py --help
```

## Measure a session

Session discovery is agent-independent. A UUID is looked up in both the Claude project
store and the Codex rollout store:

```sh
meq measure 01a08faa-edf2-7893-9f98-75ebb6aae208
meq locate 01a08faa-edf2-7893-9f98-75ebb6aae208
meq recent --days 7 --minimum 0.1
```

The old batch-oriented invocation remains available for small integrations:

```sh
meq /path/to/worktree <session-id>
meq --dir /path/to/escaped-claude-project <session-id>
meq --log /path/to/batch.log
```

Override the transcript roots in tests or non-standard installations with
`MEQ_CLAUDE_PROJECTS` and `MEQ_CODEX_SESSIONS`. The historical
`PLEMP_CLAUDE_PROJECTS` and `PLEMP_CODEX_SESSIONS` names are accepted as fallbacks.

## Propose, approve, then write

A round has two explicit steps. `propose` validates the allocation and writes an
immutable proposal. It does not call a sink:

```sh
meq propose <session-id> ISSUE-42=70 category:coordination=30 \
  --output proposal.json
```

The output contains an approval id. After a human has approved that exact allocation,
apply it with one or more sink adapters:

```sh
meq apply proposal.json --approve <approval-id> \
  --sink ./write-to-jira --sink ./write-to-ledger
```

`apply` measures the session again, after the approval conversation, and sends one JSON
document to each sink on standard input. A sink is any executable that accepts the
[documented payload](docs/sink-contract.md). Measurement and allocation therefore stay
shareable while Jira, accounting, or a plain file remain replaceable adapters.

## Contract fixtures

The files in `tests/fixtures` are intentionally small, stable examples of the external
Claude and Codex transcript formats. Other readers can consume the same fixtures and the
expected normalized counters to detect drift without sharing an implementation.

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

MIT
