"""Mechanical proof that the runtime pulls in no network module.

An import-graph check rather than a source grep: a probe subprocess imports each runtime
module into a clean isolated interpreter and reports what actually appeared in sys.modules,
so a network module reached transitively is caught the same as a direct import. Adding
`import urllib.request` to any of the four fails test_runtime_modules_import_no_network_module.

The detector is proved non-vacuous on every run by a negative control: dev_mapping is a
dev-only tool that really does call the local model, and the same check must flag it.
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULES = ("broker_engine", "broker_adapters", "durable_store", "broker_cli")
NETWORK_ROOTS = ("socket", "socketserver", "http", "ssl", "requests", "urllib3",
                 "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "asyncio")
# urllib.parse is pure string parsing that opens nothing, and CPython 3.12 pathlib imports
# it at module level, so durable_store reaches it without asking. It is the only allowed
# name under urllib; request/error/response are the network ones and stay forbidden.
ALLOWED_URLLIB = frozenset({"urllib", "urllib.parse"})
NETWORK_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(?:" + "|".join(("urllib",) + NETWORK_ROOTS) + r")\b", re.M)

PROBE = r"""
import json, sys
root, targets = sys.argv[1], sys.argv[2].split(",")
sys.path.insert(0, root)
report = {"baseline": sorted(sys.modules), "modules": {}}
for name in targets:
    before = set(sys.modules)
    __import__(name)
    report["modules"][name] = sorted(set(sys.modules) - before)
print(json.dumps(report))
"""


def probe(modules):
    """Import each module in a clean interpreter and report what it dragged in."""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", PROBE, str(ROOT), ",".join(modules)],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    # A crashed probe proves nothing; never let it read as a pass.
    if result.returncode != 0:
        raise AssertionError(f"probe exited {result.returncode}: {result.stderr.strip()[-2000:]}")
    return json.loads(result.stdout)


def network_names(imported):
    hits = []
    for name in imported:
        root = name.split(".")[0]
        if root == "urllib":
            if name not in ALLOWED_URLLIB:
                hits.append(name)
        elif root in NETWORK_ROOTS:
            hits.append(name)
    return sorted(hits)


class RuntimeIsolation(unittest.TestCase):
    def test_runtime_modules_import_no_network_module(self):
        report = probe(RUNTIME_MODULES)
        self.assertEqual(network_names(report["baseline"]), [],
                         "interpreter baseline already held a network module; probe is unsound")
        for name in RUNTIME_MODULES:
            with self.subTest(module=name):
                self.assertEqual(network_names(report["modules"][name]), [])

    def test_network_detector_flags_dev_mapping_negative_control(self):
        # dev_mapping is dev-only and genuinely imports urllib.request. If the detector
        # cannot see that, it cannot see it in a runtime module either.
        hits = network_names(probe(("dev_mapping",))["modules"]["dev_mapping"])
        for expected in ("urllib.request", "socket", "http.client", "ssl"):
            self.assertIn(expected, hits)

    def test_only_urllib_parse_is_reachable_and_no_runtime_source_imports_it(self):
        report = probe(RUNTIME_MODULES)
        reached = {name for imported in report["modules"].values() for name in imported
                   if name.split(".")[0] == "urllib"}
        self.assertLessEqual(reached, ALLOWED_URLLIB)
        # The allowance is for a stdlib detail, not for artifact code: none of the four may
        # name urllib itself, so the exception cannot be used to smuggle urllib.request in.
        for name in RUNTIME_MODULES:
            with self.subTest(module=name):
                source = (ROOT / (name + ".py")).read_text(encoding="utf-8")
                self.assertIsNone(NETWORK_IMPORT.search(source))


if __name__ == "__main__":
    unittest.main()
