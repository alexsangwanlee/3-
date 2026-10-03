# tradebot: notes for Claude Code

This is an Upbit (Korean exchange) spot trading bot that moves real money. The users are Korean non-programmers.
Everything users see (CLI messages, panel, Telegram, docs) is Korean, plain words. Code and comments are English.

## Commands
- `python -m pytest -q`: ~100 tests, about 12 s, no network.
- `ruff check tradebot tests scripts`.
- `./start.sh` opens the panel. `./start.sh run` runs headless. `./start.sh check`, `report`, `review`, `resume` and `update` are subcommands.
- The research scripts live in `scripts/` and need extra packages; see each script's docstring.

## Rules that are not negotiable
- **Secrets.** Never commit `.env`, `config.toml`, `state/`, `logs/` or `data/` (all gitignored). Never use real API keys in tests or QA. Never start live mode unless the user explicitly asks.
- **Money path** (live.py, upbit.py, risk.py, backtest.py, the run loop): change it test-first against the fake exchange in `tests/test_live_exchange.py`, then have the `live-money-reviewer` agent check the diff. Its invariants are listed in `.claude/agents/live-money-reviewer.md`.
- **Strategy changes** (new strategy, filter, data source, model): first read `results/knowledge.json`, which lists every idea already tested and its verdict; don't re-test those unless the data changed. Then follow `.claude/agents/strategy-researcher.md`: pre-register, walk-forward, lockbox. Most ideas fail, and they stay out of production. Add each result to `results/` and to `results/knowledge.json`.
- **Claude in the product** (`tradebot/claude.py`): it never places orders, and autopilot may only reduce risk. Treat model output as untrusted input.
- **UI changes:** run the `panel-qa` agent (agent-browser, paper mode, fake keys).
- **Setup or docs changes:** run the `first-run-tester` agent.
- Keep diffs small (ponytail style): the stdlib or an existing dependency first. Never add a runtime dependency without a strong reason.

## Where things are
- `tradebot/`:
  - `live.py`: bot loop, ledgers, pending orders.
  - `__main__.py`: CLI, `diagnose`, run loop, weekly re-optimisation.
  - `ui.py` and `ui.html`: the panel.
  - `claude.py`: weekly review.
  - `learn.py`: gated self-review.
  - `report.py`: plan phases.
- `results/`: research record and the operating plan (`plan.md`), including the pre-registered studies.
- `docs/GUIDE.md`: the user guide (screenshots in `docs/img/`). Keep it in sync with the panel's labels.
- `state/selected.json` is the weekly selection, written by the bot. `results/selected.json` is the shipped seed.
