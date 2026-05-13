from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    args = parser.parse_args()
    config = load_config(args.config)
    queries = load_queries()

    no_cache_config = copy.deepcopy(config)
    no_cache_config.cache.enabled = False
    no_cache_metrics = run_simulation(no_cache_config, queries)

    with_cache_metrics = run_simulation(config, queries)
    metrics = with_cache_metrics
    metrics.write_json(args.out)

    out_path = Path(args.out)
    no_cache_path = out_path.with_name("metrics.no_cache.json")
    with_cache_path = out_path.with_name("metrics.with_cache.json")
    no_cache_metrics.write_json(no_cache_path)
    with_cache_metrics.write_json(with_cache_path)

    comparison = {
        "no_cache": no_cache_metrics.to_report_dict(),
        "with_cache": with_cache_metrics.to_report_dict(),
    }
    out_path.with_name("metrics.comparison.json").write_text(json.dumps(comparison, indent=2, ensure_ascii=False))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
