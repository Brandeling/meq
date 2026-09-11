# Sink adapter contract

`meq apply` invokes each configured sink once and writes a single JSON document to its
standard input. The sink must return exit code zero only after it has durably accepted
the complete document.

```json
{
  "schema": "meq-sink-v1",
  "proposal": {
    "schema": "meq-proposal-v1",
    "session": "<uuid>",
    "allocations": [
      {"reference": "ISSUE-42", "fraction": 0.7},
      {"reference": "category:coordination", "fraction": 0.3}
    ],
    "approval_id": "<16 hex characters>",
    "created_at": "<ISO 8601 timestamp>"
  },
  "measurement": {
    "session": "<uuid>",
    "meq": 0.123,
    "modellen": {}
  }
}
```

Important properties:

- `propose` never invokes a sink.
- The approval id covers the session id and exact allocation fractions.
- `apply` rejects a changed or incorrectly approved proposal before invoking any sink.
- The measurement is made at apply time, so the approval conversation is included.
- Idempotence belongs to the sink. A useful key is `(session, approval_id, reference)`.
- A sink that receives the document but cannot durably write every allocation must return
  non-zero. `meq` then stops and does not call later sinks.

The Dutch field names inside `measurement` are retained for compatibility with the
original Rex batch consumer. They are data-contract names, not user-facing prose.
