"""Wrap a raw GitHub object as EMBER-39 intake.

Stdlib only. The envelope is {"type": "github", "body": <parsed object>}.
No cleaning, sorting, or embeddings. Live webhooks are wrapped by services/intake.
"""

from __future__ import annotations


def wrap_github(payload: dict) -> dict:
    return {"type": "github", "body": payload}
