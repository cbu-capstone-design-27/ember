"""Model profiles: which LLM and embedding model the pipeline calls and where the keys live."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "config" / "models.toml"
_REQUIRED = ("llm_base_url", "llm_model", "llm_key_env", "embedding_base_url", "embedding_model",
             "embedding_dimensions", "embedding_key_env")
_OPTIONAL = ("send_dimensions", "query_template", "embedding_revision", "identity_model")


class ConfigError(RuntimeError):
    pass


def _key(name: str, env) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set; put it in .env (see .env.example), never in the repository")
    return value


@dataclass(frozen=True)
class Profile:
    name: str
    llm_base_url: str
    llm_model: str
    llm_key_env: str
    embedding_base_url: str
    embedding_model: str
    embedding_dimensions: int
    embedding_key_env: str
    send_dimensions: bool = True
    query_template: str = "{query}"
    embedding_revision: str = "provider-managed"
    identity_model: str = ""

    def llm_key(self, env=os.environ) -> str:
        return _key(self.llm_key_env, env)

    def embedding_key(self, env=os.environ) -> str:
        return _key(self.embedding_key_env, env)


def load_profile(name: str | None = None, path: Path = DEFAULT_CONFIG, env=os.environ) -> Profile:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    profiles = data.get("profiles", {})
    name = name or env.get("MODEL_PROFILE") or data.get("default_profile")
    if name not in profiles:
        raise ConfigError(f"unknown model profile {name!r}; available: {', '.join(sorted(profiles))}")
    raw = profiles[name]
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise ConfigError(f"profile {name!r} is missing: {', '.join(missing)}")
    values = {k: raw[k] for k in _REQUIRED + _OPTIONAL if k in raw}
    # Compose passes unset variables as empty strings; an empty or blank override must not win.
    values["llm_base_url"] = env.get("LLM_BASE_URL", "").strip() or values["llm_base_url"]
    values["embedding_base_url"] = env.get("EMBEDDING_BASE_URL", "").strip() or values["embedding_base_url"]
    return Profile(name=name, **values)


def load_dotenv(path: Path, env: MutableMapping[str, str] | None = None) -> None:
    """Read KEY=VALUE lines into env. Values already in the environment win."""
    env = os.environ if env is None else env
    path = Path(path)
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and name not in env:
            env[name] = value
