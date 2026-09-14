"""DEV ONLY: propose a synthetic field mapping; never load from the runtime.

No records, credentials, external endpoints, generated code, or promotion path.
Run: python -B dev_mapping.py --output examples/mapping-proposal.json
Review the proposal, then separately author/test/freeze a deterministic adapter.
Ollama API: https://docs.ollama.com/api/chat
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import urllib.request


MODEL = "qwen3:14b"
CHAT_URL = "http://127.0.0.1:11434/api/chat"
TAGS_URL = "http://127.0.0.1:11434/api/tags"
MAX_BYTES = 65536
TIMEOUT_SECONDS = 120
SYNTHETIC_SCHEMA = {
    "units_held": "number",
    "unit_quote": "number",
    "security_code": "string",
    "quote_ccy": "string",
}
CANONICAL_SCHEMA = {
    "quantity": "number", "price": "number",
    "instrument_id": "string", "currency": "string",
}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def strict_json(raw):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique_pairs)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirects forbidden for local development requests")


def local_json(url, body=None):
    if url not in (CHAT_URL, TAGS_URL):
        raise ValueError("only fixed loopback Ollama endpoints are allowed")
    request = urllib.request.Request(
        url, data=None if body is None else canonical_json(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # Ignore proxy environment variables; never follow a redirect off loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("local model response exceeds size bound")
    return strict_json(raw)


def validate_mapping(value):
    if not isinstance(value, dict) or set(value) != {"mapping"}:
        raise ValueError("expected only a mapping object; extra instructions are forbidden")
    mapping = value["mapping"]
    if not isinstance(mapping, dict) or set(mapping) != set(SYNTHETIC_SCHEMA):
        raise ValueError("mapping must cover exactly the synthetic source field allowlist")
    if any(not isinstance(v, str) or v not in CANONICAL_SCHEMA for v in mapping.values()):
        raise ValueError("unknown canonical field")
    if set(mapping.values()) != set(CANONICAL_SCHEMA):
        raise ValueError("mapping must be one-to-one and cover the canonical allowlist")
    if any(SYNTHETIC_SCHEMA[k] != CANONICAL_SCHEMA[v] for k, v in mapping.items()):
        raise ValueError("incompatible schema types")
    return dict(mapping)


def log_event(output, severity, detail):
    event = {"utc": datetime.now(timezone.utc).isoformat(), "severity": severity,
             "stage": "dev_mapping", "detail": detail}
    line = canonical_json(event)
    print(line, file=sys.stderr)
    try:
        with Path(str(output) + ".events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError as exc:
        print("FAIL dev_mapping log write: " + type(exc).__name__, file=sys.stderr)


def create_proposal(output):
    output = Path(output)
    try:
        if output.exists():
            raise FileExistsError("proposal output already exists")
        if not output.parent.is_dir():
            raise ValueError("output parent must already exist")
        model_digest = None
        try:
            tags = local_json(TAGS_URL)
            for item in tags["models"]:
                if item.get("name") == MODEL:
                    candidate = item.get("digest")
                    if isinstance(candidate, str) and len(candidate) <= 128:
                        model_digest = candidate
                    break
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log_event(output, "WARN", "model digest unavailable: " + type(exc).__name__)
        request = {
            "model": MODEL, "stream": False, "think": False, "format": "json",
            "options": {"temperature": 0, "seed": 0, "num_predict": 180, "num_ctx": 2048},
            "messages": [{"role": "user", "content":
                "Development-only synthetic schema exercise. Return only JSON with one key "
                "mapping whose object maps every source field name to exactly one canonical "
                "field name of matching type, using each canonical name once. No code, values, "
                "explanation, or other keys. Source schema: " + canonical_json(SYNTHETIC_SCHEMA)
                + "; canonical schema: " + canonical_json(CANONICAL_SCHEMA)}],
        }
        response = local_json(CHAT_URL, request)
        if response.get("done") is not True or response.get("done_reason") == "length":
            raise ValueError("model generation incomplete")
        if response.get("model") != MODEL:
            raise ValueError("unexpected response model")
        message = response["message"]
        if message.get("tool_calls"):
            raise ValueError("model tool calls forbidden")
        mapping = validate_mapping(strict_json(message["content"]))
        proposal = {
            "artifact_type": "dev_only_synthetic_mapping_proposal", "status": "review_required",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "synthetic_only": True, "model": MODEL, "model_digest": model_digest,
            "input_sha256": digest({"source": SYNTHETIC_SCHEMA, "canonical": CANONICAL_SCHEMA}),
            "request_sha256": digest(request), "response_sha256": digest(response),
            "source_schema": SYNTHETIC_SCHEMA, "canonical_schema": CANONICAL_SCHEMA,
            "mapping": mapping,
            "next_step": "Human reviews, then separately authors and tests a deterministic transformer and fixture. No runtime loading or automatic promotion.",
            "generation": {key: response.get(key) for key in
                           ("done_reason", "total_duration", "prompt_eval_count", "eval_count")},
        }
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(proposal, indent=2, allow_nan=False) + "\n")
        log_event(output, "OK", "synthetic proposal created; review_required; not promoted")
        return proposal
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never echo arbitrary model text or local payloads into logs.
        log_event(output, "FAIL", "proposal creation failed: " + type(exc).__name__)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="new dev-only proposal JSON; existing files are refused")
    args = parser.parse_args(argv)
    try:
        create_proposal(args.output)
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    print("Proposal written for human review only: " + str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
