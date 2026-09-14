# Inspectable Python examples

Two small, standard-library-only examples intended for code review:

1. `retrieval_eval/` independently packages the ranking metric definitions used in a
   personal retrieval evaluation. Its anonymous numeric export re-derives the recorded
   2026-08-09 result: 40 paired queries, baseline recall@6 `0.4763510101010101`, candidate
   recall@6 `0.37306141774891777`, delta `-0.10328959235209237`, and paired t
   `-3.11001986924922`. The export contains no query IDs or text, document names, paths,
   retrieved hits, corpus content, or credentials. `synthetic_example.json` is a separate,
   invented demonstration; it does not reproduce the historical experiment.
2. `brokerage_normalization/` is a new illustrative reimplementation over invented broker
   payloads. It shows exact `Decimal` conversion, preservation of unknown versus zero
   account values, null-price handling, and cross-account position aggregation. It is not
   employer code, employer data, or a historical artifact.

Run from this directory with Python 3.11 or newer:

```text
python retrieval_eval/scorer.py
python retrieval_eval/scorer.py --synthetic
python brokerage_normalization/normalize.py
python -m unittest discover -s tests -v
python verify.py
python build_zip.py
```

`build_zip.py` creates `portfolio-examples.zip` from a fixed allowlist with stable file
ordering, timestamps, and permissions.
