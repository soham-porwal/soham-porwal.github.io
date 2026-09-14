"""Ingest local capture files, or inspect verified durable state. No broker/model network access.

`wrap` and `ingest-raw` accept one raw broker export and supply the capture envelope from
explicit flags. They add no validation and remove none: a wrapped export is judged by the same
engine, against the same contract, as a hand-authored capture.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

from broker_engine import Engine, MAX_BYTES, read_json, stamp
from durable_store import Store
from raw_intake import IntakeError, wrap_file

HERE = Path(__file__).resolve().parent


def policy_hash(reference_bytes):
    digest = hashlib.sha256(reference_bytes)
    for name in ("broker_engine.py", "broker_adapters.py", "durable_store.py", "broker_cli.py",
                 "raw_intake.py"):
        digest.update(name.encode())
        digest.update((HERE / name).read_bytes())
    return digest.hexdigest()


def safe_destinations(database, inputs):
    destinations = [database, database.with_suffix(database.suffix + ".errors.jsonl"),
                    Path(str(database) + "-journal"), Path(str(database) + "-wal"), Path(str(database) + "-shm")]
    for dest in destinations:
        for source in inputs:
            if dest.resolve() == source.resolve() or (dest.exists() and source.exists() and dest.samefile(source)):
                raise ValueError("database/log destination aliases an input")


def raw_capture(args, parser):
    """Wrap one raw export and return its bytes, writing the capture only when asked to.

    The written capture is the artifact a reviewer inspects, so it is never overwritten: a
    second run against a changed export must not silently replace the evidence for the first.
    """
    source = args.inputs[0]
    capture, body = wrap_file(source, args.broker, args.account, args.as_of,
                              args.currency, args.total_scope, MAX_BYTES)
    if args.out is not None:
        if args.out.exists():
            raise ValueError(f"{args.out} exists; refusing to overwrite an existing capture")
        if args.out.resolve() == source.resolve():
            raise ValueError("--out aliases the raw export")
        args.out.write_bytes(body)
    return capture, body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ingest", "report", "verify", "wrap", "ingest-raw"))
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--reference", type=Path, default=HERE / "reference.json")
    parser.add_argument("--broker", help="wrap/ingest-raw: exact adapter profile; never guessed from content")
    parser.add_argument("--account", help="wrap/ingest-raw: checked against the export's own account field")
    parser.add_argument("--as-of", dest="as_of",
                        help="wrap/ingest-raw: when the holdings were observed, YYYY-MM-DDTHH:MM:SSZ. "
                             "Required: download time is not observation time")
    parser.add_argument("--currency", default="USD", help="wrap/ingest-raw: quote currency (default USD)")
    parser.add_argument("--total-scope", dest="total_scope", default="holdings",
                        help="wrap/ingest-raw: holdings or holdings_and_cash (default holdings)")
    parser.add_argument("--out", type=Path, help="wrap: where to write the capture; ingest-raw: optional copy")
    args = parser.parse_args(argv)
    raw_commands = ("wrap", "ingest-raw")
    if args.command in ("ingest", *raw_commands) and not args.inputs:
        parser.error(f"{args.command} requires at least one input")
    if args.command in ("report", "verify") and args.inputs:
        parser.error("only ingest, wrap and ingest-raw accept inputs")
    if args.command != "wrap" and args.db is None:
        parser.error(f"{args.command} requires --db")
    if args.command == "wrap" and args.out is None:
        parser.error("wrap requires --out")
    if args.command in raw_commands:
        # Usage is settled before anything is opened or created, so a missing flag cannot
        # leave an empty database behind - the same rule report and verify already follow.
        for flag in ("broker", "account", "as_of"):
            if not getattr(args, flag):
                parser.error(f"{args.command} requires --{flag.replace('_', '-')}")
        if len(args.inputs) != 1:
            parser.error(f"{args.command} takes exactly one raw export")
    try:
        if args.command == "wrap":
            capture, _ = raw_capture(args, parser)
            print(json.dumps({"status": "WRAPPED", "capture": str(args.out),
                              "broker": capture["broker"], "account": capture["account"],
                              "snapshot_id": capture["snapshot_id"], "as_of": capture["as_of"],
                              "next": "ingest this capture; the engine, not this command, decides whether it is valid"},
                             sort_keys=True, indent=2))
            return 0
        safe_destinations(args.db, [*args.inputs, args.reference])
        if args.command not in ("ingest", "ingest-raw") and not args.db.exists():
            raise ValueError("database does not exist; report/verify never create state")
        reference_bytes = args.reference.read_bytes()
        engine = Engine(read_json(reference_bytes))
        events = []
        with Store(args.db, engine, policy_hash(reference_bytes)) as store:
            if args.command == "ingest-raw":
                # The wrapped bytes are what the store hashes and judges, so the recorded
                # source names the raw export the reviewer would go back to.
                _, body = raw_capture(args, parser)
                events.append(store.ingest(body, f"{args.inputs[0].as_posix()} (wrapped)"))
            elif args.command == "ingest":
                for path in args.inputs:
                    # Read bounded payload for parsing; oversize is operational and logs no
                    # misleading full-file hash. Inputs must be immutable regular local files.
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("capture exceeds size limit; no truncated source hash recorded")
                    events.append(store.ingest(raw, path.as_posix()))
            proof = store.verify()
            if args.command == "verify":
                result = proof
            else:
                result = store.report()
                now = datetime.now(timezone.utc)
                result["evaluated_at"] = now.isoformat()
                result["freshness_policy"] = "snapshot older than 24 elapsed hours is stale; not an exchange calendar"
                result["account_freshness"] = [{"broker": a["broker"], "account": a["account"],
                    "state": "FUTURE" if stamp(a["as_of"]) > now else
                             "STALE" if now - stamp(a["as_of"]) > timedelta(days=1) else "RECENT"}
                    for a in result["current"]]
                result["run_events"] = events
                result["verify"] = proof
            print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
            return 2 if any(e["status"] == "QUARANTINED" for e in events) else 0
    except Exception as exc:
        # Store failures have durable type-only diagnostics. File/config/alias failures
        # occur before trustworthy destinations exist; make them explicit on stderr.
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
