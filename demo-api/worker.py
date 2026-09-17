"""Private fixed Python worker. Request values arrive over stdin, never argv.

No CLI execution, global engine patching, persistent payload files or network.
The parent forcibly ends this process on deadline and removes its working dir.
"""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def _network_guard(event, args):
    if event.startswith(("socket.", "ctypes.")) or event in (
            "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork"):
        raise RuntimeError


def _resource_limits():
    if sys.platform == "linux":
        import resource
        for kind, limit in ((resource.RLIMIT_AS, 256 * 1024 * 1024),
                            (resource.RLIMIT_FSIZE, 32 * 1024 * 1024),
                            (resource.RLIMIT_CPU, 15), (resource.RLIMIT_CORE, 0)):
            resource.setrlimit(kind, (limit, limit))


def _capture_bytes(capture, raw, broker):
    if broker == "fidelity_csv":
        return json.dumps(capture, separators=(",", ":"), allow_nan=False).encode()
    # wrap already ran the engine's strict JSON reader. Carry those validated raw
    # JSON numeric tokens verbatim, avoiding Decimal -> float rounding (or strings).
    page = capture["pages"][0]
    page_json = "{" + ",".join(
        json.dumps(k) + ":" + (raw.decode("utf-8-sig") if k == "payload" else json.dumps(v))
        for k, v in page.items()) + "}"
    return ("{" + ",".join(json.dumps(k) + ":" +
            ("[" + page_json + "]" if k == "pages" else json.dumps(v, allow_nan=False))
            for k, v in capture.items()) + "}").encode()


def run(packet):
    result = dict(outcome="operational_failure", reason_codes=["ENGINE_FAILURE"],
                  normalization=None, output_sha256=None, adapter_version=None,
                  policy_hash=None)
    try:
        root = Path(packet["engine_root"])
        sys.path.insert(0, str(root))
        from broker_engine import Engine, Rejected, read_json
        from durable_store import Store, fingerprint
        from raw_intake import IntakeError, wrap

        reference_bytes = (root / "reference.json").read_bytes()
        policy = hashlib.sha256(reference_bytes)
        for name in ("broker_engine.py", "broker_adapters.py", "durable_store.py",
                     "broker_cli.py", "raw_intake.py"):
            source = (root / name).read_bytes()
            policy.update(name.encode())
            policy.update(source)
            if name == "broker_adapters.py":
                result["adapter_version"] = hashlib.sha256(source).hexdigest()
        result["policy_hash"] = policy.hexdigest()
        engine = Engine(read_json(reference_bytes))
        raw = base64.b64decode(packet["upload"], validate=True)
        metadata = packet["metadata"]
        try:
            # Explicit compatibility bridge required by the frozen handoff. Only
            # the envelope field differs; all payload and business checks remain.
            capture = wrap(raw, metadata["broker"], metadata["account"],
                           metadata["as_of"], metadata["currency"], "holdings")
            capture["context"]["total_scope"] = metadata["total_scope"]
        except IntakeError:
            result.update(outcome="invalid_request", reason_codes=["INVALID_RAW_INTAKE"])
            return result
        except Rejected as error:
            result.update(outcome="invalid_request", reason_codes=[error.code])
            return result
        body = _capture_bytes(capture, raw, metadata["broker"])
        with Store(Path.cwd() / "request.sqlite", engine, result["policy_hash"]) as store:
            event = store.ingest(body, "upload")
            proof = store.verify()
            report = store.report()
            if proof["ok"] is not True or proof["attempts"] != 1:
                raise RuntimeError
            if event["status"] == "QUARANTINED":
                if (proof["accepted_snapshots"] != 0 or proof["current_accounts"] != 0 or
                        proof["quarantined"] != 1 or report["current"]):
                    raise RuntimeError
                code = event["code"]
                if type(code) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", code):
                    raise RuntimeError
                result.update(outcome="invalid_request" if code == "INVALID_CSV" else "quarantined",
                              reason_codes=[code])
            elif event["status"] == "ACCEPTED":
                if (proof["accepted_snapshots"] != 1 or proof["current_accounts"] != 1 or
                        proof["quarantined"] != 0 or len(report["current"]) != 1):
                    raise RuntimeError
                portfolio = report["current"][0]
                if not (portfolio["positions"] or portfolio["cash"]):
                    result["reason_codes"] = ["EMPTY_NORMALIZATION"]
                    return result
                digest = fingerprint(portfolio)
                if digest != event["fingerprint"] or portfolio["broker"] != metadata["broker"] or \
                        portfolio["account"] != metadata["account"] or portfolio["as_of"] != metadata["as_of"]:
                    raise RuntimeError
                result.update(outcome="accepted", reason_codes=[],
                              normalization=portfolio, output_sha256=digest)
            else:
                # REPLAY cannot occur in a fresh one-request store.
                raise RuntimeError
        return result
    except Exception:
        result.update(outcome="operational_failure", reason_codes=["ENGINE_FAILURE"],
                      normalization=None, output_sha256=None)
        return result


def main():
    sys.dont_write_bytecode = True
    sys.addaudithook(_network_guard)
    try:
        _resource_limits()
        packet = json.loads(sys.stdin.buffer.read())
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), \
                contextlib.redirect_stderr(sink):
            result = run(packet)
        sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))
        sys.stdout.flush()
        return 0
    except Exception:
        # Parent detects nonzero/protocol failure; never emit exception text.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
