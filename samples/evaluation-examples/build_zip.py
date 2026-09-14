"""Build a deterministic ZIP containing only explicitly allowlisted portfolio files."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "portfolio-examples.zip"
ALLOWLIST = (
    "README.md",
    "CHANGELOG.md",
    "build_zip.py",
    "verify.py",
    "retrieval_eval/scorer.py",
    "retrieval_eval/historical_scores.json",
    "retrieval_eval/synthetic_example.json",
    "brokerage_normalization/normalize.py",
    "brokerage_normalization/fixtures/broker_alpha.json",
    "brokerage_normalization/fixtures/broker_beta.json",
    "tests/test_examples.py",
)


def build() -> None:
    missing = [relative for relative in ALLOWLIST if not (ROOT / relative).is_file()]
    if missing:
        raise FileNotFoundError(f"allowlisted files missing: {', '.join(missing)}")
    with zipfile.ZipFile(
        OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for relative in ALLOWLIST:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT / relative).read_bytes(), compresslevel=9)


def main() -> int:
    try:
        build()
        print(f"OK: wrote {OUTPUT.name} with {len(ALLOWLIST)} allowlisted files")
        return 0
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
