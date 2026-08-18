"""Baseline campaign for CorridorBench v0.1: run candidate parameter sets
on all three days and serialize station series for scoring.

Candidates:
  identity     harness defaults (uncalibrated boundary-driven twin)
  scale_*      global demand scale s on s_entry AND s_or, all hours, both
               directions (the naive 'turn demand up/down' move)
  prop0        the Stage-0 selected candidate (argmin over 22 logged SUMO
               runs; la_network/calibration/stage0/params_calibrated.json)

Every run is one SUMO execution covering BOTH directions, so each
(candidate, day) row scores all tasks that share that day.
Deterministic: fixed built-in seeds, unchanged network (see adapter doc).
"""
import concurrent.futures as cf
import csv
import json
import os
import sys
import time

from . import paths


def load_prop0(H):
    p = json.load(open(os.path.join(paths.STAGE0,
                                    "params_calibrated.json")))["params"]
    out = H.default_params()
    for d in "NS":
        for kind in ("s_entry", "s_or", "m_off"):
            for h, v in p[d][kind].items():
                out[d][kind][int(h)] = float(v)
        out[d]["q_dead"] = float(p[d]["q_dead"])
    return out


def scaled(H, s):
    p = H.default_params()
    for d in "NS":
        for kind in ("s_entry", "s_or"):
            for h in p[d][kind]:
                p[d][kind][h] = s
    return p


def candidates(H):
    cands = {"identity": H.default_params(), "prop0": load_prop0(H)}
    for s in (0.7, 0.85, 1.15, 1.3):
        cands[f"scale_{s:.2f}".replace(".", "p")] = scaled(H, s)
    return cands


def serialize(run_dir, sim_flow, sim_speed, stats):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "flow.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["station", "t", "veh_5min"])
        for (st, t), v in sorted(sim_flow.items()):
            w.writerow([st, t, v])
    with open(os.path.join(run_dir, "speed.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["station", "t", "mph"])
        for (st, t), v in sorted(sim_speed.items()):
            w.writerow([st, t, round(v, 3)])
    with open(os.path.join(run_dir, "stats.json"), "w") as f:
        json.dump(stats, f, indent=1)


def load_run(run_dir):
    flow, speed = {}, {}
    for r in csv.DictReader(open(os.path.join(run_dir, "flow.csv"))):
        flow[(r["station"], int(r["t"]))] = float(r["veh_5min"])
    for r in csv.DictReader(open(os.path.join(run_dir, "speed.csv"))):
        speed[(r["station"], int(r["t"]))] = float(r["mph"])
    return flow, speed


def main(workers=2, only=None):
    H, SA = paths.import_stage0()
    data = H.Data.load()
    adapter = SA.SumoAdapter()
    cands = candidates(H)
    if only:
        cands = {k: v for k, v in cands.items() if k in only}
    days = ["2026-06-02", "2026-06-03", "2026-06-04"]
    outroot = os.path.join(paths.RESULTS, "campaign")
    os.makedirs(outroot, exist_ok=True)
    log_path = os.path.join(outroot, "campaign_log.csv")
    new = not os.path.exists(log_path)
    logf = open(log_path, "a", newline="")
    logw = csv.writer(logf)
    if new:
        logw.writerow(["tag", "day", "wall_s", "loaded", "inserted",
                       "teleports", "run_dir", "finished_utc"])

    jobs = []
    for tag, params in cands.items():
        for day in days:
            run_dir = os.path.join(outroot, f"{tag}__{day}")
            if os.path.exists(os.path.join(run_dir, "flow.csv")):
                print(f"skip {tag} {day} (exists)", flush=True)
                continue
            jobs.append((tag, params, day, run_dir))

    def one(job):
        tag, params, day, run_dir = job
        spec = H.build_boundary_spec(day, params, data)
        work = os.path.join(run_dir, "work")
        res = adapter.run(spec, work)
        serialize(run_dir, res.flow, res.speed, res.stats)
        return tag, day, res.stats, run_dir

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, j) for j in jobs]
        for fut in cf.as_completed(futs):
            tag, day, stats, run_dir = fut.result()
            logw.writerow([tag, day, stats.get("wall_s"),
                           stats.get("loaded"), stats.get("inserted"),
                           stats.get("teleports"), run_dir,
                           time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime())])
            logf.flush()
            print(f"done {tag} {day} wall={stats.get('wall_s')}s "
                  f"({time.time()-t0:.0f}s elapsed)", flush=True)
    print("campaign complete", flush=True)


if __name__ == "__main__":
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    main(only=only)
