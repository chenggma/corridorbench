#!/usr/bin/env python3
"""Engine-agnostic Stage-0 calibration harness for the I-710 instrumented
mid-corridor (Abs_PM 11.540-17.607, both directions; see
../corridor_calibration_plan.md sections 1.4, 2, 3, 5, 6).

Everything here is stdlib-only and simulator-independent. The engine
interface is:

    adapter.run(spec, workdir) -> SimResult

where `spec` is the fully MATERIALIZED boundary specification produced by
`build_boundary_spec(day, params, data)` (concrete 5-min inflow counts and
hourly exit probabilities, keyed by PeMS station id -- no engine concepts),
and SimResult carries per-ML-station 5-min flows (veh/5min) and speeds (mph)
plus run health stats. Adapters:
  - sumo_adapter.SumoAdapter   -- working now (Eclipse SUMO 1.27.1 twin,
    demos/demo_d_sumo_twin network, unchanged);
  - lpsim_adapter.LPSimAdapter -- documented stub for the GPU day.

Calibration parameters (plan section 5, Stage 0), per direction:
  s_entry[h]  hour-window scale on the measured mainline entry inflow,
              bounds [0.5, 2.0] (ASSUMED bounds from the task spec);
  s_or[h]     hour-window scale on all measured (alive) on-ramp inflows,
              bounds [0.5, 2.0];
  m_off[h]    hour-window multiplier on off-ramp exit probabilities,
              simplex-safe: p = clip(p_base * m_off, 0, P_MAX), bounds
              [0.5, 2.0];
  q_dead      dead-on-ramp injection level in veh/h/lane shaping the mean
              normalized alive-OR profile (plan section 2), bounds
              [0, 1200] veh/h/lane, initialized 0 (= baseline: dead ORs
              inject nothing).

Objective (plan section 4): mean hourly GEH over FIT interior ML stations
+ LAMBDA_SPEED * station-bin speed RMSE (mph), hours 05:00-10:59.
Holdout (plan section 6): day 2026-06-03 entirely, plus every 5th ML
station per direction in corridor-wide Abs_PM order (rank mod 5 == 2,
seedless) -- those stations never enter the objective.

Holdout masking scope (disclosure): the station holdout masks RESIDUALS
only. Measured flows at holdout stations still enter the simulation
INPUTS: build_boundary_spec uses the nearest-upstream ML station's
measured flow as each off-ramp exit-probability denominator, and 3 of the
5 holdout stations serve as denominators (N 717995; S 717989, 718012),
so the optimized m_off multiplies those measured bases on every day
including the holdout day. This is inherent to the measured-boundary
design and identical across all candidates; no holdout residual ever
feeds back into the optimizer.

Data-quality screens applied to fit inputs (plan sections 1.2, 6):
  - station 718011 (SB FR)'s 2026-06-04 series is suspect (4.3x daily jump)
    -> treated as dead on that day (IMPUTED dir-hour mean exit prob);
  - zeros-implausibility: any alive-ramp hour reporting 0 veh while the
    nearest same-direction ML carries >= 1000 veh/h is replaced by the same
    station's profile from the OTHER FIT day (IMPUTED, logged in the spec).
"""
import csv
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))          # .../stage0
CALDIR = os.path.dirname(HERE)                             # .../calibration
REPO = os.path.dirname(os.path.dirname(CALDIR))            # repo root
TWIN = os.path.join(REPO, "demos", "demo_d_sumo_twin")

T0_H, T0_M = 4, 30            # sim t=0 == 04:30:00 (same clock as the twin)
END = 23400                   # sim end 11:00:00
W0 = 1800                     # comparison window start 05:00
BINS = list(range(0, END, 300))
OBJ_BINS = list(range(W0, END, 300))
HOURS_ALL = list(range(4, 11))
HOURS_OBJ = list(range(5, 11))
FIT_DAYS = ["2026-06-02", "2026-06-04"]
HOLDOUT_DAY = "2026-06-03"
ALL_DAYS = ["2026-06-02", "2026-06-03", "2026-06-04"]
LAMBDA_SPEED = 0.2            # plan section 4 (ASSUMED, reported separately)
P_BASE_CLAMP = 0.8            # base exit-prob clamp (same as the twin)
P_MAX = 0.85                  # simplex-safe cap after the m_off multiplier
SUSPECT_ML_THRESH = 1000.0    # veh/h on nearest ML for the zeros screen
SUSPECT_FR_DAYS = {("2026-06-04", "718011")}   # plan section 1.2
PARAM_BOUNDS = {"s_entry": (0.5, 2.0), "s_or": (0.5, 2.0),
                "m_off": (0.5, 2.0), "q_dead": (0.0, 1200.0)}


def geh(m, c):
    """GEH statistic on hourly flows (veh/h)."""
    return math.sqrt(2.0 * (m - c) ** 2 / (m + c)) if (m + c) > 0 else 0.0


def hour_of(t):
    return T0_H + (T0_M * 60 + t) // 3600


def hour_base(h):
    """sim-seconds of hh:00 (may be negative for the 04:00 hour)."""
    return (h - T0_H) * 3600 - T0_M * 60


def clock(t):
    m = (T0_H * 60 + T0_M) + t // 60
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------------------------------------------------------------- parameters

def default_params():
    return {d: {"s_entry": {h: 1.0 for h in HOURS_ALL},
                "s_or": {h: 1.0 for h in HOURS_ALL},
                "m_off": {h: 1.0 for h in HOURS_ALL},
                "q_dead": 0.0} for d in "NS"}


def param_keys():
    """Deterministic flat ordering of the parameter vector."""
    keys = []
    for d in "NS":
        for kind in ("s_entry", "s_or", "m_off"):
            for h in HOURS_ALL:
                keys.append((d, kind, h))
        keys.append((d, "q_dead", None))
    return keys


def clamp_params(p):
    for d in "NS":
        for kind in ("s_entry", "s_or", "m_off"):
            lo, hi = PARAM_BOUNDS[kind]
            for h in HOURS_ALL:
                p[d][kind][h] = min(max(p[d][kind][h], lo), hi)
        lo, hi = PARAM_BOUNDS["q_dead"]
        p[d]["q_dead"] = min(max(p[d]["q_dead"], lo), hi)
    return p


def params_to_unit(p):
    """-> normalized [0,1] vector in param_keys() order."""
    x = []
    for d, kind, h in param_keys():
        lo, hi = PARAM_BOUNDS[kind]
        v = p[d][kind][h] if h is not None else p[d][kind]
        x.append((v - lo) / (hi - lo))
    return x


def unit_to_params(x):
    p = default_params()
    for (d, kind, h), xi in zip(param_keys(), x):
        lo, hi = PARAM_BOUNDS[kind]
        v = lo + min(max(xi, 0.0), 1.0) * (hi - lo)
        if h is None:
            p[d][kind] = v
        else:
            p[d][kind][h] = v
    return p


# ---------------------------------------------------------------- data model

class Data:
    """All measured inputs for the Stage-0 domain, loaded once.

    Attributes (plain dicts so tests can build tiny synthetic instances):
      manifest    demo_d net/manifest.json dict (station lists in travel
                  order, per-direction entry/exit, ramp alive flags)
      ml_rank     {station: corridor-wide Abs_PM rank within its direction}
      ramp_lanes  {station: lane count} for OR/FR stations
      obs_flow    {day: {(station, t_sim): veh/5min}}   ML stations
      obs_speed   {day: {(station, t_sim): mph}}        ML stations
      ramp        {day: {(col, t_sim): veh/5min or None}}  cols '716858_OR'
    """

    def __init__(self, manifest, ml_rank, ramp_lanes, obs_flow, obs_speed,
                 ramp):
        self.manifest = manifest
        self.ml_rank = ml_rank
        self.ramp_lanes = ramp_lanes
        self.obs_flow = obs_flow
        self.obs_speed = obs_speed
        self.ramp = ramp

    # -------- station sets (plan section 6)
    def domain_stations(self, d):
        """[(station, abs_pm, boundary_input)] in travel order."""
        return [(s["station"], s["abs_pm"], s["boundary_input"])
                for s in self.manifest[d]["stations"]]

    def holdout_stations(self, d):
        """Corridor-wide rank mod 5 == 2 (Abs_PM order), intersect domain."""
        return {st for st, _, _ in self.domain_stations(d)
                if self.ml_rank.get(st, -1) % 5 == 2}

    def interior_stations(self, d):
        return [st for st, _, bi in self.domain_stations(d) if not bi]

    def fit_stations(self, d):
        hold = self.holdout_stations(d)
        return [st for st in self.interior_stations(d) if st not in hold]

    @classmethod
    def load(cls, days=ALL_DAYS, data_dir=os.path.join(HERE, "data")):
        manifest = json.load(open(os.path.join(TWIN, "net", "manifest.json")))
        ml_rank, ramp_lanes = {}, {}
        rows = list(csv.DictReader(
            open(os.path.join(TWIN, "data", "ml_stations.csv"))))
        for d in "NS":
            lst = sorted([r for r in rows if r["dir"] == d],
                         key=lambda r: float(r["abs_pm"]))
            for i, r in enumerate(lst):
                ml_rank[r["station"]] = i
        for r in csv.DictReader(
                open(os.path.join(CALDIR, "ramp_inventory.csv"))):
            if r["type"] in ("OR", "FR"):
                ramp_lanes[r["station"]] = int(r["lanes"])
        obs_flow, obs_speed, ramp = {}, {}, {}
        for day in days:
            obs_flow[day] = _load_wide(
                os.path.join(data_dir, f"obs_ml_flow_{day}.csv"))
            obs_speed[day] = _load_wide(
                os.path.join(data_dir, f"obs_ml_speed_{day}.csv"))
            ramp[day] = _load_ramp(day)
        return cls(manifest, ml_rank, ramp_lanes, obs_flow, obs_speed, ramp)


def _t_of(ts):
    hh, mm = int(ts[11:13]), int(ts[14:16])
    return (hh - T0_H) * 3600 + (mm - T0_M) * 60


def _load_wide(path):
    out = {}
    for r in csv.DictReader(open(path)):
        t = _t_of(r["timestamp"])
        for k, v in r.items():
            if k != "timestamp" and v not in ("", None):
                out[(k, t)] = float(v)
    return out


def _load_ramp(day):
    out = {}
    for d in "NS":
        path = os.path.join(CALDIR, f"ramp_boundaries_{day}_{d}.csv")
        for r in csv.DictReader(open(path)):
            t = _t_of(r["timestamp"])
            for k, v in r.items():
                if k != "timestamp":
                    out[(k, t)] = float(v) if v not in ("", None) else None
    return out


# ------------------------------------------------------- boundary spec build

def _hourly(series_get, h):
    """Sum of the 12 5-min values of clock hour h; None if all missing."""
    base = hour_base(h)
    vals = [series_get(base + i * 300) for i in range(12)]
    vals = [v for v in vals if v is not None]
    return (sum(vals), len(vals)) if vals else (None, 0)


def _nearest_ml(data, d, abs_pm):
    sts = data.domain_stations(d)
    return min(sts, key=lambda s: abs(s[1] - abs_pm))[0]


def _screen_and_impute(data, day, d, station, rtype, abs_pm, ref_day, log):
    """Alive-ramp 5-min series for `day` after the zeros-implausibility
    screen; suspect hours replaced from ref_day (IMPUTED). Returns
    ({t: veh/5min or None}, imputed_hours)."""
    col = f"{station}_{rtype}"
    # extend to 04:00 so the hour-4 window (used for exit probabilities,
    # same convention as the twin) covers its full 12 bins
    series = {t: data.ramp[day].get((col, t))
              for t in range(hour_base(HOURS_ALL[0]), END, 300)}
    ml_st = _nearest_ml(data, d, abs_pm)
    imputed = []
    for h in HOURS_ALL:
        rsum, rn = _hourly(lambda t: data.ramp[day].get((col, t)), h)
        msum, mn = _hourly(lambda t: data.obs_flow[day].get((ml_st, t)), h)
        if rsum is None or msum is None or mn == 0:
            continue
        ml_rate = msum * 12.0 / mn
        if rsum == 0 and ml_rate >= SUSPECT_ML_THRESH:
            imputed.append(h)
            base = hour_base(h)
            for i in range(12):
                t = base + i * 300
                if t in series and ref_day is not None:
                    series[t] = data.ramp[ref_day].get((col, t))
            log.append(dict(day=day, dir=d, station=station, type=rtype,
                            hour=h, action=("imputed_from_" + ref_day)
                            if ref_day else "flagged_only",
                            reason=f"0 veh vs nearest ML {ml_st} "
                                   f"{ml_rate:.0f} veh/h"))
    return series, imputed


def _ref_day(day):
    """The OTHER fit day (imputation source). Holdout day: None (flag only,
    never modify holdout inputs)."""
    if day == "2026-06-02":
        return "2026-06-04"
    if day == "2026-06-04":
        return "2026-06-02"
    return None


def build_boundary_spec(day, params, data):
    """-> materialized, engine-agnostic boundary spec (dict).

    {"day", "screen_log": [...], "dirs": {d: {
        "entry_station", "entry_flow": {t: int veh/5min},
        "on_ramps": [{"station", "flow": {t: int}, "label"}],
        "exit_probs": [{"station", "hour", "p", "label"}]}}}
    """
    clamp_params(params)
    ref = _ref_day(day)
    spec = {"day": day, "screen_log": [], "dirs": {}}
    for d in "NS":
        man = data.manifest[d]
        pd = params[d]
        # ---- mainline entry (REAL x s_entry)
        entry = man["entry"]
        eflow = {}
        for t in BINS:
            v = data.obs_flow[day].get((entry, t))
            if v:
                n = int(round(v * pd["s_entry"][hour_of(t)]))
                if n > 0:
                    eflow[t] = n
        # ---- on-ramps
        alive_series = {}
        on_ramps = []
        for r in man["on_ramps"]:
            if not r["alive"]:
                continue
            series, imput = _screen_and_impute(
                data, day, d, r["station"], "OR", r["abs_pm"], ref,
                spec["screen_log"])
            alive_series[r["station"]] = series
        # mean normalized alive-OR profile (shape for dead ORs)
        shape = {}
        n_prof = 0
        for st, series in alive_series.items():
            vals = [series.get(t) or 0.0 for t in BINS]
            mean = sum(vals) / len(vals)
            if mean <= 0:
                continue
            n_prof += 1
            for t, v in zip(BINS, vals):
                shape[t] = shape.get(t, 0.0) + v / mean
        if n_prof:
            shape = {t: v / n_prof for t, v in shape.items()}
        for r in man["on_ramps"]:
            st = r["station"]
            if r["alive"]:
                flow = {}
                for t in BINS:
                    v = alive_series[st].get(t)
                    if v:
                        n = int(round(v * pd["s_or"][hour_of(t)]))
                        if n > 0:
                            flow[t] = n
                lab = "REAL(PeMS)xs_or"
                if any(l["station"] == st and l["day"] == day
                       for l in spec["screen_log"]):
                    lab += "+IMPUTED-hours"
            else:
                lanes = data.ramp_lanes.get(st, 1)
                flow = {}
                for t in BINS:
                    n = int(round(pd["q_dead"] * lanes
                                  * shape.get(t, 1.0) / 12.0))
                    if n > 0:
                        flow[t] = n
                lab = "IMPUTED(q_dead x mean alive-OR profile)"
            on_ramps.append(dict(station=st, flow=flow, label=lab))
        # ---- off-ramp exit probabilities
        stations_by_s = sorted(man["stations"], key=lambda r: r["s"])

        def upstream_ml(s):
            cand = [r for r in stations_by_s if r["s"] <= s]
            return cand[-1]["station"] if cand else stations_by_s[0]["station"]

        fr_series = {}
        for r in man["off_ramps"]:
            alive = r["alive"] and (day, r["station"]) not in SUSPECT_FR_DAYS
            if alive:
                series, _ = _screen_and_impute(
                    data, day, d, r["station"], "FR", r["abs_pm"], ref,
                    spec["screen_log"])
                fr_series[r["station"]] = series
        exit_probs = []
        for h in HOURS_ALL:
            alive_ps = []
            row_ps = {}
            for r in man["off_ramps"]:
                st = r["station"]
                if st not in fr_series:
                    continue
                fsum, _ = _hourly(lambda t: fr_series[st].get(t), h)
                msum, _ = _hourly(
                    lambda t: data.obs_flow[day].get((upstream_ml(r["s"]), t)),
                    h)
                p = 0.0 if not msum else min(max((fsum or 0.0) / msum, 0.0),
                                             P_BASE_CLAMP)
                row_ps[st] = (p, "REAL-derived")
                alive_ps.append(p)
            mean_p = sum(alive_ps) / len(alive_ps) if alive_ps else 0.0
            for r in man["off_ramps"]:
                st = r["station"]
                if st not in row_ps:
                    lab = ("IMPUTED(dir-hour mean; suspect day)"
                           if (day, st) in SUSPECT_FR_DAYS
                           else "IMPUTED(dir-hour mean)")
                    row_ps[st] = (mean_p, lab)
                p0, lab = row_ps[st]
                p = min(max(p0 * pd["m_off"][h], 0.0), P_MAX)
                exit_probs.append(dict(station=st, hour=h, p=p,
                                       label=lab + "xm_off"))
        spec["dirs"][d] = dict(entry_station=entry, entry_flow=eflow,
                               on_ramps=on_ramps, exit_probs=exit_probs)
    return spec


def spec_totals(spec):
    out = {}
    for d, sd in spec["dirs"].items():
        out[d] = dict(
            entry_veh=sum(sd["entry_flow"].values()),
            or_veh=sum(sum(r["flow"].values()) for r in sd["on_ramps"]))
    return out


# ------------------------------------------------------------------ objective

class SimResult:
    """Engine-agnostic simulation output.

    flow  {(station, t_sim): veh/5min} at ML stations, 5-min bins
    speed {(station, t_sim): mph}      (absent key = no vehicle passed)
    stats {"loaded", "inserted", "teleports", "wall_s", ...}
    """

    def __init__(self, flow, speed, stats=None):
        self.flow = flow
        self.speed = speed
        self.stats = stats or {}


def station_metrics(sim, obs_flow, obs_speed, station,
                    hours=HOURS_OBJ, bins=OBJ_BINS):
    """Per-station hourly GEH triples and speed errors (pure, testable)."""
    gehs = {}
    for h in hours:
        base = hour_base(h)
        m = sum(sim.flow.get((station, base + i * 300), 0) for i in range(12))
        c = sum(obs_flow.get((station, base + i * 300), 0.0)
                for i in range(12))
        gehs[h] = (geh(m, c), m, c)
    errs, n_empty = [], 0
    for t in bins:
        so = obs_speed.get((station, t))
        sm = sim.speed.get((station, t))
        if so is None:
            continue
        if sm is None:
            n_empty += 1
            continue
        errs.append(sm - so)
    return gehs, errs, n_empty


def objective(sim, obs_flow, obs_speed, fit_by_dir, holdout_by_dir,
              lam=LAMBDA_SPEED):
    """Weighted GEH(flow) + lam*RMSE(speed) over FIT interior stations.

    fit_by_dir / holdout_by_dir: {"N": [station,...], "S": [...]}.
    Holdout stations are evaluated for REPORTING ONLY -- they contribute
    nothing to any objective value the optimizer sees.
    Returns dict with "fit", "holdout", per-dir breakdowns and per-station
    rows.
    """
    out = {"per_station": []}
    for setname, groups in (("fit", fit_by_dir), ("holdout", holdout_by_dir)):
        all_g, all_e = [], []
        per_dir = {}
        for d, stations in groups.items():
            gs, errs = [], []
            for st in stations:
                gehs, e, n_empty = station_metrics(sim, obs_flow, obs_speed,
                                                   st)
                gs += [g for g, _, _ in gehs.values()]
                errs += e
                out["per_station"].append(dict(
                    dir=d, station=st, set=setname, gehs=gehs,
                    mean_geh=sum(g for g, _, _ in gehs.values()) / len(gehs),
                    speed_rmse=math.sqrt(sum(x * x for x in e) / len(e))
                    if e else None,
                    speed_bins_empty=n_empty))
            per_dir[d] = _summ(gs, errs, lam)
            all_g += gs
            all_e += errs
        s = _summ(all_g, all_e, lam)
        s["per_dir"] = per_dir
        out[setname] = s
    return out


def _summ(gs, errs, lam):
    if not gs:
        return dict(mean_geh=None, median_geh=None, cov_geh5=None,
                    speed_rmse=None, combined=None, n_station_hours=0)
    mean_geh = sum(gs) / len(gs)
    srt = sorted(gs)
    n = len(srt)
    median = srt[n // 2] if n % 2 else 0.5 * (srt[n // 2 - 1] + srt[n // 2])
    cov = 100.0 * sum(1 for g in gs if g < 5) / n
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs)) if errs else 0.0
    return dict(mean_geh=mean_geh, median_geh=median, cov_geh5=cov,
                speed_rmse=rmse, combined=mean_geh + lam * rmse,
                n_station_hours=n)


# ------------------------------------------------------------- deliverables

def write_boundary_files(spec, outdir):
    """Persist a materialized spec as the versioned boundary files that seed
    LPSim on GPU day (long format, one label per row)."""
    day = spec["day"]
    bpath = os.path.join(outdir, f"calibrated_boundaries_{day}.csv")
    with open(bpath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "dir", "element", "station", "t_sim", "clock",
                    "veh_5min", "label"])
        for d, sd in spec["dirs"].items():
            for t in BINS:
                n = sd["entry_flow"].get(t)
                if n:
                    w.writerow([day, d, "ml_entry", sd["entry_station"], t,
                                clock(t), n, "REAL(PeMS)xs_entry"])
            for r in sd["on_ramps"]:
                for t in BINS:
                    n = r["flow"].get(t)
                    if n:
                        w.writerow([day, d, "on_ramp", r["station"], t,
                                    clock(t), n, r["label"]])
    ppath = os.path.join(outdir, f"calibrated_exit_probs_{day}.csv")
    with open(ppath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "dir", "fr_station", "hour", "p_exit", "label"])
        for d, sd in spec["dirs"].items():
            for r in sd["exit_probs"]:
                w.writerow([day, d, r["station"], r["hour"],
                            f"{r['p']:.4f}", r["label"]])
    spath = os.path.join(outdir, f"screen_log_{day}.csv")
    with open(spath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "dir", "station", "type", "hour", "action",
                    "reason"])
        for l in spec["screen_log"]:
            w.writerow([l["day"], l["dir"], l["station"], l["type"],
                        l["hour"], l["action"], l["reason"]])
    return bpath, ppath
