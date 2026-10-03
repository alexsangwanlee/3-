"""Turnkey setup: .env loading and `python -m tradebot check`."""
import json
import os

import requests

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
    def __init__(self, error=None, krw=1_500_000, can_order=True, online=True):
        self.error, self.krw, self.can_order, self.online = error, krw, can_order, online

    def markets(self):
        if not self.online:
            raise requests.ConnectionError("Max retries exceeded")
        return {"KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE"}

    def accounts(self):
        if self.error:
            raise RuntimeError(self.error)
        return [{"currency": "KRW", "balance": str(self.krw)}]

    def order(self, uuid=None, identifier=None):
        raise RuntimeError('Upbit GET /order -> 404: {"error":{"name":"order_not_found"}}')

    def buy_market(self, market, krw, identifier=None, path="/orders"):
        assert path == "/orders/test", "diagnose must never place a real order"
        if not self.can_order:
            raise RuntimeError('Upbit POST /orders/test -> 401: {"error":{"name":"out_of_scope"}}')
        return {"uuid": "test"}


LIVE_KEYS = {"UPBIT_ACCESS_KEY": SECRET, "UPBIT_SECRET_KEY": SECRET}


def test_paper_mode_needs_no_keys():
    ok, problems = diagnose(config.Config(), FakeClient(), env={})
    assert problems == []
    assert any("모의매매" in line for line in ok)


def test_live_mode_without_keys_is_refused():
    _, problems = diagnose(config.Config(mode="live"), FakeClient(), env={})
    assert any("API 키" in p for p in problems)


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
    sleeves = config.Config(strategies=["donchian", "ema_cross", "vbo"]).sleeves()
    assert sleeves == [("donchian", {"n": 96}, True), ("ema_cross", {"fast": 12}, False)]  # vbo: not optimised yet


def test_live_mode_needs_a_budget():
    env = {"UPBIT_ACCESS_KEY": SECRET, "UPBIT_SECRET_KEY": SECRET}
    _, problems = diagnose(config.Config(mode="live", budget_krw=0), FakeClient(), env)
    assert any("budget_krw" in p for p in problems)


def test_old_config_keys_are_ignored_not_fatal(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('strategy = "auto"\nmode = "paper"\n[params]\n')
    assert config.load(p).mode == "paper"


def test_a_restart_after_buying_is_never_refused_for_low_krw(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "live_donchian.json").write_text(json.dumps({"funded": 1_000_000, "cash": 200_000}))
    ok, problems = diagnose(config.Config(mode="live", budget_krw=1_000_000), FakeClient(krw=200_000), LIVE_KEYS)
    assert problems == []  # the other 800k sits in coins that need their stops


def test_a_first_live_start_needs_the_budget_on_the_account(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, problems = diagnose(config.Config(mode="live", budget_krw=1_000_000), FakeClient(krw=200_000), LIVE_KEYS)
    assert any("budget_krw" in p for p in problems)


def test_a_key_without_order_permission_is_caught_before_the_first_trade(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, problems = diagnose(config.Config(mode="live", budget_krw=1_000_000), FakeClient(can_order=False), LIVE_KEYS)
    assert any("주문하기" in p for p in problems)


def test_a_mistyped_market_is_named_and_no_internet_is_not_called_a_typo():
    _, problems = diagnose(config.Config(markets=["KRW-BTCC", "KRW-ETH"]), FakeClient(), env={})
    assert any("KRW-BTCC" in p and "KRW-ETH" not in p for p in problems)
    _, problems = diagnose(config.Config(), FakeClient(online=False), env={})
    assert any("인터넷" in p for p in problems)


def test_env_inline_comments_and_bom_are_not_part_of_the_value(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("\ufeffTELEGRAM_BOT_TOKEN=123:abc   # 선택: 텔레그램\nexport TELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config.load_env(p)
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "123:abc" and os.environ["TELEGRAM_CHAT_ID"] == "42"


def test_the_bot_never_mistakes_itself_for_a_second_copy(tmp_path, monkeypatch):
    from tradebot import live
    monkeypatch.setattr(live, "PID_FILE", tmp_path / "bot.pid")
    live.PID_FILE.write_text(str(os.getpid()))  # Docker restart: the new process is PID 1 again
    assert live.running_pid() is None


def test_a_strategy_removed_while_holding_coins_keeps_managing_them(tmp_path, monkeypatch):
    from tradebot.__main__ import _bots
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "selected.json").write_text(json.dumps({"generated_at": "2026-10-01T00:00:00+00:00",
                                                                  "sleeves": {"donchian": {"params": {}, "tradable": True}}}))
    held = {"qty": 1.0, "entry": 100.0, "stop": 90.0, "entry_time": "2026-10-01T00:00:00+00:00"}
    (tmp_path / "state" / "paper_ema_cross.json").write_text(json.dumps(
        {"cash": 0.0, "funded": 500_000, "positions": {"KRW-BTC": held}, "last_entry_day": {}, "last_enter_bar": {},
         "guard": None, "paper_holdings": {"KRW-BTC": 1.0}}))
    _, bots = _bots(config.Config(strategies=["donchian"]), False, FakeClient(), None, 1_000_000)
    assert [(b.cfg.strategy, b.cfg.allow_entries) for b in bots] == [("donchian", True), ("ema_cross", False)]
    assert bots[1].state["funded"] == 500_000  # its money is not counted twice


def test_resume_clears_the_kill_switch_only(tmp_path, monkeypatch, capsys):
    from tradebot.__main__ import cmd_resume
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    guard = {"day": "2026-10-01", "day_start": 650_000, "peak": 1_000_000, "locked": False, "halted": True}
    (tmp_path / "state" / "live_donchian.json").write_text(json.dumps({"cash": 650_000, "guard": guard}))
    cmd_resume(config.Config(mode="live"), None)
    st = json.loads((tmp_path / "state" / "live_donchian.json").read_text())
    assert st["guard"]["halted"] is False and st["cash"] == 650_000
