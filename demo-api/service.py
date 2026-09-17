"""Bounded HTTP/1.1 origin for a trusted TLS reverse proxy; Python 3.12 stdlib."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import uuid

import core

HERE = Path(__file__).resolve().parent
HEADER_FIELDS = {"x-broker": "broker", "x-account": "account", "x-as-of": "as_of",
                 "x-currency": "currency", "x-total-scope": "total_scope"}
CORS_HEADERS = "content-type, x-broker, x-account, x-as-of, x-currency, x-total-scope, x-request-id"
RUNTIME_FILES = ("broker_engine.py", "broker_adapters.py", "durable_store.py",
                 "broker_cli.py", "raw_intake.py", "reference.json")


@dataclass(frozen=True)
class Config:
    engine: Path = HERE / "engine"
    state: Path = HERE / "runtime"
    host: str = "127.0.0.1"
    port: int = 8140
    origins: tuple[str, ...] = ("https://soham-porwal.github.io",)
    edge_tls: bool = True
    connections: int = 16
    workers: int = 2
    requests_per_minute: int = 30
    request_seconds: float = 20.0
    work_seconds: float = 8.0

    def validate(self):
        if self.host not in ("127.0.0.1", "0.0.0.0") or not 0 <= self.port <= 65535:
            raise ValueError
        if not self.edge_tls and self.host != "127.0.0.1":
            raise ValueError
        if not self.origins or len(self.origins) > 8 or len(set(self.origins)) != len(self.origins):
            raise ValueError
        for origin in self.origins:
            if not re.fullmatch(r"https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?", origin):
                if self.edge_tls or not re.fullmatch(r"http://(?:localhost|127\.0\.0\.1):[0-9]{1,5}", origin):
                    raise ValueError
        if not (1 <= self.workers <= 4 and self.workers <= self.connections <= 32 and
                1 <= self.requests_per_minute <= 120 and 0 < self.work_seconds <= 10 and
                self.work_seconds + 8 <= self.request_seconds <= 30):
            raise ValueError
        if self.state.resolve().is_relative_to(self.engine.resolve()):
            raise ValueError

    @classmethod
    def from_env(cls):
        return cls(state=Path(os.environ.get("DEMO_STATE", str(HERE / "runtime"))),
                   host=os.environ.get("DEMO_HOST", "127.0.0.1"),
                   port=int(os.environ.get("DEMO_PORT", "8140")),
                   origins=tuple(os.environ.get("DEMO_ORIGINS", "https://soham-porwal.github.io").split(",")),
                   edge_tls=os.environ.get("DEMO_LOCAL_HTTP", "0") != "1")


class Refusal(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def result_for(request_id, outcome, code):
    return dict(request_id=request_id, outcome=outcome, reason_codes=[code] if code else [],
                normalization=None, output_sha256=None, cleanup_result="OK")


def metadata_audit(config, result, started_at, started):
    event = dict(request_id=result["request_id"], input_sha256=None, output_sha256=None,
                 adapter=None, adapter_version=None, policy_hash=None, started_at=started_at,
                 completed_at=core._now(), outcome=result["outcome"],
                 reason_codes=result["reason_codes"], duration_ms=round((time.monotonic()-started)*1000, 3),
                 cleanup_result=result["cleanup_result"])
    core._append_audit(config.state / "audit.jsonl", event)


def readiness(config):
    """Recheck dependencies and local I/O. Never reset or delete stale request state."""
    try:
        manifest = json.loads((HERE / "engine-manifest.json").read_bytes())
        if set(manifest) != set(RUNTIME_FILES):
            return "DEPENDENCY_UNAVAILABLE"
        for name, digest in manifest.items():
            if hashlib.sha256((config.engine / name).read_bytes()).hexdigest() != digest:
                return "DEPENDENCY_UNAVAILABLE"
        if not core.WORKER.is_file():
            return "DEPENDENCY_UNAVAILABLE"
        with sqlite3.connect(":memory:") as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                return "DEPENDENCY_UNAVAILABLE"
        with tempfile.TemporaryFile(dir=config.state / "requests") as probe:
            probe.write(b"probe")
            probe.flush()
            os.fsync(probe.fileno())
        audit = config.state / "audit.jsonl"
        if audit.exists() and audit.stat().st_size >= 64 * 1024 * 1024 - 16384:
            return "AUDIT_CAPACITY"
        fd = os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(fd)
        return None
    except Exception:
        return "DEPENDENCY_UNAVAILABLE"


def install_egress_guard(config):
    """Allow listening/accepted sockets; deny outbound networking and arbitrary children."""
    def guard(event, args):
        if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg", "os.system",
                     "os.exec", "os.posix_spawn", "os.fork") or event.startswith("ctypes."):
            raise RuntimeError
        if event == "subprocess.Popen":
            executable, argv, cwd, _ = args
            expected = [sys.executable, "-I", "-B", str(core.WORKER)]
            # CPython's Windows audit event carries the already-quoted command line.
            allowed_argv = core.subprocess.list2cmdline(expected) if os.name == "nt" else expected
            if (executable != sys.executable or argv != allowed_argv or
                    not Path(cwd).resolve().is_relative_to((config.state / "requests").resolve())):
                raise RuntimeError
    sys.addaudithook(guard)


class Service:
    def __init__(self, config):
        config.validate()
        self.config = config
        self.active = self.jobs = 0
        self.tokens = float(config.requests_per_minute)
        self.refilled = time.monotonic()
        self.fault = None
        self.tasks = set()

    def prepare(self):
        self.config.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.config.state / "requests").mkdir(mode=0o700, exist_ok=True)
        # Prior-process residue is an operator-visible stop, never silently erased.
        if any((self.config.state / "requests").iterdir()):
            self.fault = "STALE_REQUEST_STATE"
        self.fault = self.fault or readiness(self.config)

    def token(self):
        now = time.monotonic()
        self.tokens = min(self.config.requests_per_minute,
                          self.tokens + (now-self.refilled)*self.config.requests_per_minute/60)
        self.refilled = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def accept(self, reader, writer):
        if self.active >= self.config.connections:
            # No unbounded overload tasks, threads, logs or buffering. Edge maps EOF to 503.
            writer.close()
            return
        self.active += 1
        task = asyncio.create_task(self.handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def headers(self, reader):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            raise Refusal(400, "INVALID_HTTP") from None
        if len(raw) > 8192:
            raise Refusal(431, "HEADERS_TOO_LARGE")
        lines = raw[:-4].split(b"\r\n")
        if len(lines) > 40:
            raise Refusal(431, "HEADERS_TOO_LARGE")
        try:
            method, path, version = lines[0].decode("ascii").split(" ")
            headers = {}
            for line in lines[1:]:
                name, value = line.split(b":", 1)
                if not re.fullmatch(rb"[A-Za-z0-9-]+", name):
                    raise ValueError
                key = name.decode("ascii").lower()
                value = value.strip(b" ").decode("ascii")
                if key in headers or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise ValueError
                headers[key] = value
            if version != "HTTP/1.1" or not headers.get("host"):
                raise ValueError
        except (ValueError, UnicodeError):
            raise Refusal(400, "INVALID_HTTP") from None
        if any(k in headers for k in ("transfer-encoding", "content-encoding", "expect", "upgrade",
                                       "content-disposition", "trailer")):
            raise Refusal(400, "UNSUPPORTED_HTTP_FRAMING")
        return method, path, headers

    async def send(self, writer, status, result, origin=None, preflight=False):
        body = core._canonical(result)
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body)),
                   "Connection": "close", "Cache-Control": "no-store", "Pragma": "no-cache",
                   "X-Content-Type-Options": "nosniff", "X-Request-ID": result["request_id"],
                   "Vary": "Origin", "Content-Security-Policy": "default-src 'none'"}
        if origin in self.config.origins:
            headers.update({"Access-Control-Allow-Origin": origin,
                            "Access-Control-Expose-Headers": "X-Request-ID, Retry-After"})
            if preflight:
                headers.update({"Access-Control-Allow-Methods": "POST",
                                "Access-Control-Allow-Headers": CORS_HEADERS,
                                "Access-Control-Max-Age": "600"})
        if status in (429, 503):
            headers["Retry-After"] = "5"
        writer.write((f"HTTP/1.1 {status} Result\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n").encode("ascii") + body)
        await writer.drain()

    async def handle(self, reader, writer):
        started, started_at = time.monotonic(), core._now()
        request_id, origin, audited = uuid.uuid4().hex, None, False
        deadline = asyncio.get_running_loop().time() + self.config.request_seconds
        # Hard wire deadline also covers a blocked reader/writer. Core finalization is
        # never abandoned: no response containing financial data precedes cleanup.
        timer = asyncio.get_running_loop().call_at(deadline, writer.transport.abort)
        status, result = 500, None
        try:
            async with asyncio.timeout_at(deadline - self.config.work_seconds - 7):
                method, path, headers = await self.headers(reader)
                if "x-request-id" in headers:
                    if not re.fullmatch(r"[a-f0-9]{32}", headers["x-request-id"]):
                        raise Refusal(400, "INVALID_TRACE_ID")
                    request_id = headers["x-request-id"]
                origin = headers.get("origin")
                if origin is not None and origin not in self.config.origins:
                    raise Refusal(403, "ORIGIN_DENIED")
                if not self.token():
                    raise Refusal(429, "RATE_LIMITED")
                if path not in ("/health", "/ready", "/normalize"):
                    raise Refusal(404, "NOT_FOUND")
                if self.config.edge_tls and path == "/normalize" and headers.get("x-forwarded-proto") != "https":
                    raise Refusal(400, "TLS_REQUIRED")
                if method == "OPTIONS" and path == "/normalize":
                    requested = {s.strip().lower() for s in headers.get("access-control-request-headers", "").split(",") if s.strip()}
                    if (not origin or headers.get("access-control-request-method") != "POST" or
                            not requested <= set(CORS_HEADERS.split(", "))):
                        raise Refusal(400, "INVALID_PREFLIGHT")
                    status, result = 200, result_for(request_id, "accepted", "PREFLIGHT")
                elif path in ("/health", "/ready") and method == "GET":
                    code = None if path == "/health" else self.fault or await asyncio.to_thread(readiness, self.config)
                    status = 503 if code else 200
                    result = result_for(request_id, "operational_failure" if code else "accepted", code or "HEALTHY")
                elif path == "/normalize" and method == "POST":
                    if self.fault:
                        raise Refusal(503, self.fault)
                    if self.jobs >= self.config.workers:
                        raise Refusal(503, "CAPACITY_EXCEEDED")
                    # Reserve before receiving bytes: slow uploads consume a bounded slot.
                    self.jobs += 1
                    try:
                        code = await asyncio.to_thread(readiness, self.config)
                        if code:
                            raise Refusal(503, code)
                        length = headers.get("content-length", "")
                        if not re.fullmatch(r"[0-9]{1,7}", length):
                            raise Refusal(411, "CONTENT_LENGTH_REQUIRED")
                        length = int(length)
                        if length > 2_000_000:
                            raise Refusal(413, "INPUT_TOO_LARGE")
                        metadata = {field: headers[key] for key, field in HEADER_FIELDS.items() if key in headers}
                        media = headers.get("content-type", "")
                        if media not in ("application/json", "text/csv"):
                            raise Refusal(415, "UNSUPPORTED_MEDIA_TYPE")
                        broker = metadata.get("broker")
                        if broker in core.PROFILES and (media == "text/csv") != (broker == "fidelity_csv"):
                            raise Refusal(415, "PROFILE_MEDIA_MISMATCH")
                        if any(k.startswith("x-") and k not in {*HEADER_FIELDS, "x-request-id", "x-forwarded-proto"} for k in headers):
                            raise Refusal(400, "UNKNOWN_METADATA")
                        upload = await reader.readexactly(length)
                    except BaseException:
                        self.jobs -= 1
                        raise
                else:
                    raise Refusal(405, "METHOD_NOT_ALLOWED")
            if result is None:
                token = core.TRACE_ID.set(request_id)
                try:
                    result = await asyncio.to_thread(core.process_upload, upload, metadata,
                        engine_root=self.config.engine, temp_root=self.config.state / "requests",
                        audit_path=self.config.state / "audit.jsonl", timeout_seconds=self.config.work_seconds)
                    audited = True
                finally:
                    upload = metadata = None
                    self.jobs -= 1
                    core.TRACE_ID.reset(token)
                status = {"accepted": 200, "quarantined": 422, "invalid_request": 400,
                          "operational_failure": 503}[result["outcome"]]
                if "TIMEOUT" in result["reason_codes"]:
                    status = 504
                if result["cleanup_result"] != "OK" or "AUDIT_FAILURE" in result["reason_codes"]:
                    self.fault = "SERVICE_REQUIRES_ATTENTION"
        except Refusal as error:
            status = error.status
            result = result_for(request_id, "operational_failure" if status >= 500 else "invalid_request", error.code)
        except (TimeoutError, asyncio.IncompleteReadError):
            status, result = 408, result_for(request_id, "invalid_request", "REQUEST_BODY_TIMEOUT")
        except Exception:
            status, result = 503, result_for(request_id, "operational_failure", "HTTP_FAILURE")
        try:
            if not audited:
                try:
                    await asyncio.to_thread(metadata_audit, self.config, result, started_at, started)
                except Exception:
                    self.fault = "AUDIT_FAILURE"
                    status, result = 503, result_for(request_id, "operational_failure", "AUDIT_FAILURE")
            if not writer.is_closing():
                async with asyncio.timeout_at(deadline):
                    await self.send(writer, status, result, origin, result["reason_codes"] == ["PREFLIGHT"])
        except (ConnectionError, TimeoutError, OSError):
            pass  # Delivery failure is visible to client; completed audit describes computation, not receipt.
        finally:
            result = None
            timer.cancel()
            writer.close()
            self.active -= 1

    async def serve(self):
        self.prepare()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=self.config.connections))
        # Never let asyncio format a request/exception/transport into a diagnostic.
        def loop_failure(loop, context):
            self.fault = "EVENT_LOOP_FAILURE"
        loop.set_exception_handler(loop_failure)
        server = await asyncio.start_server(self.accept, self.config.host, self.config.port,
                                           limit=8192, backlog=self.config.connections)
        # Event-loop setup may create its own socket pair on Windows. Install only
        # after initialization, before yielding to any request callback.
        install_egress_guard(self.config)
        try:
            async with server:
                await server.serve_forever()
        finally:
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)


def main():
    config = None
    try:
        config = Config.from_env()
        config.validate()
        asyncio.run(Service(config).serve())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        record = result_for(uuid.uuid4().hex, "operational_failure", "STARTUP_FAILURE")
        try:
            metadata_audit(config, record, core._now(), time.monotonic())
        except Exception:
            # Exact audit schema even when no writable destination is available.
            event = dict(request_id=record["request_id"], input_sha256=None, output_sha256=None,
                         adapter=None, adapter_version=None, policy_hash=None, started_at=core._now(),
                         completed_at=core._now(), outcome="operational_failure",
                         reason_codes=["STARTUP_FAILURE"], duration_ms=0, cleanup_result="OK")
            print(json.dumps(event), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
