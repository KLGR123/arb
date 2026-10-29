"""One durable stage per claim; engine execution is isolated and time bounded."""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

from .store import Store

ENGINE_TIMEOUT = 120


def execute(job):
    proc = subprocess.run(
        [sys.executable, "-m", "arb_research.engines"],
        input=json.dumps(job), capture_output=True, text=True, timeout=ENGINE_TIMEOUT,
    )
    if proc.returncode:
        logging.error("Engine failed for %s/%s: %s", job["run_id"], job["engine"], proc.stderr[-4000:])
        raise RuntimeError("engine_execution_failed")
    return json.loads(proc.stdout)


def work_once(store, runner=execute) -> bool:
    job = store.claim()
    if job is None:
        return False
    try:
        result = runner(job)
        store.finish(job, result)
    except Exception:
        logging.exception("Backtest stage failed: %s/%s", job["run_id"], job["engine"])
        # Never expose exception text, paths, or environment through the wire contract.
        store.finish(job, None, error="engine_execution_failed")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("ARB_BACKTEST_DB", ".state/backtests.sqlite3"))
    parser.add_argument("--once", action="store_true", help="Process at most one stage and exit")
    args = parser.parse_args()
    store = Store(args.db)
    while True:
        worked = work_once(store)
        if args.once:
            return
        if not worked:
            time.sleep(0.5)


if __name__ == "__main__":
    main()
