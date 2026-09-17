"""Synchronous, ephemeral boundary for the frozen brokerage artifact (Python 3.12).

The worker owns all engine state. The caller owns audit_path and temp_root, which
must be local filesystem paths; temp_root must already exist. See README.md.
"""
from __future__ import annotations

import base64
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


PROFILES = frozenset(("alpaca", "plaid", "fidelity_csv"))
FIELDS = frozenset(("broker", "account", "as_of", "currency", "total_scope"))
WORKER = Path(__file__).resolve().with_name("worker.py")
FINALIZE_SECONDS = 2.0
TRACE_ID = ContextVar("request_trace_id", default=None)


def runtime_environment():
    """Only Windows runtime paths cross the child boundary; no user credentials.

    SystemRoot is needed by Winsock/_overlapped on Windows Python 3.12.
    Canonicalize names because a service launcher may use different casing.
    Missing configuration fails closed instead of guessing a Windows location.
    """
    if os.name != "nt":
        return {}
    inherited = {k.upper(): v for k, v in os.environ.items()}
    root = inherited.get("SYSTEMROOT") or inherited.get("WINDIR")
    if not root or not Path(root).is_absolute() or not Path(root).is_dir():
        raise RuntimeError
    return {"SystemRoot": root, "WINDIR": root}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _failure(result, code):
    result.update(outcome="operational_failure", normalization=None, output_sha256=None)
    result["reason_codes"] = list(dict.fromkeys([*result["reason_codes"], code]))


def _validate(upload, metadata):
    if type(upload) is not bytes:
        return "UPLOAD_BYTES_REQUIRED"
    if not upload:
        return "EMPTY_UPLOAD"
    if len(upload) > 2_000_000:
        return "INPUT_TOO_LARGE"
    if type(metadata) is not dict or metadata.keys() != FIELDS:
        return "INVALID_METADATA"
    if any(type(v) is not str or not v.strip() or len(v) > 256 or
           any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in v)
           for v in metadata.values()):
        return "INVALID_METADATA"
    if metadata["total_scope"] not in ("holdings", "holdings_plus_cash"):
        return "INVALID_TOTAL_SCOPE"
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                        metadata["as_of"]):
        return "INVALID_AS_OF"
    try:
        datetime.strptime(metadata["as_of"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return "INVALID_AS_OF"
    return None


def _append_audit(path, event):
    """Cross-process lock on the audit file itself; no persistent lock sidecar.

    Roll back a failed append while still holding the lock. If the filesystem is
    unavailable, the caller receives AUDIT_FAILURE; no fallback log leaks data.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    locked = False
    original_size = None
    try:
        deadline = time.monotonic() + FINALIZE_SECONDS
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError from None
                time.sleep(0.01)
        original_size = os.lseek(fd, 0, os.SEEK_END)
        if original_size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                # An interrupted prior append is not silently joined to a new event.
                raise OSError
            os.lseek(fd, 0, os.SEEK_END)
        remaining = memoryview(_canonical(event) + b"\n")
        if original_size + len(remaining) > 64 * 1024 * 1024:
            raise OSError
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(fd)
    except Exception:
        if locked and original_size is not None:
            os.ftruncate(fd, original_size)
            os.fsync(fd)
        raise
    finally:
        try:
            if locked:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _cleanup(workspace):
    if workspace is None:
        return True
    # Only the fresh mkdtemp child is removed, never temp_root or caller files.
    deadline = time.monotonic() + FINALIZE_SECONDS
    while True:
        try:
            shutil.rmtree(workspace)
        except FileNotFoundError:
            pass
        except OSError:
            pass  # Retry below; exhausted retries become audited CLEANUP_FAILURE.
        try:
            # On Windows a delete-pending directory can fail a direct path
            # probe while still appearing in its parent's directory listing.
            # Close the enumeration handle before retrying the removal.
            with os.scandir(workspace.parent) as entries:
                listed = any(entry.name == workspace.name for entry in entries)
            if not listed and not os.path.lexists(workspace):
                return True
        except FileNotFoundError:
            # The parent itself is gone; there can be no remaining child entry.
            return True
        except OSError:
            pass  # Inability to verify absence must never report success.
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _worker_result(message):
    if type(message) is not dict or set(message) != {
        "outcome", "reason_codes", "normalization", "output_sha256",
        "adapter_version", "policy_hash",
    }:
        raise ValueError
    if message["outcome"] not in {
        "accepted", "quarantined", "invalid_request", "operational_failure"
    }:
        raise ValueError
    codes = message["reason_codes"]
    if type(codes) is not list or any(type(c) is not str or
            re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", c) is None for c in codes):
        raise ValueError
    for key in ("adapter_version", "policy_hash"):
        if message[key] is not None and (type(message[key]) is not str or
                re.fullmatch(r"[a-f0-9]{64}", message[key]) is None):
            raise ValueError
    portfolio = message["normalization"]
    if message["outcome"] == "accepted":
        if type(portfolio) is not dict or not (portfolio.get("positions") or portfolio.get("cash")):
            raise ValueError
        if hashlib.sha256(_canonical(portfolio)).hexdigest() != message["output_sha256"]:
            raise ValueError
    elif portfolio is not None or message["output_sha256"] is not None or not codes:
        raise ValueError
    return message


def process_upload(upload: bytes, metadata: dict, *, engine_root: Path,
                   audit_path: Path, temp_root: Path,
                   timeout_seconds: float = 10.0) -> dict:
    """Normalize one upload, destroy its workspace, then append one private audit.

    timeout_seconds bounds engine work including import and SQLite operations.
    Killing/reaping, confirmed cleanup and auditing follow the work deadline;
    they must finish before returning and are not skipped on timeout.
    """
    started = time.monotonic()
    started_at = _now()
    trace = TRACE_ID.get()
    if type(trace) is not str or not re.fullmatch(r"[a-f0-9]{32}", trace):
        trace = uuid.uuid4().hex
    result = dict(outcome="invalid_request", request_id=trace,
                  reason_codes=[], normalization=None, output_sha256=None,
                  cleanup_result="OK")
    adapter = None
    adapter_version = policy_hash = None
    workspace = process = None
    input_hash = hashlib.sha256(upload).hexdigest() if type(upload) is bytes else None
    try:
        invalid = _validate(upload, metadata)
        if type(metadata) is dict and type(metadata.get("broker")) is str:
            adapter = metadata["broker"] if metadata["broker"] in PROFILES else None
        if invalid:
            result["reason_codes"] = [invalid]
        elif adapter is None:
            result.update(outcome="quarantined",
                          reason_codes=["UNRECOGNIZED_BROKER_REVIEW_REQUIRED"])
        else:
            if (type(timeout_seconds) not in (int, float) or
                    not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
                raise ValueError
            deadline = started + timeout_seconds
            engine_root = Path(engine_root).resolve()
            temp_root = Path(temp_root).resolve()
            audit_path = Path(audit_path).resolve()
            if temp_root.is_relative_to(engine_root) or audit_path.is_relative_to(engine_root):
                raise ValueError
            workspace = Path(tempfile.mkdtemp(prefix="request-", dir=temp_root))
            packet = _canonical(dict(upload=base64.b64encode(upload).decode("ascii"),
                                     metadata=metadata, engine_root=str(engine_root)))
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired([], timeout_seconds)
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(WORKER)], cwd=workspace,
                executable=sys.executable,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=runtime_environment(),
            )
            output, _ = process.communicate(packet, timeout=max(0, deadline - time.monotonic()))
            if process.returncode != 0:
                raise RuntimeError
            message = _worker_result(json.loads(output))
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired([], timeout_seconds)
            adapter_version, policy_hash = message["adapter_version"], message["policy_hash"]
            result.update({k: message[k] for k in (
                "outcome", "reason_codes", "normalization", "output_sha256")})
        # Force serialization before cleanup/audit; never discover invalid output afterward.
        _canonical(result)
    except subprocess.TimeoutExpired:
        _failure(result, "TIMEOUT")
    except Exception:
        _failure(result, "REQUEST_INFRASTRUCTURE_FAILURE")
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=FINALIZE_SECONDS)
                if process.returncode is None:
                    raise RuntimeError
        except Exception:
            _failure(result, "WORKER_TERMINATION_FAILURE")
            result["cleanup_result"] = "FAIL"
        try:
            if not _cleanup(workspace):
                result["cleanup_result"] = "FAIL"
        except Exception:
            result["cleanup_result"] = "FAIL"
        if result["cleanup_result"] == "FAIL":
            _failure(result, "CLEANUP_FAILURE")
    event = dict(request_id=result["request_id"], input_sha256=input_hash,
                 output_sha256=result["output_sha256"], adapter=adapter,
                 adapter_version=adapter_version, policy_hash=policy_hash,
                 started_at=started_at, completed_at=_now(), outcome=result["outcome"],
                 reason_codes=result["reason_codes"],
                 duration_ms=round((time.monotonic() - started) * 1000, 3),
                 cleanup_result=result["cleanup_result"])
    try:
        # Also protect the read-only artifact for requests rejected before worker startup.
        if Path(audit_path).resolve().is_relative_to(Path(engine_root).resolve()):
            raise ValueError
        _append_audit(audit_path, event)
    except Exception:
        _failure(result, "AUDIT_FAILURE")
    return result
