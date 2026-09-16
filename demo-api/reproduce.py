"""Offline regression controls: Windows startup, guarded launch and Plaid identity.

FAIL events marked expected_rejection are deliberately reproduced failures.
Exit zero requires the corresponding repaired path to succeed as well.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import core
from smoke import emit

HERE = Path(__file__).resolve().parent


def main():
    try:
        if os.name == "nt":
            code = "import asyncio.windows_events; print('RUNTIME_OK')"
            for stripped in (True, False):
                p = subprocess.run([sys.executable, "-I", "-B", "-c", code],
                    env={} if stripped else core.runtime_environment(), capture_output=True, timeout=10)
                if stripped:
                    if p.returncode == 0 or b"10106" not in p.stderr:
                        raise RuntimeError
                    emit("FAIL", "empty_windows_environment", winerror=10106, expected_rejection=True)
                else:
                    if p.returncode or p.stdout.strip() != b"RUNTIME_OK" or p.stderr:
                        raise RuntimeError
                    emit("OK", "minimal_windows_environment")
        else:
            emit("WARN", "windows_reproduction_unavailable", platform=sys.platform)
        with tempfile.TemporaryDirectory(prefix="repro-test-", dir=HERE) as scratch:
            root = Path(scratch)
            request = root/"requests"/"request-synthetic"
            request.mkdir(parents=True)
            # Keep the installed production guard. Deliberately reproduce the old
            # call's missing executable, then exercise the repaired call verbatim.
            code = f"""
import base64, json, os, pathlib, subprocess, sys
sys.path.insert(0, {str(HERE)!r})
import core, service
root = pathlib.Path({str(root)!r})
example = json.loads((service.HERE/'examples'/'examples.json').read_bytes())[0]
packet = core._canonical(dict(upload=base64.b64encode((service.HERE/'examples'/example['file']).read_bytes()).decode(), metadata=example['metadata'], engine_root=str(service.HERE/'engine')))
service.install_egress_guard(service.Config(state=root))
args = [sys.executable, '-I', '-B', str(core.WORKER)]
options = dict(cwd=root/'requests'/'request-synthetic', env=core.runtime_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if os.name == 'nt':
    try:
        p = subprocess.Popen(args, **options)
    except RuntimeError:
        print(json.dumps(dict(severity='FAIL', check='implicit_windows_executable', expected_rejection=True)))
    else:
        p.kill(); p.communicate(timeout=5); raise SystemExit(2)
p = subprocess.Popen(args, executable=sys.executable, **options)
stdout, stderr = p.communicate(packet, timeout=15)
if p.returncode or stderr or json.loads(stdout)['outcome'] != 'accepted': raise SystemExit(3)
print(json.dumps(dict(severity='OK', check='explicit_executable_guarded_worker')))
"""
            p = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=HERE,
                               env=core.runtime_environment(), capture_output=True, timeout=25)
            if p.returncode or p.stderr or not p.stdout:
                raise RuntimeError
            for line in p.stdout.splitlines():
                event = json.loads(line)
                emit(**event)
            example = json.loads((HERE/"examples"/"plaid.json").read_bytes())
            metadata = json.loads((HERE/"examples"/"examples.json").read_bytes())[1]["metadata"]
            for reviewed in (False, True):
                identity = "synthetic-plaid-aapl" if reviewed else "unreviewed-plaid-aapl"
                example["securities"][0]["security_id"] = identity
                example["holdings"][0]["security_id"] = identity
                result = core.process_upload(json.dumps(example).encode(), metadata,
                    engine_root=HERE/"engine", temp_root=root, audit_path=root/"audit.jsonl")
                if result["outcome"] != ("accepted" if reviewed else "quarantined") or result["cleanup_result"] != "OK":
                    raise RuntimeError
                if not reviewed and result["reason_codes"] != ["UNRESOLVED_IDENTITY"]:
                    raise RuntimeError
                emit("OK" if reviewed else "WARN", "plaid_identity_control", reviewed=reviewed,
                     outcome=result["outcome"], reason_codes=result["reason_codes"], expected_rejection=not reviewed)
        emit("OK", "reproduction_complete")
        return 0
    except Exception as error:
        emit("FAIL", "reproduction_incomplete", error_type=type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
