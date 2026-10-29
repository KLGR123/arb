"""Offline replay plus explicit public archive/local inference commands."""

import argparse
import json
from pathlib import Path

from .context import canonical
from .demo import cohort
from .public import PublicClient, collect
from .runner import AuditStore, MockPredictor, RecordedPredictor, run


def main():
    parser = argparse.ArgumentParser(description="Read-only offline forecast evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "evaluate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--output", type=Path, required=True)
        cmd.add_argument("--db", type=Path, default=Path(".state/forecasts.sqlite3"))
        cmd.add_argument("--seed", type=int, default=11)
        cmd.add_argument("--samples", type=int, default=2)
        cmd.add_argument("--bootstrap", type=int, default=2000)
        cmd.add_argument("--fee-bps", type=float, default=20)
        cmd.add_argument("--kelly-fraction", type=float, default=0.25)
        cmd.add_argument("--kelly-cap", type=float, default=0.1)
        if name == "evaluate":
            cmd.add_argument("--input", type=Path, required=True)
            cmd.add_argument("--predictions", type=Path,
                             help="JSON array of model/training_cutoff/records for authorized offline responses")
        else:
            cmd.add_argument("--count", type=int, default=60)
    cmd = sub.add_parser("collect")
    cmd.add_argument("--output", type=Path, required=True)
    cmd.add_argument("--count", type=int, default=50)
    cmd.add_argument("--max-pages", type=int, default=10)
    for name in ("archive-open", "predict-open", "join", "score", "history"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--output", type=Path, required=True)
        if name == "archive-open":
            cmd.add_argument("--count", type=int, default=56)
            cmd.add_argument("--min-minutes", type=int, default=15)
            cmd.add_argument("--max-minutes", type=int, default=45)
        else:
            cmd.add_argument("--input", type=Path, required=True)
        if name in ("join", "score"):
            cmd.add_argument("--predictions", type=Path, required=True)
        if name == "predict-open":
            cmd.add_argument("--codex", default="codex")
            cmd.add_argument("--effort", choices=("low", "medium", "high"), default="low")
            cmd.add_argument("--samples", type=int, default=2)
            cmd.add_argument("--workers", type=int, default=4)
            cmd.add_argument("--db", type=Path, default=Path(".state/forecasts.sqlite3"))
    args = parser.parse_args()
    if args.command in ("archive-open", "predict-open", "join", "score", "history"):
        from .prospective import archive_open, predict_archive, join_settlements, score_joined, archive_history
        if args.command == "archive-open":
            result = archive_open(PublicClient(), args.output, count=args.count,
                                  min_minutes=args.min_minutes, max_minutes=args.max_minutes)
        elif args.command == "predict-open":
            from .codex import CodexPredictor
            result = predict_archive(json.loads(args.input.read_text()),
                                     CodexPredictor(args.codex, effort=args.effort), AuditStore(args.db),
                                     args.output, samples=args.samples, workers=args.workers)
        elif args.command == "history":
            result = archive_history(PublicClient(), json.loads(args.input.read_text()), args.output)
        elif args.command == "join":
            result = join_settlements(PublicClient(), json.loads(args.input.read_text()),
                                      json.loads(args.predictions.read_text()), args.output)
        else:
            result = score_joined(json.loads(args.input.read_text()), json.loads(args.predictions.read_text()))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
        print(canonical({"command": args.command, "output": str(args.output)}))
        return
    if args.command == "collect":
        result = collect(PublicClient(), count=args.count, max_pages=args.max_pages)
    else:
        markets = (cohort(args.count, args.seed) if args.command == "demo"
                   else json.loads(args.input.read_text())["markets"])
        predictors = [MockPredictor("mock-a", args.seed), MockPredictor("mock-b", args.seed)]
        if args.command == "evaluate" and args.predictions:
            predictors = [RecordedPredictor(**item) for item in json.loads(args.predictions.read_text())]
        result = run(markets, predictors,
                     AuditStore(args.db), samples=args.samples, seed=args.seed,
                     bootstrap=args.bootstrap, fee_bps=args.fee_bps,
                     kelly_fraction=args.kelly_fraction, cap=args.kelly_cap)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(canonical({"mode": result.get("mode", "collection"),
                     "n": result.get("metrics", {}).get("n", len(result.get("markets", []))),
                     "output": str(args.output)}))


if __name__ == "__main__":
    main()
