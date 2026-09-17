"""Offline proof runner. Every write stays below demo-api/proof; no publication."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True,
                        help="Read-only original artifact for its full, byte-identical checkpoint")
    args = parser.parse_args()
    proof = HERE / "proof" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    def event(severity, check, **fields):
        record = dict(at=datetime.now(timezone.utc).isoformat(), severity=severity, check=check, **fields)
        try:
            with (proof / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            print('FAIL: proof log unavailable', file=sys.stderr)
            raise

    def command(label, argv, cwd, suite=False, completion=None):
        invocation = [sys.executable, "-B", *argv]
        event("OK", label + "_started", command=invocation, cwd=str(cwd))
        try:
            p = subprocess.run(invocation, cwd=cwd, capture_output=True, timeout=240,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            (proof / (label+".stdout.log")).write_bytes(error.stdout or b"")
            (proof / (label+".stderr.log")).write_bytes(error.stderr or b"")
            event("FAIL", label, reason="COMMAND_TIMEOUT", command=invocation, cwd=str(cwd))
            raise
        (proof / (label+".stdout.log")).write_bytes(p.stdout)
        (proof / (label+".stderr.log")).write_bytes(p.stderr)
        if p.returncode:
            event("FAIL", label, exit_code=p.returncode, command=invocation, cwd=str(cwd))
            raise RuntimeError
        if not (p.stdout.strip() or p.stderr.strip()):
            event("SUSPECT", label, reason="EMPTY_COMMAND_OUTPUT")
            raise RuntimeError
        count = None
        if suite:
            match = re.search(rb"Ran ([0-9]+) tests? in", p.stderr)
            if not match or int(match[1]) == 0 or not re.search(rb"\nOK\s*$", p.stderr):
                event("SUSPECT", label, reason="NO_COMPLETE_NONEMPTY_SUITE")
                raise RuntimeError
            count = int(match[1])
        if completion:
            records = [json.loads(line) for line in p.stdout.splitlines()]
            if not records or records[-1].get("check") != completion or records[-1].get("severity") != "OK":
                raise RuntimeError
            for record in records:
                if record["severity"] == "FAIL" and not record.get("expected_rejection"):
                    raise RuntimeError
                event(**record)
        event("OK", label, exit_code=p.returncode, tests=count, command=invocation, cwd=str(cwd))

    def inventory(root):
        return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    try:
        proof.mkdir(parents=True)
        if sys.version_info[:2] != (3, 12):
            event("FAIL", "required_python_312", runtime=sys.version.split()[0])
            raise RuntimeError
        event("OK", "required_python_312", runtime=sys.version.split()[0], executable=sys.executable)
        manifest = json.loads((HERE / "artifact-source-manifest.json").read_bytes())
        if not manifest or "checkpoint.py" not in manifest or not any(n.startswith("tests/") for n in manifest):
            raise ValueError
        original = args.artifact.resolve()
        before = inventory(original)
        if not before:
            event("SUSPECT", "empty_original_artifact")
            raise ValueError
        (proof / "original-before.json").write_text(json.dumps(before, indent=2, sort_keys=True), encoding="utf-8")
        clone = proof / "artifact-checkpoint"
        for name, expected in manifest.items():
            source = (original / name).resolve()
            destination = (clone / name).resolve()
            if not source.is_relative_to(original) or not destination.is_relative_to(clone.resolve()):
                raise ValueError
            body = source.read_bytes()
            if hashlib.sha256(body).hexdigest() != expected:
                event("FAIL", "artifact_source_drift")
                raise ValueError
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
        runtime = json.loads((HERE / "engine-manifest.json").read_bytes())
        if len(runtime) != 6:
            raise ValueError
        for name, digest in runtime.items():
            if (hashlib.sha256((HERE / "engine" / name).read_bytes()).hexdigest() != digest or
                    digest != manifest[name]):
                event("FAIL", "engine_semantics_drift")
                raise ValueError
        event("OK", "byte_identical_checkpoint_and_engine", files=len(manifest))
        command("artifact-full-checkpoint", ["checkpoint.py"], clone)
        summaries = list((clone/"checkpoints").glob("*/summary.json"))
        if len(summaries) != 1:
            raise RuntimeError
        checkpoint = json.loads(summaries[0].read_bytes())
        suite_output = (summaries[0].parent/"tests.stderr.txt").read_bytes()
        match = re.search(rb"Ran ([0-9]+) tests? in", suite_output)
        if checkpoint["status"] != "PASS" or not match or int(match[1]) == 0:
            raise RuntimeError
        (proof/"artifact-summary.json").write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
        (proof/"artifact-tests.stderr.log").write_bytes(suite_output)
        event("OK", "artifact_checkpoint_nonempty", tests=int(match[1]), first_ingest=checkpoint["first_ingest"])
        command("core-tests", ["-m", "unittest", "discover", "-s", ".", "-p", "test_core.py", "-v"], HERE, suite=True)
        command("service-tests", ["-m", "unittest", "discover", "-s", ".", "-p", "test_service.py", "-v"], HERE, suite=True)
        command("failure-reproduction", ["reproduce.py"], HERE, completion="reproduction_complete")
        command("real-loopback-examples", ["smoke.py"], HERE, completion="smoke_complete")
        # Original sources remain byte-identical after all subprocess tests.
        after = inventory(original)
        (proof/"original-after.json").write_text(json.dumps(after, indent=2, sort_keys=True), encoding="utf-8")
        if before != after or any(hashlib.sha256((clone/name).read_bytes()).hexdigest() != digest for name, digest in manifest.items()):
            raise RuntimeError
        if any(hashlib.sha256((HERE/"engine"/name).read_bytes()).hexdigest() != digest for name, digest in runtime.items()):
            raise RuntimeError
        event("OK", "original_artifact_unchanged", files=len(after), clone_inputs_unchanged=True, engine_unchanged=True)
        event("OK", "negative_controls", controls=["unsafe_http", "malformed_payload", "unknown_identity",
              "outbound_tcp_dns_udp_subprocess", "sensitive_logging", "partial_audit_write", "cleanup_visibility"])
        event("WARN", "deployment_boundary", unobserved=["TLS_edge", "Linux_container_resource_limits",
              "host_firewall", "sustained_load"], docker_cli_available=shutil.which("docker") is not None,
              note="Local Python guards are not an OS sandbox; no deployment attempted")
        deliverables = {str(p.relative_to(HERE)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(HERE.iterdir()) if p.is_file() and p.suffix in (".py", ".md", ".json")}
        for name in ("Dockerfile", ".dockerignore"):
            deliverables[name] = hashlib.sha256((HERE/name).read_bytes()).hexdigest()
        for p in sorted((HERE/"examples").iterdir()):
            deliverables["examples/"+p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        (proof/"deliverable-hashes.json").write_text(json.dumps(deliverables, indent=2, sort_keys=True), encoding="utf-8")
        event("OK", "verification_complete")
        print("OK: Python 3.12 full artifact checkpoint and service/core verification")
        print("Proof: " + str(proof))
        return 0
    except Exception as error:
        try:
            event("FAIL", "verification_incomplete", error_type=type(error).__name__)
        except OSError:
            pass  # event() already reports the unavailable durable log to stderr.
        print("FAIL: inspect " + str(proof))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
