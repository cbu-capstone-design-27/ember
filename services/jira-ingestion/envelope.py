"""Wrap a raw Jira webhook body as EMBER-39 intake.

Stdlib only. The envelope is {"type": "jira", "body": <parsed object>}.
No cleaning, sorting, or embeddings.

Admin webhooks (Jira Administration, or POST /rest/webhooks/1.0/webhook)
sign the raw body when a secret is set. The header is X-Hub-Signature
with value sha256=<hex HMAC-SHA256>. See Atlassian "Secure admin webhooks".
This module does not treat a query-string secret as authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys

LOG = logging.getLogger("jira-ingestion")

# Stay under a size a local stub can log. Jira retries oversized failures.
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
    LOG.warning("JIRA_WEBHOOK_SECRET is unset; HMAC verification skipped")
    _hmac_skip_warned = True


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Check X-Hub-Signature (sha256=<hex HMAC-SHA256 of the raw body>).

    Only the sha256 method from Atlassian's current admin-webhook docs is
    accepted. A different method prefix does not match.
    """
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = prefix + digest
    if len(signature_header) != len(expected):
        return False
    return hmac.compare_digest(expected, signature_header)


def wrap_jira(payload: dict) -> dict:
    return {"type": "jira", "body": payload}


def delivery_key(payload: dict) -> dict[str, str | None]:
    """cloudId, project key, and issue key, when the payload has them.

    Later routing can use this. It is not a filter and it is not added to
    the emitted envelope. Missing fields stay None; the delivery is still intake.
    Admin webhook bodies often omit cloudId. Project key is read from
    issue.fields.project.key, then from a top-level project object.
    """
    cloud_id = payload.get("cloudId")
    if not isinstance(cloud_id, str) or not cloud_id:
        cloud_id = None

    issue = payload.get("issue")
    issue_key = None
    project: dict = {}
    if isinstance(issue, dict):
        candidate = issue.get("key")
        if isinstance(candidate, str) and candidate:
            issue_key = candidate
        fields = issue.get("fields")
        if isinstance(fields, dict) and isinstance(fields.get("project"), dict):
            project = fields["project"]
    if not project and isinstance(payload.get("project"), dict):
        project = payload["project"]
    project_key = project.get("key")
    if not isinstance(project_key, str) or not project_key:
        project_key = None

    return {"cloud_id": cloud_id, "project_key": project_key, "issue_key": issue_key}


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
    return wrap_jira(payload)


def emit(envelope: dict) -> None:
    """Write one envelope as a single JSON line. No queue in this scaffold."""
    sys.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
    sys.stdout.flush()
