"""Render saved calibration data without network access (Matplotlib via VectorBT)."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = json.loads(args.input.read_text())
    bins = [b for b in result["metrics"]["calibration"] if b["count"]]
    fig, ax = plt.subplots(figsize=(5, 4), layout="constrained")
    ax.plot([0, 1], [0, 1], "--", color="gray", label="Perfect calibration")
    ax.plot([b["mean_p"] for b in bins], [b["observed_frequency"] for b in bins],
            marker="o", label=f"{result['mode']} (n={result['metrics']['n']})")
    ax.set(xlim=(0, 1), ylim=(0, 1), xlabel="Mean forecast probability",
           ylabel="Observed YES frequency", title="Synthetic mock calibration")
    ax.legend()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    main()
