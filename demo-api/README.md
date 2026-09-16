# Brokerage file upload demo

Upload a JSON or CSV export with explicit metadata and receive either a normalized
portfolio, a review quarantine, an invalid-request response or an operational
failure. The three bundled accepted examples and one unknown-identity example
are synthetic. The frozen registry intentionally supports only reviewed sample
identities. No account connection or provider credentials are involved.

Quick review from `demo-api/` (starts and stops its own loopback service):

```powershell
$python312 = 'python' # Use Python 3.12.
& $python312 -B smoke.py
```

Full evidence and commands: [proof index](PROOF.md). Deployment prerequisites and
unverified external boundaries: [deployment contract](DEPLOYMENT.md).

The generated Appmosis case-study page contains the matching split-screen browser
experience. On `localhost` or `127.0.0.1` it targets the loopback service below;
on the public site it fails visibly until `projects[].live_demo.api_url` in
`v2/content.json` is set to a verified TLS deployment. It never substitutes a
sample dashboard for an unavailable response.

## HTTP file upload

Run `& $python312 -B service.py` with `DEMO_LOCAL_HTTP=1` for synthetic loopback
review; it listens at `http://127.0.0.1:8140`. Leave that foreground process running
and use another terminal for a request. Stop it after pending requests finish.
All local state defaults to `demo-api/runtime/`.

```powershell
# Server terminal, from demo-api/
$env:DEMO_LOCAL_HTTP = '1'
python -B service.py # Python 3.12
```

```powershell
# Client terminal, from demo-api/; the file's raw bytes are the body.
curl.exe --fail-with-body http://127.0.0.1:8140/normalize `
  -H 'Content-Type: application/json' `
  -H 'X-Broker: plaid' -H 'X-Account: SYNTHETIC-DEMO' `
  -H 'X-As-Of: 2026-09-01T20:00:00Z' -H 'X-Currency: USD' `
  -H 'X-Total-Scope: holdings' --data-binary '@examples/plaid.json'
```

`POST /normalize` accepts raw `application/json` for Alpaca/Plaid or `text/csv`
for Fidelity. Send Content-Length and all five metadata headers above. This is
not multipart/form-data: a browser can send its selected File directly as the
fetch body. Do not send a filename or Content-Disposition. Content encoding,
transfer encoding, duplicate headers, query paths, unknown `X-*` metadata and
missing/ambiguous framing are refused. Maximum raw body: 2,000,000 bytes.

Browser requests require an exact configured Origin; OPTIONS supports POST and
the documented metadata headers. The default allowed origin is
`https://soham-porwal.github.io`. Local browser review can explicitly set
`DEMO_ORIGINS=http://localhost:8000` in loopback mode. Optional `X-Request-ID` is
exactly 32 lowercase hex characters; otherwise the service generates it.

| Request/result | HTTP | Meaning |
|---|---:|---|
| `GET /health` | 200 | HTTP liveness (not worker readiness) |
| `GET /ready` | 200 / 503 | Dependency, state and audit probe |
| Accepted normalization | 200 | Nonempty portfolio and canonical output hash |
| Quarantine | 422 | Engine reason code; no portfolio |
| Invalid upload | 400 | Syntax/metadata failure; no portfolio |
| Framing, media, size or method refusal | 400/404/405/411/413/415/431 | Stable boundary reason code |
| Origin denied / rate limited | 403 / 429 | Refusal; retry policy at edge |
| Upload timeout / worker timeout | 408 / 504 | Deadline expired |
| Infrastructure, audit, cleanup or capacity failure | 503 | No portfolio; inspect metadata reason |

Every response carries `request_id`, `outcome`, `reason_codes`, `normalization`,
`output_sha256`, `cleanup_result`; it sets no-store and echoes the trace header.
Health/preflight responses use `accepted` with a fixed status reason and null
normalization. Only accepted **normalization** responses contain a portfolio.
Readiness and all admitted HTTP requests are audited. Connections refused at the
connection ceiling close without allocating a request or writing an audit.

`core.py` exports the frozen `process_upload` signature. Import by adding this
directory to the module search path or using `importlib.util.spec_from_file_location`.
The implementation uses Python 3.12 standard-library APIs only.

## Calling contract

Pass raw bytes and exactly five string metadata fields: `broker`, `account`,
`as_of`, `currency`, `total_scope`. Supported profiles are `alpaca`, `plaid`,
`fidelity_csv`. Observation time must be an explicit `YYYY-MM-DDTHH:MM:SSZ` instant.
Scopes are `holdings` and `holdings_plus_cash`. Pass trusted local filesystem
configuration for `engine_root`, `audit_path`, and an **existing** `temp_root`.
The audit's parent directory must exist. Output paths inside the engine are refused.

Each valid supported request starts a shell-free Python worker using the current
interpreter, `-I` isolation, and `-B` to prevent bytecode writes. The fixed worker
receives its inputs on stdin. It imports the existing raw intake, engine and
durable store; it never executes the artifact CLI. It uses a unique workspace and
SQLite database, ingests once, verifies durable state, and reads the portfolio
back from that store. Network/socket operations and child process launches are
denied inside the worker. The raw input is carried in memory; the SQLite snapshot
and any store diagnostics exist only in the request workspace.

The child receives only canonical `SystemRoot` and `WINDIR` on Windows, and an
empty environment on other platforms. Python 3.12's Windows networking runtime
needs SystemRoot even when outbound traffic is denied. Missing Windows runtime
paths fail closed; credentials, proxy settings, PATH and PYTHONPATH are not
forwarded. `Popen` supplies the explicit interpreter executable so the Windows
audit event satisfies the service's exact executable/arguments/workspace guard.

Two transport details preserve the frozen contract:

- Raw intake's legacy scope vocabulary differs from the engine. The wrapper
  supplies the neutral `holdings` scope to intake, then sets the explicitly
  requested engine scope in the envelope before engine validation.
- JSON payload numeric tokens pass through unchanged after intake's strict
  parse. This avoids converting its `Decimal` values to floats or strings.
  The engine's own capture-size limit still applies after adding the envelope;
  a raw upload at the byte limit can therefore be quarantined as `INPUT_TOO_LARGE`.

No business validation, identity mapping or normalization rules are copied.
Domain/store rejections retain their engine codes. Invalid JSON and CSV syntax
are invalid requests; schema/domain rejection remains quarantine. An empty
portfolio, store invariant failure, worker failure or timeout is operational
failure. Accepted portfolios may still carry the engine's `WARN` status for
missing quote times; this wrapper invents no times or reconciliation totals.

## Deadline, cleanup and audit

The deadline starts on entry and bounds worker startup, imports, intake, engine
work and SQLite through `Popen.communicate(timeout=remaining)`. The parent kills
and reaps an overdue worker before removing its workspace. Cleanup and audit
finalization follow the work deadline rather than being skipped when it expires.
Every work-deadline expiry uses the stable `TIMEOUT` reason in the response and
audit, including expiry before worker startup.
Worker reaping, cleanup retries, and audit-lock acquisition each have a two-second
allowance. This is not a real-time bound on OS filesystem calls themselves.

Every returned result has confirmed `cleanup_result=OK` or explicit `FAIL` with
`operational_failure`; failure responses clear the portfolio and output hash.
Cleanup verifies both direct path absence and absence from the parent directory's
listing. This covers Windows delete-pending entries that a direct path probe can
miss. A still-visible entry is retried within the cleanup allowance; inability to
confirm removal becomes `CLEANUP_FAILURE`. Each concurrent call completes its own
cleanup before returning; no deferred cleanup or executor shutdown is required.
Only the parent appends the persistent audit, after cleanup. Its exact twelve
fields contain only opaque IDs, hashes, fixed profile/version information, times,
outcomes and reason codes. Unknown user broker strings are represented by null.
`adapter_version` is SHA-256 of `broker_adapters.py`; `policy_hash` follows the
artifact policy fingerprint (reference bytes and its five named runtime sources).
These are null when the worker has not established them. `input_sha256` is null
for non-byte input. Output hashing uses the store's canonical JSON convention:
sorted keys, compact separators, UTF-8, `ensure_ascii=False`, no nonfinite numbers.

Audit writers use a cross-process OS lock on the audit file itself, write one
complete JSONL record, and fsync. Failed writes are truncated back to their prior
length under the same lock when the filesystem allows it. Audit failure returns
`AUDIT_FAILURE` and clears success; no exception text or fallback payload log is
written. An inaccessible/unwritable audit destination cannot physically receive
a record. Callers must treat that operational result as a failed request, and
must not assume an event exists. Catastrophic I/O failures can also prevent
rollback or cleanup; those failures are not reported as success. This slice
offers no crash-recovery service for host/process termination outside the call.

## Verification and scope

From the repository root, run the complete backend suites and synthetic HTTP checks:

```powershell
cd demo-api
python -B -m unittest discover -s . -p 'test_*.py' -v
python -B reproduce.py
python -B smoke.py
```

The optional original-artifact verifier (`python -B verify.py --artifact <original-artifact-directory>`)
requires the original pinned development artifact, which is not included in this public source release.
It requires Python 3.12 and creates a unique `proof/` run, verifies and
copies the original artifact's pinned inputs, executes its unchanged checkpoint
in that copy, runs both full backend suites, reproduces the repaired failures and
executes every example through the real guarded entrypoint. It compares the
original artifact's entire file/hash inventory before and after; engine and
cloned checkpoint source bytes must also remain unchanged. Empty command output,
zero discovered tests or missing completion records fail the verifier.

Tests author synthetic payloads and their own scratch files below `demo-api/`.
They cover all outcome classes, three profiles, both scopes, decimal precision,
canonical hashing, explicit observation times, isolation, metadata/schema/syntax
errors, store admission codes, SQLite/import/invariant/protocol faults, deadlines,
cleanup failures, audit privacy/write rollback, and threaded/process concurrency.
Dependency/cache hashing checks consumed artifact files across a real request.
Fault tests redirect the fixed worker to same-run test scripts; production has
no request-controlled fault switch or worker path.

Current Python 3.12 results and reproduction commands
are cataloged in [PROOF.md](PROOF.md). These are authored regression tests and
observed local runs, not production-readiness certification.

Backend deliverables and their proof index remain in `demo-api/`. The selected
Arm B integration also changes the Appmosis source/generated page, shared styles,
the browser client, build asset list and browser regression under `tests/`.
The source and static interface are published on GitHub Pages. No public API is
configured; the required backend hosting boundaries remain unverified.
