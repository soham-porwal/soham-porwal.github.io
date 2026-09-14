"""Fail loudly when package behavior, privacy checks, or ZIP contents drift."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import build_zip


ROOT = Path(__file__).resolve().parent
TEXT_SUFFIXES = {".md", ".py", ".json"}
FORBIDDEN_PATTERNS = {
    "private Windows root": re.compile(
        rb"\b[A-Za-z]:[\\/](?:Users|soham|home)[\\/]", re.IGNORECASE
    ),
    "home directory path": re.compile(
        rb"(?:/Use" rb"rs/|/ho" rb"me/)", re.IGNORECASE
    ),
    "email address": re.compile(rb"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    "credential-shaped assignment": re.compile(
        rb"(?:api[_-]?key|secret|password|token)\s*[:=]\s*[\"'][^\"']+[\"']",
        re.IGNORECASE,
    ),
    "private result fields": re.compile(
        rb"(?:query_" rb"text|hits_" rb"json|note_" rb"path|document_" rb"path)",
        re.IGNORECASE,
    ),
}


def run_json(relative: str, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(ROOT / relative), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{relative} failed: {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{relative} did not emit JSON") from exc


def check_privacy() -> None:
    for relative in build_zip.ALLOWLIST:
        path = ROOT / relative
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        data = path.read_bytes()
        for label, pattern in FORBIDDEN_PATTERNS.items():
            if pattern.search(data):
                raise RuntimeError(f"privacy scan found {label} in {relative}")


def run_tests() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"unit tests failed:\n{completed.stdout}{completed.stderr}")


def check_zip() -> None:
    build_zip.build()
    with zipfile.ZipFile(build_zip.OUTPUT, "r") as archive:
        names = archive.namelist()
        if names != list(build_zip.ALLOWLIST):
            raise RuntimeError("ZIP members differ from the ordered allowlist")
        if any(info.date_time != (1980, 1, 1, 0, 0, 0) for info in archive.infolist()):
            raise RuntimeError("ZIP contains a non-deterministic timestamp")


def main() -> int:
    try:
        historical = run_json("retrieval_eval/scorer.py")
        synthetic = run_json("retrieval_eval/scorer.py", "--synthetic")
        brokerage = run_json("brokerage_normalization/normalize.py")
        if historical["n_paired"] != 40:
            raise RuntimeError("historical pair-count invariant failed")
        if synthetic["scope"] != "synthetic_demonstration":
            raise RuntimeError("synthetic scope label missing")
        if brokerage["scope"] != "invented_illustrative_reimplementation":
            raise RuntimeError("brokerage provenance label missing")
        run_tests()
        check_privacy()
        check_zip()
        print("PASS: historical metrics re-derived from 40 anonymous numeric pairs")
        print("PASS: invented brokerage fixtures preserve null, zero, and Decimal semantics")
        print("PASS: unit tests, privacy scan, and deterministic ZIP allowlist")
        return 0
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
