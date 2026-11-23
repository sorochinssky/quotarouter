"""Key lookup order: `os.environ` → optional dotenv → host resolvers.

The order is a contract, not an implementation detail: an operator must be able to override a
resolver by exporting a variable, and a test must be able to override both.
"""

from __future__ import annotations

import pytest

from quotarouter import env_source


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    saved = env_source.registered_resolvers()
    env_source.clear_resolvers()
    for name in ("QUOTAROUTER_ENV_FILE", "QR_TEST_KEY", "QR_OTHER_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    env_source.set_resolvers(saved)


def test_environment_wins(monkeypatch, tmp_path):
    env_file = tmp_path / "vendor.env"
    env_file.write_text("QR_TEST_KEY=from-file\n")
    monkeypatch.setenv("QUOTAROUTER_ENV_FILE", str(env_file))
    monkeypatch.setenv("QR_TEST_KEY", "from-env")
    assert env_source.resolve("QR_TEST_KEY") == "from-env"


def test_dotenv_is_used_when_the_environment_is_silent(monkeypatch, tmp_path):
    env_file = tmp_path / "vendor.env"
    env_file.write_text('# comment\n\nQR_TEST_KEY="quoted-value"\nQR_OTHER_KEY=plain\n')
    monkeypatch.setenv("QUOTAROUTER_ENV_FILE", str(env_file))
    assert env_source.resolve("QR_TEST_KEY") == "quoted-value"
    assert env_source.resolve("QR_OTHER_KEY") == "plain"


def test_resolver_is_the_last_word(monkeypatch, tmp_path):
    env_source.register_resolver(lambda name: "from-resolver" if name == "QR_TEST_KEY" else "")
    # no dotenv configured: the resolver answers
    assert env_source.resolve("QR_TEST_KEY") == "from-resolver"
    # a dotenv file outranks a resolver — the file is an explicit operator decision
    env_file = tmp_path / "vendor.env"
    env_file.write_text("QR_TEST_KEY=from-file\n")
    monkeypatch.setenv("QUOTAROUTER_ENV_FILE", str(env_file))
    assert env_source.resolve("QR_TEST_KEY") == "from-file"


def test_a_broken_resolver_cannot_break_a_call():
    def boom(name: str) -> str:
        raise RuntimeError("resolver exploded")

    env_source.register_resolver(boom)
    assert env_source.resolve("QR_TEST_KEY") == ""


def test_unknown_name_resolves_to_an_empty_string():
    assert env_source.resolve("QR_DEFINITELY_NOT_SET") == ""


def test_a_foreign_host_finds_a_dotenv_in_the_working_directory(tmp_path, monkeypatch):
    """Without this, only Hermes could find keys: every other MCP client ran keyless."""
    from quotarouter import env_source

    (tmp_path / ".env").write_text("EXA_API_KEY=from-cwd\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QUOTAROUTER_ENV_FILE", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    env_source._dotenv_cache = None
    assert env_source.resolve("EXA_API_KEY") == "from-cwd"


def test_the_explicit_env_file_still_wins(tmp_path, monkeypatch):
    from quotarouter import env_source

    (tmp_path / ".env").write_text("EXA_API_KEY=from-cwd\n", encoding="utf-8")
    explicit = tmp_path / "chosen.env"
    explicit.write_text("EXA_API_KEY=explicit\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUOTAROUTER_ENV_FILE", str(explicit))
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    env_source._dotenv_cache = None
    assert env_source.resolve("EXA_API_KEY") == "explicit"
