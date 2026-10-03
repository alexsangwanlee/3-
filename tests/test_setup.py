"""Turnkey setup: .env loading and `python -m tradebot check`."""
import os

from tradebot import config
from tradebot.__main__ import diagnose

SECRET = "s3cr3t-key-never-printed-0123456789"


def test_env_file_is_loaded_but_never_overrides_real_environment(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('# comment\nUPBIT_ACCESS_KEY="abc"\nUPBIT_SECRET_KEY=from-file\n\nTELEGRAM_CHAT_ID=\n')
    monkeypatch.delenv("UPBIT_ACCESS_KEY", raising=False)
    monkeypatch.setenv("UPBIT_SECRET_KEY", "from-env")
    config.load_env(p)
    assert os.environ["UPBIT_ACCESS_KEY"] == "abc"
    assert os.environ["UPBIT_SECRET_KEY"] == "from-env"
    monkeypatch.delenv("UPBIT_ACCESS_KEY")


class FakeClient:
    def __init__(self, error=None):
        self.error = error

    def tickers(self, markets):
        return {m: 100.0 for m in markets}

    def accounts(self):
        if self.error:
            raise RuntimeError(self.error)
        return [{"currency": "KRW", "balance": "1500000"}]


def test_paper_mode_needs_no_keys():
    ok, problems = diagnose(config.Config(), FakeClient(), env={})
    assert problems == []
    assert any("paper" in line for line in ok)


def test_live_mode_without_keys_is_refused():
    _, problems = diagnose(config.Config(mode="live"), FakeClient(), env={})
    assert any("UPBIT_ACCESS_KEY" in p for p in problems)


def test_unregistered_ip_gets_actionable_advice_and_never_leaks_the_key():
    env = {"UPBIT_ACCESS_KEY": SECRET, "UPBIT_SECRET_KEY": SECRET}
    ok, problems = diagnose(config.Config(mode="live"), FakeClient('401: {"error":{"name":"no_authorization_ip"}}'), env)
    assert any("IP" in p for p in problems)
    assert SECRET not in "\n".join(ok + problems)


def test_live_mode_with_working_keys_shows_balance():
    env = {"UPBIT_ACCESS_KEY": SECRET, "UPBIT_SECRET_KEY": SECRET}
    ok, problems = diagnose(config.Config(mode="live", budget_krw=1_000_000), FakeClient(), env)
    assert problems == []
    assert any("1,500,000" in line for line in ok)
    assert SECRET not in "\n".join(ok)


def test_sleeves_come_from_the_weekly_selection(tmp_path, monkeypatch):
    sel = tmp_path / "selected.json"
    sel.write_text('{"generated_at": "2026-10-01T00:00:00+00:00", "sleeves": {'
                   '"donchian": {"params": {"n": 96}, "tradable": true},'
                   '"ema_cross": {"params": {"fast": 12}, "tradable": false}}}')
    monkeypatch.setattr(config, "SELECTED", str(sel))
    sleeves = config.Config(strategies=["donchian", "ema_cross"]).sleeves()
    assert sleeves == [("donchian", {"n": 96}, True), ("ema_cross", {"fast": 12}, False)]


def test_live_mode_needs_a_budget():
    env = {"UPBIT_ACCESS_KEY": SECRET, "UPBIT_SECRET_KEY": SECRET}
    _, problems = diagnose(config.Config(mode="live", budget_krw=0), FakeClient(), env)
    assert any("budget_krw" in p for p in problems)


def test_old_config_keys_are_ignored_not_fatal(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('strategy = "auto"\nmode = "paper"\n[params]\n')
    assert config.load(p).mode == "paper"
