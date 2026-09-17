# Deployment contract — requires external verification

This is a raw file upload demonstration using a frozen synthetic registry. The
backend needs no provider connection, secret, login, session cookie or external
API. Broker names select file formats. Unknown identities require review and
quarantine; the service never learns mappings from an upload.

## Observed and unobserved

`verify.py` proves the Python 3.12 Windows backend, original artifact checkpoint,
guarded service entrypoint and synthetic loopback uploads. It does not establish
Linux container operation, public TLS, firewall policy, proxy privacy or sustained
load. Docker was unavailable in the build environment. No deployment was attempted.

The static client ships with an empty `live_demo.api_url` in `v2/content.json`.
This is intentional: public pages render an explicit unavailable state until an
operator completes this release gate, places the exact HTTPS normalization URL
there, rebuilds the site and verifies a synthetic canary through the published
origin. Loopback pages automatically target `http://127.0.0.1:8140/normalize`
for local review only.

## Required boundary

1. Run a single service process as UID/GID 65532 with read-only application and
   engine files, no capabilities, no privilege escalation, bounded memory/CPU,
   processes and file descriptors. Do not mount host credentials or sockets.
2. Place it on a private network reachable only by the TLS reverse proxy. Block
   new outbound connections and DNS at the network/host layer. Python audit hooks
   add defense in depth; they are not an OS security sandbox. Worker resource
   limits are Linux-only: 256 MiB address space, 32 MiB per file, 15 CPU seconds,
   and zero core dump bytes. Parent service limits belong to the container host.
3. The edge terminates HTTPS, overwrites `X-Forwarded-Proto: https`, strips other
   forwarding headers and denies direct origin access. A client-supplied forwarded
   header cannot prove TLS. `DEMO_LOCAL_HTTP=1` is for loopback synthetic review
   only; it refuses a non-loopback bind.
4. Disable edge access logs, body capture, WAF payload samples, tracing, analytics,
   request mirroring, debug exception capture and disk request buffering. Disable
   response caching. Pass the upload as bounded raw bytes. Limit edge bodies to
   2,000,000 bytes and headers to 8 KiB; reject ambiguous framing, compression,
   chunked upstream requests and pipelining. Forward one HTTP/1.1 request with an
   exact Content-Length and close the connection. The edge must not append
   duplicate headers or unsupported `X-*` headers.
5. Set upload/header deadlines at the edge, upstream timeout at least 20 seconds,
   and per-client abuse limits there. Map origin connection refusal/EOF to a
   generic 503. CORS is a browser policy, not authentication; non-browser clients
   can call an exposed endpoint. This is a public demo with bounded global
   capacity, not an authenticated account service.
6. Give `/state` only to this service. Pre-create it and its `requests` subdirectory
   owned by 65532, mode 0700. Keep audit metadata in the persistent `/state`
   volume; mount `/state/requests` as bounded private tmpfs. Normal cleanup removes
   each request workspace before responding. Never share the state volume among
   service instances. Do not back up or collect request scratch, swap or core dumps.
   Disable or encrypt host swap; Python byte objects are not securely zeroized.

## Container template (Linux operator commands, not executed here)

From `demo-api/`, build an image and record its resolved base-image digest. The
Dockerfile's Python 3.12 tag is mutable; pin a reviewed digest for release.

```sh
docker build --pull -t brokerage-upload-demo:review .
docker network create --internal demo-backend
docker run --name brokerage-upload-demo --network demo-backend \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges:true \
  --memory=384m --cpus=1 --pids-limit=64 --ulimit nofile=256:256 --ulimit core=0 \
  --mount type=bind,src=/srv/brokerage-demo-state,dst=/state \
  --tmpfs /state/requests:rw,noexec,nosuid,size=80m,mode=0700,uid=65532,gid=65532 \
  -e DEMO_ORIGINS=https://soham-porwal.github.io \
  brokerage-upload-demo:review
```

The host directory must already exist with the ownership above. Attach the TLS
edge to `demo-backend`; only the edge also joins its public network. Do not publish
port 8140. Independently prove the actual host blocks IPv4/IPv6 TCP, UDP and DNS
egress while allowing edge responses. Docker build-time networking is separate
from the running service. The image allowlist excludes proof, tests and runtime.

Keep the default internal port 8140: the image readiness healthcheck uses it.
Allow one minute between probes; health/readiness consume global request tokens
and metadata audit capacity. Changing the port requires changing the healthcheck.

## Configuration and capacity

| Environment | Default | Contract |
|---|---|---|
| `DEMO_STATE` | `demo-api/runtime` (`/state` in image) | Private writable state, outside engine |
| `DEMO_HOST` | `127.0.0.1` (`0.0.0.0` in image) | Only these two literal addresses |
| `DEMO_PORT` | `8140` | Integer port |
| `DEMO_ORIGINS` | `https://soham-porwal.github.io` | 1–8 exact comma-separated HTTPS origins; no wildcard |
| `DEMO_LOCAL_HTTP` | `0` | `1` enables loopback-only HTTP review |

Defaults: 16 concurrent connections, 2 normalization slots, 30 requests/minute
global token bucket (initial burst 30), 8-second worker deadline, 20-second wire
deadline, 8 KiB/39 header limit and a 2,000,000-byte raw upload limit. The frozen
engine separately caps 5,000 rows and capture-envelope size. Slow uploads hold a
normalization slot. Excess connections close without scheduling a task. No queue
or persistent account state exists. Scale only after independently validating
per-instance state isolation and edge-wide admission limits.

## Operations and external release gate

`GET /health` checks HTTP liveness; `GET /ready` checks pinned engine hashes,
SQLite availability, scratch write/fsync and audit availability/capacity. Readiness
does not run a worker: a successful synthetic normalize is also required after
startup. Audit/cleanup failures latch service attention. Existing scratch at
startup yields `STALE_REQUEST_STATE`; never silently delete or reuse it. A lost
client connection does not cancel cleanup. Forced host/process termination can
leave residue; restart refuses that state.

The audit is capped at 64 MiB; readiness refuses near the limit. Monitor readiness
and metadata reason codes, alert on 503/504, `AUDIT_FAILURE`, `CLEANUP_FAILURE`,
`STALE_REQUEST_STATE`, and `EVENT_LOOP_FAILURE`. Rotate only with the service
stopped and a reviewed metadata retention policy. Investigate scratch residue
privately before any operator-approved disposal. Runtime stdout/stderr stays
empty during normal handling; startup failures emit fixed metadata if audit is
unwritable. Failures before service import are launcher failures: require a
nonzero-exit alert and do not assume an application audit exists.

Before external release: execute the full verification on Python 3.12 Linux,
prove container limits and egress denial, run all examples through the real TLS
edge, test spoofed forwarding/origin headers, disconnects and slow clients, inspect
every proxy/host log for canaries, verify readiness alerts and audit retention,
and test sustained load. These are outstanding deployment checks, not completed
claims. No OAuth or provider credentials are required to close them.
