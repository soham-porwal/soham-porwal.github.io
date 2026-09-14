"""prepare_examples.py regenerates reviewed state, so it must never quietly destroy any.

reference.json and captures/provenance.json are reviewed registries that later work
appends to. A regeneration that rewrote them from its own sources alone would drop those
additions while still printing success, and the damage would only surface later as an
unrelated-looking test failure. These tests run the real script against a throwaway copy
of the artifact and assert that it does not.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ARTIFACT = Path(__file__).resolve().parent.parent
ROOT_FILES = ("broker_engine.py", "broker_adapters.py", "prepare_examples.py", "reference.json")


class PrepareExamplesPreservesReviewedState(unittest.TestCase):
    def workspace(self):
        """A throwaway copy; the script writes in place, so it never touches the artifact."""
        root = Path(tempfile.mkdtemp(prefix="prepare-examples-"))
        self.addCleanup(shutil.rmtree, root, True)
        for name in ROOT_FILES:
            shutil.copy2(ARTIFACT / name, root / name)
        shutil.copytree(ARTIFACT / "captures", root / "captures")
        # The script reads only the json/csv fixtures; the archived html pages are large.
        shutil.copytree(ARTIFACT / "sources", root / "sources",
                        ignore=shutil.ignore_patterns("*.html"))
        return root

    def regenerate(self, root, expected_exit=0):
        result = subprocess.run([sys.executable, "-B", "prepare_examples.py"], cwd=root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
        return result

    def digests(self, root):
        paths = [root / "reference.json", *sorted((root / "captures").glob("*.json"))]
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paths}

    def registry(self, root):
        return (json.loads((root / "reference.json").read_text(encoding="utf-8")),
                json.loads((root / "captures" / "provenance.json").read_text(encoding="utf-8")))

    def test_regeneration_leaves_the_committed_tree_byte_identical(self):
        # Derived, not hardcoded: whatever reviewed state is committed today must survive,
        # so this assertion cannot go stale as later work adds more of it.
        root = self.workspace()
        before = self.digests(root)
        self.regenerate(root)
        self.assertEqual(self.digests(root), before)

    def test_regeneration_is_idempotent(self):
        root = self.workspace()
        self.regenerate(root)
        once = self.digests(root)
        self.regenerate(root)
        self.assertEqual(self.digests(root), once)

    def test_reviewed_rows_it_did_not_author_survive_regeneration(self):
        root = self.workspace()
        reference, provenance = self.registry(root)
        reference["version"] = "test-reviewed-marker-v9"
        reference["instruments"].append(
            {"id": "test:FOREIGN", "type": "equity", "currency": "USD", "multiplier": "1",
             "status": "active"})
        reference["aliases"].append(
            {"broker": "plaid", "scheme": "ticker", "value": "FOREIGN",
             "instrument_id": "test:FOREIGN", "from": "2000-01-01T00:00:00Z",
             "until": "2100-01-01T00:00:00Z"})
        provenance.append({"file": "captures/99-foreign.json", "sha256": "0" * 64,
                           "class": "synthetic_edge_case", "capture_as_of_is_authored": True})
        (root / "reference.json").write_text(json.dumps(reference, indent=2) + "\n",
                                             encoding="utf-8")
        (root / "captures" / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n",
                                                           encoding="utf-8")
        foreign_capture = root / "captures" / "99-foreign.json"
        foreign_capture.write_bytes(b'{"authored":"by another session"}\n')

        self.regenerate(root)

        rewritten, rewritten_provenance = self.registry(root)
        self.assertIn("test:FOREIGN", [i["id"] for i in rewritten["instruments"]])
        self.assertIn(("plaid", "FOREIGN"),
                      [(a["broker"], a["value"]) for a in rewritten["aliases"]])
        self.assertIn("captures/99-foreign.json", [p["file"] for p in rewritten_provenance])
        self.assertEqual(foreign_capture.read_bytes(), b'{"authored":"by another session"}\n')
        # The version marker records a human review this script did not perform.
        self.assertEqual(rewritten["version"], "test-reviewed-marker-v9")

    def test_unreadable_registry_is_never_treated_as_absent(self):
        # Rewriting from scratch here is the clobber: a corrupt file would be silently
        # replaced by a fresh one, which is indistinguishable from a legitimate first run.
        root = self.workspace()
        (root / "reference.json").write_bytes(b"{ not json")
        before = self.digests(root)
        result = self.regenerate(root, expected_exit=1)
        self.assertIn("FAIL preparing examples", result.stderr)
        self.assertEqual(self.digests(root), before)
        logged = (root / "prepare-errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual([json.loads(line)["status"] for line in logged], ["FAIL"])

    def test_regeneration_refuses_to_introduce_identities_into_a_reviewed_registry(self):
        # Adding an identity changes how captures resolve. Dropping an authored alias makes
        # the script want to re-add it; it must refuse and write nothing rather than decide.
        root = self.workspace()
        reference, _ = self.registry(root)
        # Must be an alias this script authors: removing one it merely preserves is a
        # legitimate reviewer edit and correctly changes nothing.
        dropped = next(a for a in reference["aliases"]
                       if (a["broker"], a["scheme"], a["value"]) == ("alpaca", "ticker", "AAPL"))
        reference["aliases"].remove(dropped)
        (root / "reference.json").write_text(json.dumps(reference, indent=2) + "\n",
                                             encoding="utf-8")
        before = self.digests(root)
        result = self.regenerate(root, expected_exit=1)
        self.assertIn("would introduce", result.stderr)
        self.assertIn(dropped["value"], result.stderr)
        self.assertEqual(self.digests(root), before)

    def test_capture_02_still_fails_closed_on_an_unregistered_figi_after_regeneration(self):
        # The concrete regression this file exists for. sources/plaid-docs-security.json
        # carries a FIGI that reference.json deliberately does not register, which is what
        # makes capture 02 demonstrate fail-closed identity resolution. Authoring that one
        # alias makes capture 02 resolve and be ACCEPTED instead, silently deleting the
        # demonstration. Measured 2026-09-13: it also breaks test_26.
        root = self.workspace()
        self.regenerate(root)
        proof = subprocess.run(
            [sys.executable, "-B", "-c",
             "from pathlib import Path\n"
             "from broker_engine import Engine, Rejected, read_json\n"
             "engine = Engine(read_json(Path('reference.json').read_bytes()))\n"
             "try:\n"
             "    print('ACCEPTED', engine(Path('captures/02-plaid-upstream.json').read_bytes())['status'])\n"
             "except Rejected as rejected:\n"
             "    print('REJECTED', rejected.code)\n"],
            cwd=root, capture_output=True, text=True)
        self.assertEqual(proof.returncode, 0, proof.stderr)
        self.assertEqual(proof.stdout.strip(), "REJECTED UNRESOLVED_IDENTITY")
        figis = [a for a in self.registry(root)[0]["aliases"] if a["scheme"] == "figi"]
        self.assertEqual(figis, [], "a registered FIGI would unblock capture 02")


if __name__ == "__main__":
    unittest.main()
