"""ceres-chat config: config.yaml + CERES_CHAT_<SECTION>__<KEY> env overrides (and CERES_CHAT_RUNTIME_DIR for runtime_dir)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENV_PREFIX = "CERES_CHAT_"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    ceres_url: str
    api_key: str = ""
    timeout_s: float = 300.0
    host: str = "127.0.0.1"
    port: int = 8310
    runtime_dir: Path = ROOT / "runtime"


def _apply_env(raw: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    for name, value in env.items():
        if name == ENV_PREFIX + "RUNTIME_DIR":   # the one top-level key: no section, so no double underscore
            raw["runtime_dir"] = value
            continue
        if not name.startswith(ENV_PREFIX) or "__" not in name:
            continue
        section, key = name[len(ENV_PREFIX):].lower().split("__", 1)
        raw.setdefault(section, {})[key] = value
    return raw


def _read_key(ceres: dict[str, Any], env: Mapping[str, str], base: Path) -> str:
    if ceres.get("api_key_file"):
        path = Path(ceres["api_key_file"])
        path = path if path.is_absolute() else base / path
        if not path.is_file():
            raise ConfigError(f"ceres.api_key_file does not exist: {path}")
        return path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    name = ceres.get("api_key_env") or "CERES_API_KEY"
    return env.get(name, "").strip()


def load(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = path or Path(env.get("CERES_CHAT_CONFIG", ROOT / "config.yaml"))
    if not path.is_file():
        raise ConfigError(f"no config file at {path}: copy config.example.yaml to config.yaml")
    raw = _apply_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, env)
    ceres, server = raw.get("ceres") or {}, raw.get("server") or {}
    if not ceres.get("url"):
        raise ConfigError("ceres.url is not set")
    runtime = Path(raw.get("runtime_dir") or "runtime")
    return Config(
        ceres_url=str(ceres["url"]).rstrip("/"),
        api_key=_read_key(ceres, env, path.parent),
        timeout_s=float(ceres.get("timeout_s", 300)),
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8310)),
        runtime_dir=runtime if runtime.is_absolute() else path.parent / runtime,
    )
