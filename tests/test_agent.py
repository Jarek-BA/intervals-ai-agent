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

    monkeypatch.setattr(
        agent.genai, "Client", lambda **kwargs: FakeClient()
    )
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


def test_gemini_client_uses_bounded_timeout_and_retries(monkeypatch):
    captured = {}

    class FakeInteraction:
        output_text = "report"

    class FakeInteractions:
        def create(self, **kwargs):
            return FakeInteraction()

    class FakeClient:
        interactions = FakeInteractions()

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(agent.genai, "Client", fake_client)
    monkeypatch.setattr(agent, "GEMINI_API_KEY", "dummy-key")

    assert agent.generate_ai_recommendation([], []) == "report"

    http_options = captured["http_options"]
    assert http_options.timeout == 120_000
    assert http_options.retry_options.attempts == 3
    assert http_options.retry_options.initial_delay == 2.0
    assert http_options.retry_options.max_delay == 15.0


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


def test_get_request_auth_missing_key(monkeypatch):
    monkeypatch.setenv("INTERVALS_USE_BASIC_AUTH", "1")
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    headers, auth = get_request_auth()
    assert headers is None
    assert auth is None


def test_validate_env_vars_missing(monkeypatch):
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    monkeypatch.delenv("INTERVALS_ATHLETE_ID", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        validate_env_vars()


def test_safe_get_returns_none_for_http_errors(monkeypatch):
    response = type("Response", (), {
        "raise_for_status": lambda self: (_ for _ in ()).throw(
            agent.requests.HTTPError("401 Client Error")
        ),
    })()
    monkeypatch.setattr(agent.requests, "get", lambda *args, **kwargs: response)

    assert agent.safe_get("https://example.test") is None


def test_get_intervals_data_fails_when_required_request_fails(monkeypatch):
    monkeypatch.setattr(agent, "safe_get", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="wellness data"):
        agent.get_intervals_data()


def test_get_intervals_data_fails_on_invalid_wellness_payload(monkeypatch):
    class FakeResponse:
        def json(self):
            return {"error": "unexpected payload"}

    monkeypatch.setattr(
        agent, "safe_get", lambda *args, **kwargs: FakeResponse()
    )

    with pytest.raises(RuntimeError, match="invalid wellness data"):
        agent.get_intervals_data()


def test_main_fails_when_ai_generation_fails(monkeypatch):
    monkeypatch.setattr(agent, "validate_env_vars", lambda: None)
    monkeypatch.setattr(agent, "get_intervals_data", lambda: ([], []))
    monkeypatch.setattr(
        agent,
        "generate_ai_recommendation",
        lambda *args: (_ for _ in ()).throw(RuntimeError("API unavailable")),
    )

    with pytest.raises(RuntimeError, match="AI recommendation generation failed"):
        agent.main()


def test_main_fails_when_email_is_not_sent(monkeypatch):
    monkeypatch.setattr(agent, "validate_env_vars", lambda: None)
    monkeypatch.setattr(agent, "get_intervals_data", lambda: ([], []))
    monkeypatch.setattr(
        agent, "generate_ai_recommendation", lambda *args: "report"
    )
    monkeypatch.setattr(agent, "send_email", lambda *args: False)

    with pytest.raises(RuntimeError, match="Report was not sent"):
        agent.main()


def test_main_fails_when_report_is_empty(monkeypatch):
    monkeypatch.setattr(agent, "validate_env_vars", lambda: None)
    monkeypatch.setattr(agent, "get_intervals_data", lambda: ([], []))
    monkeypatch.setattr(
        agent, "generate_ai_recommendation", lambda *args: "  "
    )

    with pytest.raises(RuntimeError, match="Empty report received"):
        agent.main()
