"""Local control panel: `python -m tradebot ui` -> http://127.0.0.1:8765

Stdlib only. Listens on 127.0.0.1, every request must carry a localhost Host header (blocks DNS
rebinding) and every API call the per-start token embedded in the page. API keys are write-only:
the page can set them but never read them back.
"""
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

from . import claude, config, live
from .report import report
from .strategies import STRATEGIES

PAGE = Path(__file__).with_name("ui.html")
SECRET_KEYS = {"UPBIT_ACCESS_KEY": r"[A-Za-z0-9]{20,80}", "UPBIT_SECRET_KEY": r"[A-Za-z0-9]{20,80}",
               "TELEGRAM_BOT_TOKEN": r"\d+:[A-Za-z0-9_-]{20,}", "TELEGRAM_CHAT_ID": r"-?\d+",
               "ANTHROPIC_API_KEY": r"sk-ant-[A-Za-z0-9_-]{20,}"}
LABELS = {"UPBIT_ACCESS_KEY": "Access 키", "UPBIT_SECRET_KEY": "Secret 키",
          "TELEGRAM_BOT_TOKEN": "텔레그램 봇 토큰", "TELEGRAM_CHAT_ID": "텔레그램 chat id", "ANTHROPIC_API_KEY": "Claude API 키"}
CONFIG_KEYS = ("mode", "budget_krw", "paper_krw", "markets", "strategies", "claude_autopilot")


# -- settings files ----------------------------------------------------------

def _clean(value) -> str:
    return str(value).strip().strip("'\"").strip()


def validate(payload: dict) -> tuple[dict, list[str]]:
    out, errors = {}, []
    for key, value in payload.items():
        if key == "mode":
            if value in ("paper", "live"):
                out[key] = value
            else:
                errors.append("모드는 모의매매(paper) 또는 실거래(live)만 고를 수 있습니다.")
        elif key in ("budget_krw", "paper_krw"):
            v = _clean(value).replace(",", "")
            if not v:
                continue  # empty field = keep what is saved
            try:
                n = float(v)
            except ValueError:
                n = -1
            low = live.MIN_ORDER_KRW if key == "paper_krw" else 0
            if low <= n <= 1e12 and (n == 0 or n >= live.MIN_ORDER_KRW):
                out[key] = int(n)
            else:
                label = "모의매매 자금" if key == "paper_krw" else "실거래 금액"
                errors.append(f"{label}은 {live.MIN_ORDER_KRW:,}원 이상의 숫자로 적어 주세요 (예: 1000000)."
                              + ("" if low else " 실거래를 안 쓰면 0."))
        elif key == "markets":
            ms = [_clean(m).upper() for m in value if _clean(m)]
            if ms and all(re.fullmatch(r"KRW-[A-Z0-9]{2,10}", m) for m in ms):
                out[key] = ms
            else:
                errors.append("마켓은 KRW-BTC 처럼 'KRW-코인' 형식으로 하나 이상 고르세요.")
        elif key == "claude_autopilot":
            if isinstance(value, bool):
                out[key] = value
            else:
                errors.append("Claude 자동 실행은 켜기/끄기만 고를 수 있습니다.")
        elif key == "strategies":
            if value and all(s in STRATEGIES and s != "hold" for s in value):
                out[key] = list(value)
            else:
                errors.append("전략을 하나 이상 고르세요.")
        elif key in SECRET_KEYS:
            v = _clean(value)
            if not v:
                continue  # empty field = keep what is saved
            if re.fullmatch(SECRET_KEYS[key], v):
                out[key] = v
            else:
                errors.append(f"{LABELS[key]} 형식이 맞지 않습니다. 업비트·텔레그램에서 복사한 값을 그대로 붙여 넣으세요.")
        else:
            errors.append(f"알 수 없는 항목: {key}")
    return out, errors


def upsert_env(text: str, updates: dict) -> str:
    lines = text.splitlines()
    for key, value in updates.items():
        line = f"{key}={_clean(value)}"
        hit = next((i for i, l in enumerate(lines) if not l.lstrip().startswith("#") and l.split("=", 1)[0].strip() == key), None)
        if hit is None:
            lines.append(line)
        else:
            lines[hit] = line
    return "\n".join(lines) + "\n"


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False) if isinstance(v, str) else str(v)


def _comment(line: str) -> str:
    """Trailing `# ...` (with the spaces before it), ignoring # inside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if ch in "\"'":
            quote = None if quote == ch else (quote or ch)
        elif ch == "#" and not quote:
            return line[len(line[:i].rstrip()):]
    return ""


def set_toml_top(text: str, updates: dict) -> str:
    """Change top-level `key = value` lines, keeping comments and everything under [tables]."""
    lines = text.splitlines()
    end = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    for key, value in updates.items():
        new = f"{key} = {_toml(value)}"
        hit = next((i for i in range(end) if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])), None)
        if hit is not None:
            lines[hit] = new + _comment(lines[hit])
        else:
            at = max((i for i in range(end) if lines[i].strip()), default=-1) + 1
            lines.insert(at, new)
            end += 1
    return "\n".join(lines) + "\n"


def read_env(path=".env") -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    pairs = (l.split("=", 1) for l in p.read_text(encoding="utf-8").splitlines() if "=" in l and not l.lstrip().startswith("#"))
    return {k.strip(): _clean(v) for k, v in pairs}


def env() -> dict:
    """What the bot will see: the real environment, overridden by the .env file the page just saved."""
    return {**os.environ, **{k: v for k, v in read_env().items() if v}}


def _use_saved_keys() -> None:
    """Keys saved in the page after this panel started: make them visible to code running in this process."""
    os.environ.update({k: v for k, v in read_env().items() if v and k in SECRET_KEYS})


def save(payload: dict) -> tuple[int, dict]:
    clean, errors = validate(payload)
    if errors:
        return 400, {"errors": errors}
    running = live.running_mode()
    if running and clean.get("mode", running) != running:
        return 409, {"errors": ["봇이 돌고 있는 동안에는 모드를 바꿀 수 없습니다. 먼저 정지하세요."]}
    cfg = Path("config.toml")
    if not cfg.exists():
        if Path("config.example.toml").exists():
            shutil.copy("config.example.toml", cfg)
        else:
            cfg.write_text("")
    text = set_toml_top(cfg.read_text(encoding="utf-8"), {k: v for k, v in clean.items() if k in CONFIG_KEYS})
    tomllib.loads(text)  # never write a file the bot cannot read
    cfg.write_text(text, encoding="utf-8")
    keys = {k: v for k, v in clean.items() if k in SECRET_KEYS}
    if keys:
        e = Path(".env")
        old = next((p.read_text(encoding="utf-8") for p in (e, Path(".env.example")) if p.exists()), "")
        fd = os.open(e, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never readable by others, not even briefly
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(upsert_env(old, keys))
        if os.name != "nt":
            e.chmod(0o600)  # an older .env may have been created with looser permissions
    return 200, {"saved": True, "restart_needed": live.running_pid() is not None}


# -- what the page shows -----------------------------------------------------

def _csv(path: str) -> pd.DataFrame:
    p = Path(path)
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def status() -> dict:
    cfg = config.load()
    e = env()
    running = live.running_mode()
    mode = running or cfg.mode  # what is trading now, not what was saved for the next start
    sleeves, positions = [], []
    for p in sorted(Path("state").glob(f"{mode}_*.json")):  # includes a removed strategy still holding coins
        s = p.stem.removeprefix(f"{mode}_")
        st = json.loads(p.read_text(encoding="utf-8"))
        if s in STRATEGIES and (s in cfg.strategies or st["positions"]):
            sleeves.append({"strategy": s, "cash": st["cash"], "funded": st["funded"], "positions": st["positions"],
                            "halted": bool((st.get("guard") or {}).get("halted"))})
    held = sorted({m for s in sleeves for m in s["positions"]})
    try:
        from .upbit import UpbitClient
        prices = UpbitClient().tickers(held) if held else {}
    except Exception:
        prices = {}
    equity = funded = 0.0
    for s in sleeves:
        equity += s["cash"]
        funded += s["funded"]
        for m, pos in s["positions"].items():
            px = prices.get(m, pos.get("last", pos["entry"]))
            equity += pos["qty"] * px
            positions.append({"strategy": s["strategy"], "market": m, "value": pos["qty"] * px, "entry": pos["entry"],
                              "price": px, "pnl": px / pos["entry"] - 1, "stop": pos.get("stop")})
    log = _csv(f"logs/{mode}_equity.csv")
    curve = (log.pivot_table(index="date", columns="strategy", values="equity").ffill().sum(axis=1)
             if not log.empty else pd.Series(dtype=float))
    trades = _csv(f"logs/{mode}_trades.csv").tail(15).iloc[::-1].fillna("").to_dict("records")
    tail = Path("logs/bot.log").read_text(encoding="utf-8", errors="replace").splitlines()[-60:] if Path("logs/bot.log").exists() else []
    sel = config.selection()
    return {
        "running": running is not None, "mode": mode,
        "stopping": running is not None and live.STOP_FILE.exists(),
        "halted": [s["strategy"] for s in sleeves if s["halted"]],  # -35% kill switch fired: no new buys
        "first_run": mode == "paper" and log.empty,  # never ran yet: show the paper-trading onboarding
        "config": {"mode": mode, "budget_krw": cfg.budget_krw, "paper_krw": cfg.paper_krw,
                   "markets": cfg.markets, "strategies": cfg.strategies, "claude_autopilot": cfg.claude_autopilot},
        "claude_set": bool(e.get("ANTHROPIC_API_KEY")), "paused": live.pause_info(),
        "claude": json.loads(claude.REVIEW.read_text(encoding="utf-8")) if claude.REVIEW.exists() else None,
        "keys_set": bool(e.get("UPBIT_ACCESS_KEY") and e.get("UPBIT_SECRET_KEY")),
        "telegram_set": bool(e.get("TELEGRAM_BOT_TOKEN") and e.get("TELEGRAM_CHAT_ID")),
        "equity": equity, "funded": funded, "positions": positions, "trades": trades, "log": tail,
        "curve": [[d, round(v)] for d, v in curve.items()],
        "report": report(mode, f"logs/{mode}_equity.csv"),
        "sleeves": {k: {"params": v["params"], "tradable": v["tradable"]} for k, v in sel.get("sleeves", {}).items()},
        "optimized_at": sel.get("generated_at"),
        "recommend": (sel.get("self_review") or {}).get("recommend"),
        "all_strategies": [s for s in STRATEGIES if s != "hold"],
    }


def check(telegram: bool = False) -> dict:
    from .__main__ import diagnose
    from .upbit import UpbitClient

    e = env()
    ok, problems = diagnose(config.load(), UpbitClient(e.get("UPBIT_ACCESS_KEY"), e.get("UPBIT_SECRET_KEY")), e)
    if telegram and e.get("TELEGRAM_BOT_TOKEN") and e.get("TELEGRAM_CHAT_ID"):
        _use_saved_keys()
        if not live.send_telegram("tradebot 연결 점검: 이 메시지가 보이면 알림 설정 완료입니다."):
            ok = [line for line in ok if not line.startswith("텔레그램")]
            problems.append("텔레그램 전송 실패: 토큰과 chat id 를 확인하고, 텔레그램에서 내 봇에게 /start 를 먼저 보내세요.")
    return {"ok": ok, "problems": problems}


def start(live_confirmed: bool) -> tuple[int, dict]:
    if live.running_pid():
        return 409, {"error": "이미 실행 중입니다."}
    cfg = config.load()
    args = [sys.executable, "-m", "tradebot", "run"]
    problems = check()["problems"]  # the same checks `run` does, answered here instead of in a log file
    if problems:
        return 400, {"error": "시작하지 못했습니다. 아래를 고친 뒤 다시 누르세요.", "problems": problems}
    if cfg.mode == "live":
        if not live_confirmed:
            return 400, {"error": "실거래는 '실제 돈으로 매매합니다' 확인란을 체크해야 시작됩니다."}
        args.append("--i-understand-the-risk")
    Path("logs").mkdir(exist_ok=True)
    live.STOP_FILE.unlink(missing_ok=True)
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
              else {"start_new_session": True})  # keeps trading when this window closes
    out = Path("logs/run.out")
    before = out.stat().st_size if out.exists() else 0
    with out.open("ab") as f:
        proc = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, env={**env(), "PYTHONUTF8": "1"}, **detach)
    for _ in range(20):  # a bot that dies at once says why, here
        time.sleep(0.1)
        if proc.poll() is not None:
            why = out.read_bytes()[before:].decode("utf-8", "replace").strip().splitlines()[-5:]
            return 500, {"error": "봇이 바로 멈췄습니다.", "problems": why}
    return 200, {"started": True}


def review_now() -> tuple[int, dict]:
    _use_saved_keys()
    out = claude.run(config.load())
    if out is None:
        return 400, {"error": "Claude 검토를 하지 못했습니다. Claude API 키와 인터넷 연결을 확인하세요."}
    return 200, out


def resume_entries() -> tuple[int, dict]:
    live.PAUSE_FILE.unlink(missing_ok=True)
    return 200, {"resumed": True}


def stop() -> tuple[int, dict]:
    if not live.running_pid():
        return 200, {"stopped": True}
    live.STOP_FILE.parent.mkdir(exist_ok=True)
    live.STOP_FILE.touch()  # the bot finishes its current step, saves, then exits
    return 200, {"stopping": True}


# -- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    token = ""

    def _allowed(self) -> bool:
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        return not self.path.startswith("/api/") or hmac.compare_digest(self.headers.get("X-Token", ""), self.token)

    def _send(self, code: int, body, ctype="application/json; charset=utf-8") -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "제어판이 다시 시작됐습니다. 이 페이지를 새로고침하세요."})
        if self.path == "/":
            return self._send(200, PAGE.read_text(encoding="utf-8").replace("__TOKEN__", self.token), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "제어판이 다시 시작됐습니다. 이 페이지를 새로고침하세요."})
        size = int(self.headers.get("Content-Length") or 0)
        if size > 20_000:
            return self._send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        routes = {"/api/settings": lambda: save(body), "/api/check": lambda: (200, check(telegram=True)),
                  "/api/start": lambda: start(bool(body.get("live_confirmed"))), "/api/stop": stop,
                  "/api/claude/review": review_now, "/api/claude/approve": lambda: (200, {"status": claude.approve(config.load())}),
                  "/api/entries/resume": resume_entries}
        if self.path not in routes:
            return self._send(404, {"error": "not found"})
        try:
            self._send(*routes[self.path]())
        except Exception as e:  # show the reason on the page instead of a dead button
            self._send(500, {"error": f"처리 중 오류: {e}"})

    def log_message(self, *args):
        pass


def make_server(port: int = 8765, token: str | None = None) -> ThreadingHTTPServer:
    handler = type("PanelHandler", (Handler,), {"token": token or secrets.token_urlsafe(24)})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    srv = make_server(port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"제어판: {url}  (이 창을 닫아도 봇은 계속 돕니다. 제어판만 닫힙니다)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
