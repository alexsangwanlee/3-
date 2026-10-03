"""V4 of results/data_study.md: Kronos-small zero-shot 24h forecasts at every candidate entry bar.

    git clone https://github.com/shiyu-coder/Kronos /tmp/kronos
    pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install einops huggingface_hub safetensors
    PYTHONPATH=. python scripts/data_study.py candidates
    PYTHONPATH=. python scripts/kronos_forecasts.py --kronos /tmp/kronos

Resumable: forecasts are appended to data/ext/kronos_preds.csv as they finish.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

from tradebot import data

LOOKBACK, HORIZON, SAMPLES, BATCH = 400, 6, 5, 16
EXT = Path("data/ext")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kronos", required=True, help="path of a clone of github.com/shiyu-coder/Kronos")
    args = ap.parse_args()
    sys.path.insert(0, args.kronos)
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer

    torch.manual_seed(0)
    predictor = KronosPredictor(Kronos.from_pretrained("NeoQuasar/Kronos-small"),
                                KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base"), device="cpu",
                                max_context=512)
    todo = pd.read_csv(EXT / "kronos_candidates.csv", parse_dates=["time"])
    out = EXT / "kronos_preds.csv"
    if out.exists():
        done = pd.read_csv(out, parse_dates=["time"])
        todo = todo.merge(done[["market", "time"]], how="left", indicator=True).query("_merge == 'left_only'")
    bars = {m: data.load(m, 240, 1825, offline=True) for m in todo["market"].unique()}
    jobs = []
    for m, t in zip(todo["market"], todo["time"]):
        df = bars[m]
        hist = df[df.index < t].iloc[-LOOKBACK:]  # up to the previous bar's close: what was known at the entry
        if len(hist) == LOOKBACK:
            jobs.append((m, t, hist))
    print(f"{len(jobs)} forecasts to make", flush=True)
    for i in range(0, len(jobs), BATCH):
        chunk = jobs[i:i + BATCH]
        xs = [pd.Series(h.index.tz_localize(None)) for _, _, h in chunk]
        ys = [pd.Series(pd.date_range(h.index[-1].tz_localize(None) + pd.Timedelta(hours=4), periods=HORIZON, freq="4h"))
              for _, _, h in chunk]
        preds = predictor.predict_batch([h[["open", "high", "low", "close", "volume"]].reset_index(drop=True)
                                         for _, _, h in chunk], xs, ys, pred_len=HORIZON, T=1.0, top_p=0.9,
                                        sample_count=SAMPLES, verbose=False)
        rows = pd.DataFrame({"market": [m for m, _, _ in chunk], "time": [t for _, t, _ in chunk],
                             "pred": [p["close"].iloc[-1] / h["close"].iloc[-1] - 1 for (_, _, h), p in zip(chunk, preds)]})
        rows.to_csv(out, mode="a", header=not out.exists(), index=False)
        print(f"{i + len(chunk)}/{len(jobs)}", flush=True)


if __name__ == "__main__":
    main()
