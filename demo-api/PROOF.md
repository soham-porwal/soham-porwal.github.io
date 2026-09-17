# Backend verification and release status

The source and static Appmosis interface are released separately from backend
hosting. No compatible authorized backend target is configured. The public page
therefore displays an unavailable state, with an empty API URL and no fabricated
portfolio. Public API canaries, Linux container limits, TLS edge privacy and
host-level egress denial are **not verified**. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Reproduce the backend checks

Use Python 3.12, from `demo-api/` in the public repository:

```sh
python -B -m unittest discover -s . -p 'test_*.py' -v
python -B reproduce.py
python -B smoke.py
```

The core suite has 24 tests; the HTTP service suite has 16. The smoke command
starts and stops its own loopback service, checks readiness/health, executes all
four bundled synthetic examples, and checks hashes, metadata-only audit and
request cleanup. Expected outcomes are three accepted requests and one
`UNRESOLVED_IDENTITY` quarantine. It also rejects malformed input. Require a
zero exit and the final `smoke_complete` event; an existing proof folder does
not establish success. The reproduction command retains the old Windows launch
forms as expected negative controls, then checks the repaired executable/runtime
behavior and reviewed/unreviewed synthetic Plaid identities.

The six engine inputs are pinned in `engine-manifest.json`. Normalization rules
were not changed for release. The optional `verify.py --artifact <directory>`
runner additionally executes the original 102-test checkpoint in a byte-identical
copy, checks original inventories before/after, then runs both backend suites,
reproduction and HTTP smoke. Its original development artifact is not shipped
in this public release. Local development evidence remains outside the public
inventory; the release rerun uses the already-bundled pinned checkpoint copy.
Require its final `verification_complete` event.

## Browser checks

The public unavailable-state regression uses Playwright with Google Chrome:

```powershell
$env:DEMO_SITE_URL = 'https://soham-porwal.github.io/appmosis.html'
$env:DEMO_EXPECT_UNAVAILABLE = '1'
$env:DEMO_SCREENSHOT_DIR = 'browser-proof'
node tests/test_brokerage_demo_browser.js
```

Install Playwright in your review environment if it is not already available;
`DEMO_CHROME` can select your Chrome executable. The regression checks desktop
(1440 px) and mobile (390 px), all four synthetic controls, the unavailable
verdict, absence of a portfolio or normalization request, JavaScript errors and
horizontal overflow. It writes screenshots and a metadata-only result file.

For local accepted/quarantine checks, serve the repository root on loopback
port 8139, start `demo-api/service.py` with `DEMO_LOCAL_HTTP=1`,
`DEMO_ORIGINS=http://127.0.0.1:8139` and a fresh private `DEMO_STATE`. Unset
`DEMO_EXPECT_UNAVAILABLE`, set `DEMO_SITE_URL` to
`http://127.0.0.1:8139/appmosis.html`, and run the same browser regression. It
checks desktop Alpaca acceptance and quarantine, mobile Fidelity acceptance,
trace/positions and overflow. Stop the local service after requests complete.
Only use bundled synthetic examples.

These authored regressions establish local behavior when their commands pass;
they are not production-readiness certification or public API evidence.
