"""Launch the real guarded service, exercise synthetic uploads, then stop it.

No persistent server or external connections. JSONL stdout is proof metadata only.
"""
import hashlib
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import core
import service

HERE = Path(__file__).resolve().parent


def emit(severity, check, **fields):
    print(json.dumps(dict(severity=severity, check=check, **fields), sort_keys=True), flush=True)


def main():
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="smoke-test-", dir=HERE) as scratch:
            state = Path(scratch) / "state"
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            env = core.runtime_environment()
            env.update(DEMO_STATE=str(state), DEMO_LOCAL_HTTP="1", DEMO_PORT=str(port))
            # Match the isolated Docker entrypoint, with an explicit trusted import root.
            bootstrap = f"import sys; sys.path.insert(0, {str(HERE)!r}); import service; raise SystemExit(service.main())"
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", bootstrap],
                executable=sys.executable, env=env, cwd=HERE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            def request(method, path, raw=None, metadata=None):
                headers = {}
                if metadata:
                    headers = {key: metadata[field] for key, field in service.HEADER_FIELDS.items()}
                    headers["Content-Type"] = "text/csv" if metadata["broker"] == "fidelity_csv" else "application/json"
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                try:
                    connection.request(method, path, raw, headers)
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    if response.getheader("Cache-Control") != "no-store":
                        raise RuntimeError("cache_contract")
                    if response.getheader("X-Request-ID") != body["request_id"]:
                        raise RuntimeError("trace_contract")
                    return response.status, body
                finally:
                    connection.close()

            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        status, body = request("GET", "/ready")
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("startup") from None
                        time.sleep(.05)
                if status != 200 or body["reason_codes"] != ["HEALTHY"]:
                    raise RuntimeError("readiness")
                emit("OK", "loopback_ready", http=status)
                status, body = request("GET", "/health")
                if status != 200 or body["reason_codes"] != ["HEALTHY"]:
                    raise RuntimeError("health")
                emit("OK", "loopback_health", http=status)
                examples = json.loads((HERE/"examples"/"examples.json").read_bytes())
                if len(examples) != 4 or {e["metadata"]["broker"] for e in examples} != core.PROFILES:
                    raise RuntimeError("example_inventory")
                for example in examples:
                    raw = (HERE/"examples"/example["file"]).read_bytes()
                    status, body = request("POST", "/normalize", raw, example["metadata"])
                    accepted = example["expected_outcome"] == "accepted"
                    if status != (200 if accepted else 422) or body["outcome"] != example["expected_outcome"]:
                        raise RuntimeError("example_outcome")
                    if body["cleanup_result"] != "OK" or list((state/"requests").iterdir()):
                        raise RuntimeError("cleanup")
                    rows = 0
                    if accepted:
                        normalization = body["normalization"]
                        rows = len(normalization["positions"]) + len(normalization["cash"])
                        if not rows or hashlib.sha256(core._canonical(normalization)).hexdigest() != body["output_sha256"]:
                            raise RuntimeError("empty_or_hash")
                    elif body["reason_codes"] != ["UNRESOLVED_IDENTITY"] or body["normalization"] is not None:
                        raise RuntimeError("quarantine")
                    emit("OK" if accepted else "WARN", "bundled_example", file=example["file"],
                         http=status, outcome=body["outcome"], rows=rows,
                         reason_codes=body["reason_codes"], expected_rejection=not accepted)
                status, body = request("POST", "/normalize", b"[]", examples[0]["metadata"])
                if status != 503 or body["reason_codes"] != ["EMPTY_NORMALIZATION"] or body["normalization"] is not None:
                    raise RuntimeError("empty_control")
                emit("SUSPECT", "empty_normalization_control", http=status,
                     outcome=body["outcome"], reason_codes=body["reason_codes"], expected_rejection=True)
                status, body = request("POST", "/normalize", b"{", examples[0]["metadata"])
                if status != 400 or body["reason_codes"] != ["INVALID_JSON"]:
                    raise RuntimeError("malformed_control")
                emit("WARN", "malformed_upload_control", http=status, expected_rejection=True)
                status, body = request("GET", "/ready")
                if status != 200 or list((state/"requests").iterdir()):
                    raise RuntimeError("final_readiness_cleanup")
                audit = (state/"audit.jsonl").read_text()
                records = [json.loads(line) for line in audit.splitlines()]
                fields = set("request_id input_sha256 output_sha256 adapter adapter_version policy_hash started_at completed_at outcome reason_codes duration_ms cleanup_result".split())
                if len(records) != 9 or any(set(r) != fields or r["cleanup_result"] != "OK" for r in records):
                    raise RuntimeError("audit_accounting")
                if any(canary in audit for canary in ("SYNTHETIC-DEMO", "AAPL", "positions", "security_id", str(state))):
                    raise RuntimeError("audit_privacy")
                emit("OK", "loopback_audit_and_cleanup", records=len(records))
            finally:
                process.terminate()
                stdout, stderr = process.communicate(timeout=10)
                process = None
            if stdout or stderr:
                raise RuntimeError("private_diagnostics")
            emit("OK", "silent_service_diagnostics", streams=2)
        emit("OK", "smoke_complete", examples=4)
        return 0
    except Exception as error:
        # Never print arbitrary exception messages, responses or child diagnostics.
        emit("FAIL", "smoke_incomplete", error_type=type(error).__name__)
        return 1
    finally:
        if process is not None:
            try:
                process.kill()
                process.communicate(timeout=10)
            except Exception:
                emit("FAIL", "smoke_process_cleanup")


if __name__ == "__main__":
    raise SystemExit(main())
