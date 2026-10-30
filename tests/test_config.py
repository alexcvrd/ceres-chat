"""Config loading. Failure modes: the key read from the wrong place, an env override ignored, a relative runtime dir resolved
against the working directory instead of the config file."""
from pathlib import Path

import pytest

from ceres_chat.config import ConfigError, load


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_key_from_env_or_file(tmp_path):
    """Bug: the key is not found where the config says, so every ask is rejected."""
    p = write(tmp_path, "ceres: {url: http://x:1, api_key_env: MY_KEY}\n")
    assert load(p, {"MY_KEY": " k1 "}).api_key == "k1"
    (tmp_path / "key").write_text("k2\n")
    p = write(tmp_path, "ceres: {url: http://x:1, api_key_file: key}\n")
    assert load(p, {}).api_key == "k2"


def test_missing_key_file_refused(tmp_path):
    """Bug: a typo in api_key_file silently starts without a key."""
    with pytest.raises(ConfigError):
        load(write(tmp_path, "ceres: {url: http://x:1, api_key_file: nope}\n"), {})


def test_env_override_and_runtime_dir(tmp_path):
    """Bug: CERES_CHAT_SERVER__PORT is ignored, or runtime/ lands in the shell's working directory."""
    cfg = load(write(tmp_path, "ceres: {url: http://x:1/}\nruntime_dir: rt\n"), {"CERES_CHAT_SERVER__PORT": "8399"})
    assert cfg.port == 8399 and cfg.ceres_url == "http://x:1" and cfg.runtime_dir == tmp_path / "rt"


def test_env_overrides_runtime_dir(tmp_path):
    """Bug: CERES_CHAT_RUNTIME_DIR is ignored (no double underscore, so it was skipped) though the README promises it."""
    p = write(tmp_path, "ceres: {url: http://x:1}\nruntime_dir: rt\n")
    assert load(p, {"CERES_CHAT_RUNTIME_DIR": "/var/lib/chat"}).runtime_dir == Path("/var/lib/chat")
    assert load(p, {"CERES_CHAT_RUNTIME_DIR": "other"}).runtime_dir == tmp_path / "other"
