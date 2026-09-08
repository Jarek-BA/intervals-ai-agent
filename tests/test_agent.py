import datetime

import pytest
import agent
from agent import (
    build_wellness_context,
    get_request_auth,
    validate_env_vars,
)


def test_build_wellness_context_falls_back_per_metric():
    context = build_wellness_context(
        [
            {
                "id": "2026-09-07",
                "sleepSecs": 28800,
                "restingHR": 48,
                "steps": 12000,
            },
            {
                "id": "2026-09-08",
                "sleepSecs": 25200,
                "restingHR": None,
                "steps": None,
            },
        ],
        evaluation_date=datetime.date(2026, 9, 8),
    )

    assert context["data"]["sleepSecs"] == 25200
    assert context["data"]["restingHR"] == 48
    assert context["data"]["steps"] == 12000
    assert context["sources"]["sleepSecs"] == "2026-09-08"
    assert context["sources"]["restingHR"] == "2026-09-07"
    assert context["sources"]["steps"] == "2026-09-07"


def test_prompt_explains_fallback_metric_provenance(monkeypatch):
    captured = {}

    class FakeInteraction:
        output_text = "report"

    class FakeInteractions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeInteraction()

    class FakeClient:
        interactions = FakeInteractions()

    monkeypatch.setattr(agent.genai, "Client", lambda api_key: FakeClient())
    monkeypatch.setattr(agent, "GEMINI_API_KEY", "dummy-key")
    monkeypatch.setattr(
        agent.datetime, "date", type("FixedDate", (datetime.date,), {
            "today": classmethod(lambda cls: cls(2026, 9, 8)),
        })
    )

    result = agent.generate_ai_recommendation(
        [
            {"id": "2026-09-07", "restingHR": 48},
            {"id": "2026-09-08", "restingHR": None},
        ],
        [],
    )

    assert result == "report"
    prompt = captured["input"]
    assert "restingHR: 2026-09-07" in prompt
    assert "post-training" in prompt
    assert "data-based performance forecast" in prompt


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
