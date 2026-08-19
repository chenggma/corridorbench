#!/usr/bin/env python3
"""Persistence baseline: score fit-day OBSERVED flows directly against
holdout-day observations — no simulation at all (meteorology's
"yesterday equals today" reference; WeatherBench convention).

If persistence scores high, day-to-day variability is low and the
calibration task is easier than it looks; if low, it bounds what ANY
one-parameter-set model can achieve across days.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from corridorbench import paths, scoring, taskset  # noqa


def main():
    tasks, data = taskset.load_tasks()
    out = {}
    print(f"{'task':<18} {'persist(day)':<22} cov    meanGEH")
    for t in tasks:
        rows = {}
        for f in t.fit_days:
            sc = scoring.score_day(
                data.obs_flow[f], data.obs_speed[f],
                data.obs_flow[t.holdout_day], data.obs_speed[t.holdout_day],
                t.interior)
            rows[f] = {"cov_geh5": sc["cov_geh5"],
                       "mean_geh": sc["mean_geh"],
                       "n": sc["n_station_hours"]}
            print(f"{t.task_id:<18} obs({f})      "
                  f"{sc['cov_geh5']*100:5.1f}%  {sc['mean_geh']:.2f}")
        out[t.task_id] = rows
    with open(os.path.join(paths.RESULTS, "persistence.json"), "w") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
