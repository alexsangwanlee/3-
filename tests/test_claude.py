"""Claude's weekly review: it explains and picks one action from a whitelist. It can make the bot safer on its own,
never riskier, and it never sees a secret."""
import json

import pytest

from tradebot import claude, config, live


class Reply:
    def __init__(self, review, status=200):
        self.review, self.status_code = review, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise claude.requests.HTTPError(f"{self.status_code} overloaded")

    def json(self):
        return {"content": [{"type": "text", "text": "ok"},
                            {"type": "tool_use", "name": "weekly_review", "input": self.review}],
                "usage": {"input_tokens": 3100, "output_tokens": 420, "cache_read_input_tokens": 0}}


def review(action, **kw):
    return {"summary": "이번 주는 정상 범위입니다.", "risks": [], "action": action, "reason": "근거", "user_checks": [], **kw}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-0123456789abcdefghij")
    monkeypatch.setenv("UPBIT_SECRET_KEY", "upbit-secret-never-sent-anywhere-123")
    monkeypatch.setattr(claude, "notify", lambda text: None)
    monkeypatch.setattr(claude, "market_context", lambda: {})
    return tmp_path


def answer(monkeypatch, rv, sent=None):
    def post(url, headers, json, timeout):
        if sent is not None:
            sent.append((headers, json))
        return Reply(rv) if isinstance(rv, dict) else rv
    monkeypatch.setattr(claude.requests, "post", post)


def test_without_an_api_key_nothing_is_called(home, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(claude.requests, "post", lambda *a, **k: pytest.fail("no key, no call"))
    assert claude.run(config.Config()) is None


def test_autopilot_pauses_new_buys_on_its_own(home, monkeypatch):
    answer(monkeypatch, review("pause_entries"))
    out = claude.run(config.Config(claude_autopilot=True))
    assert out["status"] == "applied" and live.entries_paused()
    assert json.loads(claude.REVIEW.read_text(encoding="utf-8"))["review"]["summary"].startswith("이번 주")


def test_without_autopilot_the_action_waits_for_approval(home, monkeypatch):
    answer(monkeypatch, review("pause_entries"))
    out = claude.run(config.Config())
    assert out["status"] == "waiting" and not live.entries_paused()
    assert claude.approve(config.Config()) == "applied" and live.entries_paused()


def test_resuming_buys_always_waits_for_the_user(home, monkeypatch):
    live.PAUSE_FILE.write_text("{}")
    answer(monkeypatch, review("resume_entries"))
    assert claude.run(config.Config(claude_autopilot=True))["status"] == "waiting"
    assert live.entries_paused()


def test_claude_cannot_invent_actions_or_strategies(home, monkeypatch):
    answer(monkeypatch, review("sell_everything_now"))
    assert claude.run(config.Config(claude_autopilot=True))["review"]["action"] == "keep"
    answer(monkeypatch, review("apply_recommendation"))  # but the self-review recommended nothing
    out = claude.run(config.Config(claude_autopilot=True))
    assert out["review"]["action"] == "keep" and out["status"] == "none"


def test_a_validated_recommendation_is_applied_by_autopilot(home, monkeypatch):
    rec = {"recommend": ["donchian"], "baseline": {"sharpe": 1.0, "recent": 0.01},
           "candidates": [{"strategies": ["donchian"], "sharpe": 1.3, "recent": 0.02}]}
    (home / "state" / "selected.json").write_text(json.dumps({"self_review": rec}))
    (home / "config.toml").write_text('strategies = ["donchian", "ema_cross"]\n')
    answer(monkeypatch, review("apply_recommendation"))
    assert claude.run(config.Config(claude_autopilot=True))["status"] == "applied"
    assert config.load(home / "config.toml").strategies == ["donchian"]


def test_no_secret_ever_reaches_claude(home, monkeypatch):
    sent = []
    answer(monkeypatch, review("keep"), sent)
    claude.run(config.Config())
    headers, body = sent[0]
    assert "upbit-secret-never-sent-anywhere-123" not in json.dumps(body, ensure_ascii=False)
    assert headers["x-api-key"].startswith("sk-ant-") and body["tool_choice"]["name"] == "weekly_review"


def test_an_api_failure_never_breaks_the_bot(home, monkeypatch):
    answer(monkeypatch, Reply({}, status=529))
    assert claude.run(config.Config(claude_autopilot=True)) is None
    assert not live.entries_paused()


def test_nothing_changed_means_no_new_call_and_no_tokens(home, monkeypatch):
    sent = []
    answer(monkeypatch, review("keep"), sent)
    first = claude.run(config.Config())
    again = claude.run(config.Config())
    assert len(sent) == 1 and again["skipped"] and again["review"] == first["review"]
    assert first["usage"]["input_tokens"] == 3100


def test_what_was_already_learned_is_sent_as_a_cacheable_digest_not_rediscovered(home, monkeypatch):
    sent = []
    (home / "results").mkdir()
    (home / "results" / "knowledge.json").write_text(json.dumps({"rejected": [{"idea": "trailing stop"}]}))
    answer(monkeypatch, review("keep"), sent)
    claude.run(config.Config())
    system = sent[0][1]["system"]
    assert "trailing stop" in system[-1]["text"] and system[-1]["cache_control"] == {"type": "ephemeral"}
