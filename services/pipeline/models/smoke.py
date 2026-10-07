from __future__ import annotations

from .client import Client, ModelError
from .config import Profile


def smoke(profile: Profile, client: Client) -> list[str]:
    """One real chat call and one real embedding call. Raises on any failure."""
    reply = client.chat([{"role": "user", "content": "Reply with the single word: pong"}])
    if not reply.strip():
        raise ModelError(f"{profile.llm_model} returned an empty reply "
                         "(a reasoning model may have used its whole token budget)")
    vector = client.embed(["hello world"])[0]
    return [
        f"profile:   {profile.name}",
        f"llm:       {profile.llm_model} replied {reply.strip()[:40]!r}",
        f"embedding: {profile.embedding_model} returned {len(vector)} dimensions",
    ]
