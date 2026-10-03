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
import tomllib
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

from . import config, live
from .report import report
from .strategies import STRATEGIES

PAGE = Path(__file__).with_name("ui.html")
SECRET_KEYS = {"UPBIT_ACCESS_KEY": r"[A-Za-z0-9]{20,80}", "UPBIT_SECRET_KEY": r"[A-Za-z0-9]{20,80}",
               "TELEGRAM_BOT_TOKEN": r"\d+:[A-Za-z0-9_-]{20,}", "TELEGRAM_CHAT_ID": r"-?\d+"}
LABELS = {"UPBIT_ACCESS_KEY": "Access 키", "UPBIT_SECRET_KEY": "Secret 키",
          "TELEGRAM_BOT_TOKEN": "텔레그램 봇 토큰", "TELEGRAM_CHAT_ID": "텔레그램 chat id"}
CONFIG_KEYS = ("mode", "budget_krw", "paper_krw", "markets", "strategies")


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
            try:
                n = float(_clean(value).replace(",", "") or 0)
            except ValueError:
                n = -1
            if 0 <= n <= 1e12:
                out[key] = int(n)
            else:
                errors.append("금액은 0 이상의 숫자로 적어 주세요 (예: 1000000).")
        elif key == "markets":
            ms = [_clean(m).upper() for m in value if _clean(m)]
            if ms and all(re.fullmatch(r"KRW-[A-Z0-9]{2,10}", m) for m in ms):
                out[key] = ms
            else:
                errors.append("마켓은 KRW-BTC 처럼 'KRW-코인' 형식으로 하나 이상 고르세요.")
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


def save(payload: dict) -> tuple[int, dict]:
    clean, errors = validate(payload)
    if errors:
        return 400, {"errors": errors}
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
        e.write_text(upsert_env(e.read_text(encoding="utf-8") if e.exists() else "", keys), encoding="utf-8")
        if os.name != "nt":
            e.chmod(0o600)
    return 200, {"saved": True, "restart_needed": live.running_pid() is not None}


# -- what the page shows -----------------------------------------------------

def _csv(path: str) -> pd.DataFrame:
    p = Path(path)
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def status() -> dict:
    cfg = config.load()
    e = env()
    mode = cfg.mode
    sleeves, positions = [], []
    for s in cfg.strategies:
        p = Path(f"state/{mode}_{s}.json")
        if p.exists():
            st = json.loads(p.read_text())
            sleeves.append({"strategy": s, "cash": st["cash"], "funded": st["funded"], "positions": st["positions"]})
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
            px = prices.get(m, pos["entry"])
            equity += pos["qty"] * px
            positions.append({"strategy": s["strategy"], "market": m, "value": pos["qty"] * px, "entry": pos["entry"],
                              "price": px, "pnl": px / pos["entry"] - 1, "stop": pos.get("stop")})
    log = _csv(f"logs/{mode}_equity.csv")
    curve = (log.pivot_table(index="date", columns="strategy", values="equity").ffill().sum(axis=1)
             if not log.empty else pd.Series(dtype=float))
    trades = _csv(f"logs/{mode}_trades.csv").tail(15).iloc[::-1].fillna("").to_dict("records")
    tail = Path("logs/bot.log").read_text(encoding="utf-8", errors="replace").splitlines()[-60:] if Path("logs/bot.log").exists() else []
    sel = json.loads(Path(config.SELECTED).read_text()) if Path(config.SELECTED).exists() else {}
    return {
        "running": live.running_pid() is not None, "mode": mode,
        "first_run": log.empty,  # the bot never ran in this mode yet
        "config": {"mode": mode, "budget_krw": cfg.budget_krw, "paper_krw": cfg.paper_krw,
                   "markets": cfg.markets, "strategies": cfg.strategies},
        "keys_set": bool(e.get("UPBIT_ACCESS_KEY") and e.get("UPBIT_SECRET_KEY")),
        "telegram_set": bool(e.get("TELEGRAM_BOT_TOKEN") and e.get("TELEGRAM_CHAT_ID")),
        "equity": equity, "funded": funded, "positions": positions, "trades": trades, "log": tail,
        "curve": [[d, round(v)] for d, v in curve.items()],
        "report": report(mode, f"logs/{mode}_equity.csv"),
        "sleeves": {k: {"params": v["params"], "tradable": v["tradable"]} for k, v in sel.get("sleeves", {}).items()},
        "optimized_at": sel.get("generated_at"),
        "all_strategies": [s for s in STRATEGIES if s != "hold"],
    }


def check() -> dict:
    from .__main__ import diagnose
    from .upbit import UpbitClient

    e = env()
    ok, problems = diagnose(config.load(), UpbitClient(e.get("UPBIT_ACCESS_KEY"), e.get("UPBIT_SECRET_KEY")), e)
    return {"ok": ok, "problems": problems}


def start(live_confirmed: bool) -> tuple[int, dict]:
    if live.running_pid():
        return 409, {"error": "이미 실행 중입니다."}
    cfg = config.load()
    args = [sys.executable, "-m", "tradebot", "run"]
    if cfg.mode == "live":
        problems = check()["problems"]
        if problems:
            return 400, {"error": "실거래 점검을 통과하지 못했습니다.", "problems": problems}
        if not live_confirmed:
            return 400, {"error": "실거래는 '실제 돈으로 매매합니다' 확인란을 체크해야 시작됩니다."}
        args.append("--i-understand-the-risk")
    Path("logs").mkdir(exist_ok=True)
    live.STOP_FILE.unlink(missing_ok=True)
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
              else {"start_new_session": True})  # keeps trading when this window closes
    with open("logs/run.out", "ab") as out:
        subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, env=env(), **detach)
    return 200, {"started": True}


def stop() -> tuple[int, dict]:
    if not live.running_pid():
        return 200, {"stopped": True}
    live.STOP_FILE.parent.mkdir(exist_ok=True)
    live.STOP_FILE.touch()  # the bot finishes its current step, saves, then exits (<= 10 s)
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
            return self._send(403, {"error": "forbidden"})
        if self.path == "/":
            return self._send(200, PAGE.read_text(encoding="utf-8").replace("__TOKEN__", self.token), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        size = int(self.headers.get("Content-Length") or 0)
        if size > 20_000:
            return self._send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        routes = {"/api/settings": lambda: save(body), "/api/check": lambda: (200, check()),
                  "/api/start": lambda: start(bool(body.get("live_confirmed"))), "/api/stop": stop}
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
