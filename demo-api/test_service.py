"""Real loopback HTTP and fault controls; synthetic data only, same-test scratch."""
import asyncio
from dataclasses import replace
import hashlib
import json
import http.client
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

import core
import service
from test_core import RAW, ROW, META, CSV, ACCOUNT, AUDIT_FIELDS, HERE


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="http-test-", dir=HERE)
        self.root = Path(self.scratch.name)
        self.config = service.Config(state=self.root / "state", port=0, edge_tls=False,
                                     origins=("http://localhost:8000",), requests_per_minute=120)
        self.app = service.Service(self.config)
        self.app.prepare()
        self.server = await asyncio.start_server(self.app.accept, "127.0.0.1", 0, limit=8192)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        if self.app.tasks:
            await asyncio.gather(*self.app.tasks)
        self.scratch.cleanup()

    def packet(self, body=RAW, meta=None, headers=None, path="/normalize", method="POST"):
        fields = {"Host": "localhost", "Content-Type": "application/json",
                  "Content-Length": str(len(body)), "Origin": "http://localhost:8000"}
        fields.update({key: (META if meta is None else meta)[field] for key, field in service.HEADER_FIELDS.items()
                       if field in (META if meta is None else meta)})
        fields.update(headers or {})
        return (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k,v in fields.items())+
                "\r\n").encode() + body

    async def request(self, packet=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(self.packet() if packet is None else packet)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 25)
        writer.close()
        await writer.wait_closed()
        head, body = response.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:])
        self.assertEqual(int(headers["Content-Length"]), len(body))
        return int(lines[0].split()[1]), headers, json.loads(body)

    def audit(self):
        return [json.loads(line) for line in (self.config.state / "audit.jsonl").read_text().splitlines()]

    async def test_accept_hash_trace_privacy_and_cleanup(self):
        trace = "1234567890abcdef1234567890abcdef"
        a = await self.request(self.packet(headers={"X-Request-ID": trace}))
        b = await self.request()
        self.assertEqual(a[0], 200)
        self.assertEqual(a[2]["normalization"]["positions"][0]["quantity"], "2")
        self.assertFalse(a[2]["normalization"]["valuation_complete"])
        self.assertEqual(a[2]["output_sha256"], b[2]["output_sha256"])
        self.assertEqual(a[2]["request_id"], trace)
        self.assertEqual(a[1]["X-Request-ID"], trace)
        self.assertEqual(a[1]["Cache-Control"], "no-store")
        self.assertEqual(a[1]["Access-Control-Allow-Origin"], "http://localhost:8000")
        self.assertEqual(list((self.config.state / "requests").iterdir()), [])
        records = self.audit()
        self.assertEqual(records[0]["request_id"], trace)
        for r in records:
            self.assertEqual(set(r), AUDIT_FIELDS)
        raw = (self.config.state / "audit.jsonl").read_text()
        for canary in (ACCOUNT, "AAPL", "asset_id", "positions", "current_price", "holdings", str(self.root)):
            self.assertNotIn(canary, raw)

    async def test_outcome_matrix_and_row_cap(self):
        cases = [(b"{", META, 400, "invalid_request", "INVALID_JSON"),
                 (RAW, {**META, "broker": "UNSUPPORTED-PRIVATE"}, 422, "quarantined", "UNRECOGNIZED_BROKER_REVIEW_REQUIRED"),
                 (json.dumps([{**ROW, "asset_id": "UNSAFE-PRIVATE"}]).encode(), META, 422, "quarantined", "UNRESOLVED_IDENTITY"),
                 (b"[]", META, 503, "operational_failure", "EMPTY_NORMALIZATION"),
                 (json.dumps([ROW]*5001).encode(), META, 422, "quarantined", "TOO_MANY_ROWS"),
                 (b"[NaN]", META, 400, "invalid_request", "NONFINITE_JSON"),
                 (b'{"x":1,"x":2}', META, 400, "invalid_request", "DUPLICATE_JSON_KEY")]
        for raw, meta, status, outcome, code in cases:
            with self.subTest(code=code):
                r = await self.request(self.packet(raw, meta))
                self.assertEqual(r[0], status)
                self.assertEqual(r[2]["outcome"], outcome)
                self.assertIn(code, r[2]["reason_codes"])
                self.assertIsNone(r[2]["normalization"])
        r = await self.request(self.packet(CSV, {**META, "broker": "fidelity_csv"}, {"Content-Type": "text/csv"}))
        self.assertEqual(r[0], 200)

    async def test_oversize_media_metadata_and_unsafe_framing(self):
        packets = [(self.packet(headers={"Content-Length": "2000001"}), 413),
                   (self.packet(headers={"Content-Type": "application/octet-stream"}), 415),
                   (self.packet(headers={"Content-Type": "text/csv"}), 415),
                   (self.packet(headers={"Content-Encoding": "gzip"}), 400),
                   (self.packet(headers={"Content-Disposition": "attachment; filename=PRIVATE.csv"}), 400),
                   (self.packet(headers={"Transfer-Encoding": "chunked"}), 400),
                   (self.packet(headers={"X-Unknown": "private"}), 400),
                   (self.packet(headers={"X-Request-ID": "PRIVATE-ACCOUNT"}), 400),
                   (self.packet(meta={}), 400),
                   (self.packet().replace(b"Host: localhost", b"Host: localhost\r\ncontent-length: 0"), 400),
                   (self.packet().replace(b"Host: localhost", b"Host : localhost"), 400),
                   (self.packet(path="/normalize?account=PRIVATE"), 404),
                   (self.packet(method="PUT"), 405)]
        for packet, status in packets:
            self.assertEqual((await self.request(packet))[0], status)
        self.assertNotIn("PRIVATE", (self.config.state / "audit.jsonl").read_text())

    async def test_cors_preflight_and_tls(self):
        r = await self.request(self.packet(headers={"Origin": "https://evil.example"}))
        self.assertEqual(r[0], 403)
        self.assertNotIn("Access-Control-Allow-Origin", r[1])
        r = await self.request(self.packet(b"", method="OPTIONS", headers={
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": service.CORS_HEADERS}))
        self.assertEqual(r[0], 200)
        self.assertEqual(r[1]["Access-Control-Allow-Methods"], "POST")
        self.assertNotIn("Access-Control-Allow-Credentials", r[1])
        self.app.config = replace(self.config, edge_tls=True)
        self.assertEqual((await self.request())[2]["reason_codes"], ["TLS_REQUIRED"])
        self.assertEqual((await self.request(self.packet(headers={"X-Forwarded-Proto": "https"})))[0], 200)

    async def test_health_readiness_dependency_and_audit_faults(self):
        self.assertEqual((await self.request(self.packet(b"", path="/health", method="GET")))[0], 200)
        self.assertEqual((await self.request(self.packet(b"", path="/ready", method="GET")))[0], 200)
        self.app.config = replace(self.config, engine=self.root / "absent")
        self.assertEqual((await self.request(self.packet(b"", path="/ready", method="GET")))[0], 503)
        self.assertEqual((await self.request())[2]["reason_codes"], ["DEPENDENCY_UNAVAILABLE"])
        self.app.config = self.config
        with mock.patch.object(core, "_append_audit", side_effect=OSError("PRIVATE-ERROR")):
            r = await self.request()
        self.assertEqual(r[0], 503)
        self.assertIsNone(r[2]["normalization"])
        self.assertEqual(self.app.fault, "SERVICE_REQUIRES_ATTENTION")
        self.assertNotIn("PRIVATE-ERROR", str(r))

    async def test_rate_and_concurrency_admission(self):
        self.app.tokens = 0
        r = await self.request()
        self.assertEqual(r[0], 429)
        self.assertIn("Retry-After", r[1])
        self.app.tokens = 120
        self.app.config = replace(self.config, workers=1)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        packet = self.packet()
        writer.write(packet[:-len(RAW)])
        await writer.drain()
        for _ in range(100):
            if self.app.jobs == 1:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.app.jobs, 1)
        r = await self.request()
        self.assertEqual(r[0], 503)
        self.assertEqual(r[2]["reason_codes"], ["CAPACITY_EXCEEDED"])
        writer.write(RAW)
        await writer.drain()
        self.assertIn(b'"outcome":"accepted"', await reader.read())
        writer.close()
        await writer.wait_closed()
        self.assertEqual(self.app.jobs, 0)

    async def test_slow_headers_and_body_have_absolute_deadline(self):
        self.app.config = replace(self.config, request_seconds=15.2, work_seconds=8)
        # Read budget = 0.2 seconds, independent of byte arrival cadence.
        for payload in (b"POST /normalize HTTP/1.1\r\n", self.packet()[:-len(RAW)]):
            start = time.monotonic()
            r = await self.request(payload)
            self.assertEqual(r[0], 408)
            self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(self.app.jobs, 0)

    async def test_worker_timeout_and_cleanup(self):
        script = self.root / "sleep.py"
        script.write_text("from pathlib import Path\nimport time\nPath('private').write_text('PRIVATE')\ntime.sleep(10)\n")
        self.app.config = replace(self.config, work_seconds=.2)
        with mock.patch.object(core, "WORKER", script):
            r = await self.request()
        self.assertEqual(r[0], 504)
        self.assertEqual(r[2]["reason_codes"], ["TIMEOUT"])
        self.assertEqual(list((self.config.state / "requests").iterdir()), [])
        self.assertEqual(self.audit()[-1]["reason_codes"], ["TIMEOUT"])

    async def test_connection_ceiling_and_request_isolation(self):
        self.app.config = replace(self.config, connections=1)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        await asyncio.sleep(.02)
        other, other_writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.assertEqual(await asyncio.wait_for(other.read(), 1), b"")
        other_writer.close()
        await other_writer.wait_closed()
        writer.close()
        await writer.wait_closed()
        await reader.read()
        await asyncio.sleep(.05)
        self.app.config = self.config
        async def invoke(i):
            account = ACCOUNT+str(i)
            raw = json.dumps([{**ROW, "account_id": account, "qty": str(i+1)}]).encode()
            return await self.request(self.packet(raw, {**META, "account": account}))
        a, b = await asyncio.gather(invoke(0), invoke(1))
        for i, result in enumerate((a,b)):
            self.assertEqual(result[0], 200)
            self.assertEqual(result[2]["normalization"]["account"], ACCOUNT+str(i))
            self.assertEqual(result[2]["normalization"]["positions"][0]["quantity"], str(i+1))
        self.assertNotEqual(a[2]["output_sha256"], b[2]["output_sha256"])
        self.assertEqual(list((self.config.state / "requests").iterdir()), [])


class PolicyTests(unittest.TestCase):
    def test_real_service_entrypoint_guard_and_private_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="entry-test-", dir=HERE) as path:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            env = core.runtime_environment()
            env.update(DEMO_STATE=str(Path(path)/"state"), DEMO_LOCAL_HTTP="1", DEMO_PORT=str(port))
            process = subprocess.Popen([sys.executable, "-B", str(HERE/"service.py")],
                cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                for _ in range(100):
                    connection = None
                    try:
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        connection.request("GET", "/ready")
                        response = connection.getresponse()
                        body = response.read()
                        self.assertEqual(response.status, 200, body)
                        break
                    except OSError:
                        if process.poll() is not None:
                            self.fail("entrypoint stopped")
                        time.sleep(.05)
                    finally:
                        if connection is not None:
                            connection.close()
                else:
                    self.fail("entrypoint never became ready")
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", "/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["reason_codes"], ["HEALTHY"])
                connection.close()
                headers = {key: META[field] for key,field in service.HEADER_FIELDS.items()}
                headers["Content-Type"] = "application/json"
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                connection.request("POST", "/normalize", RAW, headers)
                response = connection.getresponse()
                result = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200, result)
                self.assertEqual(result["normalization"]["account"], ACCOUNT)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", "/PRIVATE-FILENAME.csv?account=PRIVATE")
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
                connection.close()
                self.assertEqual(list((Path(path)/"state"/"requests").iterdir()), [])
            finally:
                process.terminate()
                stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(stdout, b"")
            self.assertEqual(stderr, b"")
            audit = (Path(path)/"state"/"audit.jsonl").read_text()
            for canary in ("PRIVATE", "AAPL", "positions", "account_id"):
                self.assertNotIn(canary, audit)

    def test_bundled_examples_are_executable(self):
        examples = json.loads((HERE/"examples"/"examples.json").read_bytes())
        self.assertEqual(len(examples), 4)
        with tempfile.TemporaryDirectory(prefix="example-test-", dir=HERE) as path:
            root = Path(path)
            for example in examples:
                result = core.process_upload((HERE/"examples"/example["file"]).read_bytes(), example["metadata"],
                    engine_root=HERE/"engine", temp_root=root, audit_path=root/"audit.jsonl")
                self.assertEqual(result["outcome"], example["expected_outcome"], example["file"])
                self.assertEqual(result["cleanup_result"], "OK")
                if result["outcome"] == "accepted":
                    self.assertTrue(result["normalization"]["positions"] or result["normalization"]["cash"])
                else:
                    self.assertEqual(result["reason_codes"], ["UNRESOLVED_IDENTITY"])

    def test_plaid_unreviewed_identity_still_quarantines(self):
        example = json.loads((HERE/"examples"/"plaid.json").read_bytes())
        metadata = json.loads((HERE/"examples"/"examples.json").read_bytes())[1]["metadata"]
        example["securities"][0]["security_id"] = "unreviewed-plaid-aapl"
        example["holdings"][0]["security_id"] = "unreviewed-plaid-aapl"
        with tempfile.TemporaryDirectory(prefix="example-test-", dir=HERE) as path:
            result = core.process_upload(json.dumps(example).encode(), metadata,
                engine_root=HERE/"engine", temp_root=Path(path), audit_path=Path(path)/"audit.jsonl")
        self.assertEqual(result["outcome"], "quarantined")
        self.assertEqual(result["reason_codes"], ["UNRESOLVED_IDENTITY"])

    @unittest.skipUnless(os.name == "nt", "Windows runtime regression")
    def test_minimal_windows_runtime_and_no_environment_secrets(self):
        with mock.patch.dict(os.environ, {"PRIVATE_TOKEN": "PRIVATE-CREDENTIAL",
                                         "PYTHONPATH": "PRIVATE-PATH", "HTTP_PROXY": "PRIVATE-PROXY"}):
            env = core.runtime_environment()
        self.assertEqual(set(env), {"SystemRoot", "WINDIR"})
        code = "import asyncio.windows_events, os; assert set(os.environ) <= {'SYSTEMROOT', 'WINDIR'}; print('RUNTIME_OK')"
        p = subprocess.run([sys.executable, "-I", "-B", "-c", code], env=env,
                           capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), b"RUNTIME_OK")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                core.runtime_environment()

    def test_config_refuses_unsafe_settings(self):
        for options in ({"origins": ("*",)}, {"origins": ("null",)}, {"origins": ("https://x/y",)},
                        {"host": "0.0.0.0", "edge_tls": False}, {"workers": 5},
                        {"request_seconds": float("nan")}, {"connections": 500}):
            with self.assertRaises(ValueError):
                service.Config(**options).validate()

    def test_network_negative_controls_in_actual_guard_processes(self):
        for guard in ("import worker; sys.addaudithook(worker._network_guard)",
                      "import service; service.install_egress_guard(service.Config())"):
            code = "import sys, socket, subprocess\n" + guard + "\n" + """
checks = [lambda: socket.create_connection(('127.0.0.1',9)),
          lambda: socket.getaddrinfo('example.com',443),
          lambda: socket.socket().sendto(b'PRIVATE',('127.0.0.1',9)),
          lambda: subprocess.run([sys.executable,'-c',"print('PRIVATE')"])]
for check in checks:
    try: check()
    except RuntimeError: pass
    else: raise SystemExit(7)
print('OK blocked outbound TCP DNS UDP and arbitrary subprocess')
"""
            p = subprocess.run([sys.executable, "-B", "-c", code], cwd=HERE, capture_output=True, timeout=10)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn(b"PRIVATE", p.stdout+p.stderr)

    def test_pinned_engine_and_stale_state_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=HERE) as path:
            config = service.Config(state=Path(path)/"state")
            app = service.Service(config)
            app.prepare()
            self.assertIsNone(app.fault)
            (config.state/"requests"/"request-prior").mkdir()
            app = service.Service(config)
            app.prepare()
            self.assertEqual(app.fault, "STALE_REQUEST_STATE")
            self.assertTrue((config.state/"requests"/"request-prior").exists())
            with mock.patch.object(service.hashlib, "sha256") as h:
                h.return_value.hexdigest.return_value = "wrong"
                self.assertEqual(service.readiness(config), "DEPENDENCY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
