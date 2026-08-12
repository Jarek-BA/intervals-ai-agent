import os
from agent import get_request_auth

def test_get_request_auth_uses_bearer_by_default(monkeypatch):
    monkeypatch.setenv("INTERVALS_API_KEY", "testkey")
    monkeypatch.delenv("INTERVALS_USE_BASIC_AUTH", raising=False)
    headers, auth = get_request_auth()
    assert headers is not None
    assert headers.get("Authorization") == "Bearer testkey"
    assert auth is None

def test_get_request_auth_basic_fallback(monkeypatch):
    monkeypatch.setenv("INTERVALS_API_KEY", "testkey")
    monkeypatch.setenv("INTERVALS_USE_BASIC_AUTH", "1")
    headers, auth = get_request_auth()
    assert headers is None
    assert auth is not None
