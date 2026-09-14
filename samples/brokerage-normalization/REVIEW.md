# Adversarial review

AI-assisted implementation and separate delegated code review, followed by executable
regression tests. This is not a human audit or production certification.

| Finding | Fix and evidence |
|---|---|
| Old CSV date could hide behind a new wrapper | Source/capture date conflict rejects; broker test 19 |
| Malformed Plaid account elements dropped or escaped quarantine | Domain/type validation; test 20 |
| Extra strong identifiers ignored | Supplied FIGI/SEDOL must resolve; test 21 |
| Expired options valued as complete; nonstandard multiplier accepted | Unknown expired value and explicit 100-unit boundary; tests 22–23 |
| Contradictory source quote date ignored | Compare source-local date with quote timestamp/sidecar; tests 24–25 |
| Huge JSON integers aborted batch | Domain rejection and continued processing; baseline parser regression |
| Output exposed mutable state | Detached reports and hash verification |
| Audit path could alias input | Alias checks and byte-preservation tests |
| Ambient Decimal context changed arithmetic | Explicit isolated context and integer-scaled oracle |
| Regeneration could rewrite reviewed identities | Preserve registry, reject unapproved additions; preparation tests |

`checkpoint.py` runs the entire suite and verifies source hashes, known arithmetic,
expected quarantines, persisted replay across processes and SQLite integrity. Tests that
expect rejection pass only when unsafe input is refused: they are not skipped failures.

The unchanged Plaid documentation sample intentionally quarantines on an unapproved FIGI.
The unchanged Fidelity test report intentionally quarantines for pending activity. Accepted
paths use separately labeled derived/synthetic captures; neither rejection proves a full
broker integration. The evidence supports the stated subset and boundaries only.

## Residual limitations

- **No authenticated broker request, live account connection or customer export was ever
  observed or used**, at any point, for any part of this sample. Every accepted capture is
  derived from published vendor documentation, from an author-published MIT-licensed fixture,
  or is explicitly labeled synthetic; `captures/provenance.json` records which is which, and
  `sources/CITATIONS.md` records the documentation each unit convention rests on.
- **Schwab/TD and Robinhood are not supported.** An unrecognized broker quarantines as
  `UNRECOGNIZED_BROKER_REVIEW_REQUIRED`. That is a deliberate fail-closed refusal, not
  partial support.
- **Corporate actions are partial.** Dated symbol aliases and absolute post-split snapshots
  are handled. There is no corporate-action engine, no synthetic trade reconstruction, and no
  support for adjusted or index option deliverables; those fail closed rather than approximate.
- **Bounds are per input** (2,000,000 bytes, 5,000 rows). File count, account count and
  accumulated store history are unbounded. This is for small work-sample batches.
- **Fault-injected storage failure, corrupt-database refusal, policy-hash mismatch and
  concurrent writers are covered** in `tests/test_durable_store.py`. Production load testing
  and long-duration operational observation are not. Passing unit tests are not production
  certification and must not be described as such.
- **The local model proposed field names once.** No human approval of that individual mapping
  is claimed, and `frozen_vendorx.py` is deliberately not registered in runtime dispatch.

For the current test count, run the suite; it prints its own. No count is hardcoded here.
