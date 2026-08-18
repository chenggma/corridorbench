"""Scoring metrics. Stdlib only.

GEH (Geoffrey E. Havers, UK 1970s): GEH = sqrt(2(M-C)^2 / (M+C)) with M =
modeled hourly flow (veh/h) and C = observed hourly flow (veh/h). The
FHWA Traffic Analysis Toolbox Vol III (FHWA-HRT-04-040, 2004, Table 4,
inherited from WisDOT 2002 <- UK DMRB 1996) acceptance targets encoded in
criteria() below. Cross-checked against Eclipse SUMO's implementation
(tools/sumolib/statistics.py: geh) -- same formula.

WisDOT TEOpS 16-20 (2019) replaces GEH with RNSE on an asymmetry argument.
RNSE is NOT implemented here yet: the primary-source formula has not been
transcribed from the TEOpS PDF in this session, and we do not encode
formulas we have not verified. Placeholder raises NotImplementedError.
"""
import math

HOURS_OBJ = list(range(5, 11))          # scored clock hours 05..10


def geh(m, c):
    """GEH statistic on hourly flows (veh/h)."""
    if m < 0 or c < 0:
        raise ValueError("negative flow")
    s = m + c
    if s == 0:
        return 0.0
    return math.sqrt(2.0 * (m - c) ** 2 / s)


def rnse(*_args, **_kw):
    """WisDOT TEOpS 16-20 RNSE -- pending primary-source transcription."""
    raise NotImplementedError(
        "RNSE formula not yet transcribed from WisDOT TEOpS 16-20; "
        "refusing to guess.")


def hourly_flows(series_get, hour, t0_offset_s=1800):
    """Sum 12 five-minute values for clock hour `hour`.

    series_get(t_sim) -> veh/5min or None. Sim t=0 is 04:30, so clock hour
    h starts at (h-4)*3600-1800 sim seconds. Returns (veh_per_hour, n_bins)
    or (None, 0) when every bin is missing.
    """
    base = (hour - 4) * 3600 - t0_offset_s
    vals = [series_get(base + i * 300) for i in range(12)]
    vals = [v for v in vals if v is not None]
    return (sum(vals), len(vals)) if vals else (None, 0)


def station_hour_geh(sim_flow, obs_flow, stations, hours=HOURS_OBJ):
    """Per (station, hour) GEH on hourly flows.

    sim_flow/obs_flow: {(station, t_sim): veh/5min}. Missing obs bins are
    skipped on both sides symmetrically (both series summed over the SAME
    observed bins) so partial hours compare like-for-like.
    Returns {(station, hour): geh_value}; hours with zero observed bins are
    absent from the result.
    """
    out = {}
    for st in stations:
        for h in hours:
            base = (h - 4) * 3600 - 1800
            m = c = 0.0
            n = 0
            for i in range(12):
                t = base + i * 300
                o = obs_flow.get((st, t))
                if o is None:
                    continue
                m += sim_flow.get((st, t), 0.0)
                c += o
                n += 1
            if n == 0:
                continue
            # scale partial hours to veh/h like-for-like
            scale = 12.0 / n
            out[(st, h)] = geh(m * scale, c * scale)
    return out


def coverage(geh_by_sh, threshold=5.0):
    """(share_below_threshold, n_station_hours). Empty input -> (None, 0)."""
    if not geh_by_sh:
        return None, 0
    n = len(geh_by_sh)
    k = sum(1 for v in geh_by_sh.values() if v < threshold)
    return k / n, n


def speed_rmse(sim_speed, obs_speed, stations, hours=HOURS_OBJ):
    """RMSE (mph) over 5-min bins in the scored window, station-aggregated.

    Only bins where BOTH sides report a speed enter; returns
    (rmse_mph, bias_mph, n_bins) or (None, None, 0).
    """
    se = []
    err = []
    for st in stations:
        for h in hours:
            base = (h - 4) * 3600 - 1800
            for i in range(12):
                t = base + i * 300
                o = obs_speed.get((st, t))
                s = sim_speed.get((st, t))
                if o is None or s is None:
                    continue
                se.append((s - o) ** 2)
                err.append(s - o)
    if not se:
        return None, None, 0
    n = len(se)
    return math.sqrt(sum(se) / n), sum(err) / n, n


def fhwa_2004_criteria(geh_by_sh, sim_flow, obs_flow, stations,
                       hours=HOURS_OBJ):
    """FHWA Toolbox Vol III (2004) Table 4 checks that apply to link flows.

    Encoded verbatim from the table (hourly flows):
      A. GEH < 5 for individual link flows in > 85% of cases
      B. Individual flows within 15% for 700 < flow < 2700 veh/h, > 85%
      C. Within 100 veh/h for flow < 700, > 85%
      D. Within 400 veh/h for flow > 2700, > 85%
      E. Sum of all link flows within 5% of sum of counts
      F. GEH < 4 for the sum of all link flows
    Returns a dict of named results; each is (pass_bool, value).
    """
    hourly = {}
    for st in stations:
        for h in hours:
            base = (h - 4) * 3600 - 1800
            m = c = 0.0
            n = 0
            for i in range(12):
                t = base + i * 300
                o = obs_flow.get((st, t))
                if o is None:
                    continue
                m += sim_flow.get((st, t), 0.0)
                c += o
                n += 1
            if n:
                hourly[(st, h)] = (m * 12.0 / n, c * 12.0 / n)
    out = {}
    cov, n = coverage(geh_by_sh)
    out["A_geh5_85pct"] = (cov is not None and cov > 0.85, cov)
    mids = [(m, c) for m, c in hourly.values() if 700 < c < 2700]
    lows = [(m, c) for m, c in hourly.values() if c < 700]
    highs = [(m, c) for m, c in hourly.values() if c > 2700]
    def _share(pairs, ok):
        if not pairs:
            return None
        return sum(1 for m, c in pairs if ok(m, c)) / len(pairs)
    s = _share(mids, lambda m, c: abs(m - c) / c <= 0.15)
    out["B_within15pct_mid"] = (s is not None and s > 0.85, s)
    s = _share(lows, lambda m, c: abs(m - c) <= 100)
    out["C_within100_low"] = (s is not None and s > 0.85, s)
    s = _share(highs, lambda m, c: abs(m - c) <= 400)
    out["D_within400_high"] = (s is not None and s > 0.85, s)
    M = sum(m for m, _ in hourly.values())
    C = sum(c for _, c in hourly.values())
    out["E_sum_within5pct"] = (C > 0 and abs(M - C) / C <= 0.05,
                               (M - C) / C if C else None)
    out["F_sum_geh4"] = (geh(M, C) < 4 if C or M else True, geh(M, C))
    return out
