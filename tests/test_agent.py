import pytest
from agent import get_request_auth, validate_env_vars


def test_get_request_auth_default(monkeypatch):
    monkeypatch.delenv("INTERVALS_USE_BASIC_AUTH", raising=False)
    monkeypatch.setenv("INTERVALS_API_KEY", "dummy_key")
    headers, auth = get_request_auth()
    assert headers == {"Authorization": "Bearer dummy_key"}
    assert auth is None


def test_get_request_auth_basic(monkeypatch):
    monkeypatch.setenv("INTERVALS_USE_BASIC_AUTH", "1")
    monkeypatch.setenv("INTERVALS_API_KEY", "dummy_key")
    headers, auth = get_request_auth()
    assert headers is None
    assert auth == ("API_KEY", "dummy_key")


def test_validate_env_vars_missing(monkeypatch):
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        validate_env_vars()
