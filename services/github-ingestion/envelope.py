"""Wrap a raw GitHub webhook body as EMBER-39 intake.

Stdlib only. The envelope is {"type": "github", "body": <parsed object>}.
No cleaning, sorting, or embeddings.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys

LOG = logging.getLogger("github-ingestion")

# Push deliveries can be large. Stay under GitHub's 25 MiB webhook cap.
MAX_BODY_BYTES = 8 * 1024 * 1024

_hmac_skip_warned = False


class WebhookError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def note_verification(secret: str) -> None:
    """Warn once when HMAC is skipped because no secret is configured."""
    global _hmac_skip_warned
    if secret or _hmac_skip_warned:
        return
    LOG.warning("GITHUB_WEBHOOK_SECRET is unset; HMAC verification skipped")
    _hmac_skip_warned = True


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Check X-Hub-Signature-256 (sha256=<hex HMAC-SHA256 of the raw body>)."""
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = prefix + digest
    if len(signature_header) != len(expected):
        return False
    return hmac.compare_digest(expected, signature_header)


def wrap_github(payload: dict) -> dict:
    return {"type": "github", "body": payload}


def delivery_key(payload: dict) -> dict[str, int | str | None]:
    """Installation id and repository full_name, when the payload has them.

    Later routing can use this. It is not a filter and it is not added to
    the emitted envelope. Missing fields stay None; the delivery is still intake.
    """
    installation = payload.get("installation")
    repository = payload.get("repository")
    installation_id = None
    full_name = None
    if isinstance(installation, dict):
        candidate = installation.get("id")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            installation_id = candidate
    if isinstance(repository, dict):
        candidate_name = repository.get("full_name")
        if isinstance(candidate_name, str) and candidate_name:
            full_name = candidate_name
    return {"installation_id": installation_id, "full_name": full_name}


def ingest(raw_body: bytes, signature_header: str | None, secret: str) -> dict:
    """Verify the optional HMAC and return the {type, body} envelope."""
    if secret:
        if not signature_header or not verify_signature(secret, raw_body, signature_header):
            raise WebhookError(401, "invalid signature")
    else:
        note_verification(secret)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError(400, "invalid json") from exc
    if not isinstance(payload, dict):
        raise WebhookError(400, "body must be a JSON object")
    return wrap_github(payload)


def emit(envelope: dict) -> None:
    """Write one envelope as a single JSON line. No queue in this scaffold."""
    sys.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
    sys.stdout.flush()
