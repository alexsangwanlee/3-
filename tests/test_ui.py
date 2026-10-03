"""Control panel: settings files are edited safely, and the local API refuses anything not from the page."""
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from tradebot import ui


def test_env_upsert_keeps_other_lines_and_cleans_pasted_keys():
    text = "# comment\nUPBIT_ACCESS_KEY=\nOTHER=1\n"
    out = ui.upsert_env(text, {"UPBIT_ACCESS_KEY": ' "abc123" ', "TELEGRAM_CHAT_ID": "42"})
    assert out == "# comment\nUPBIT_ACCESS_KEY=abc123\nOTHER=1\nTELEGRAM_CHAT_ID=42\n"


def test_toml_update_keeps_comments_and_tables():
    text = 'mode = "paper"  # 모의\nbudget_krw = 0\n\n[risk]\nmode = "not-top-level"\n'
    out = ui.set_toml_top(text, {"mode": "live", "budget_krw": 2000000, "strategies": ["donchian"]})
    assert out.startswith('mode = "live"  # 모의\nbudget_krw = 2000000\nstrategies = ["donchian"]\n')
    assert 'mode = "not-top-level"' in out  # tables untouched


@pytest.mark.parametrize("bad", [
    {"mode": "yolo"}, {"budget_krw": -1}, {"budget_krw": "3000"}, {"paper_krw": "0"},
    {"markets": ["BTC"]}, {"strategies": ["hold"]},
    {"UPBIT_ACCESS_KEY": "short"}, {"TELEGRAM_CHAT_ID": "abc"},
])
def test_settings_are_validated(bad):
    _, errors = ui.validate(bad)
    assert errors


def test_valid_settings_pass():
    clean, errors = ui.validate({"mode": "live", "budget_krw": "1,000,000", "markets": ["KRW-BTC"],
                                 "strategies": ["donchian", "ema_cross"], "UPBIT_ACCESS_KEY": "A" * 40})
    assert errors == [] and clean["budget_krw"] == 1_000_000


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    srv = ui.make_server(0, token="t0k")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def call(url, path, token=None, host=None, body=None):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    if token:
        req.add_header("X-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_api_needs_the_page_token(server):
    assert call(server, "/api/status") == 403
    assert call(server, "/api/settings", body={"mode": "live"}) == 403


def test_api_rejects_other_hostnames(server):  # DNS rebinding: evil.com resolving to 127.0.0.1
    assert call(server, "/api/status", token="t0k", host="evil.com") == 403


def test_secrets_are_never_sent_to_the_page(server, tmp_path):
    (tmp_path / ".env").write_text("UPBIT_ACCESS_KEY=" + "A" * 40 + "\nUPBIT_SECRET_KEY=" + "S" * 40 + "\n")
    with urllib.request.urlopen(urllib.request.Request(server + "/api/status", headers={"X-Token": "t0k"})) as r:
        body = r.read().decode()
    assert "A" * 40 not in body and "S" * 40 not in body
    assert json.loads(body)["keys_set"] is True


def test_an_empty_amount_keeps_the_saved_value():
    clean, errors = ui.validate({"paper_krw": "", "budget_krw": " "})
    assert errors == [] and clean == {}


def test_the_mode_cannot_change_under_a_running_bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ui.live, "running_mode", lambda: "paper")
    code, body = ui.save({"mode": "live"})
    assert code == 409 and not (tmp_path / "config.toml").exists()
    assert ui.save({"paper_krw": "2,000,000"})[0] == 200  # other settings: saved, applied on restart


def test_keys_file_is_private_from_the_first_byte(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ui.live, "running_mode", lambda: None)
    (tmp_path / ".env.example").write_text("# 업비트 키\nUPBIT_ACCESS_KEY=\n")
    assert ui.save({"UPBIT_ACCESS_KEY": "A" * 40})[0] == 200
    text = (tmp_path / ".env").read_text()
    assert text.startswith("# 업비트 키") and "A" * 40 in text  # setup comments kept
    if os.name != "nt":
        assert (tmp_path / ".env").stat().st_mode & 0o077 == 0


def test_profit_shows_what_fees_and_slippage_cost():
    import pandas as pd
    log = pd.DataFrame({"date": ["2026-10-01", "2026-10-02", "2026-10-03"], "strategy": "donchian",
                        "equity": [1_000_000, 1_010_000, 1_030_000], "funded": 1_000_000})
    trades = pd.DataFrame({"time": ["2026-10-01T05:00:00+00:00", "2026-10-03T09:00:00+00:00"], "qty": [1.0, 1.0],
                           "price": [500_000, 520_000], "value": [500_000, None], "fee": [250.0, None],
                           "slip_cost": [100.0, None], "slip": [0.0002, 0.001]})
    out = ui.profit(log, trades, fee=0.0005)
    assert out["net"] == 30_000
    assert out["fees"] == pytest.approx(250 + 260)  # the old row's fee is estimated from qty * price
    assert out["slippage"] == pytest.approx(100 + 520)
    assert out["series"] == [["2026-10-01", 0, 350], ["2026-10-02", 10_000, 350], ["2026-10-03", 30_000, 1130]]
