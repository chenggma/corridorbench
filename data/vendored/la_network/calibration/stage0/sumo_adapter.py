#!/usr/bin/env python3
"""SUMO adapter for the Stage-0 calibration harness.

Runs the UNCHANGED demo_d_sumo_twin network (net/i710_twin.net.xml,
Eclipse SUMO 1.27.1 from demos/demo_d_sumo_twin/.venv -- install provenance
in demos/demo_d_sumo_twin/PROVENANCE.md) with demand materialized from an
engine-agnostic boundary spec (harness.build_boundary_spec output).

Determinism: jtrrouter and sumo are run WITHOUT --seed, i.e. on their fixed
built-in default seed (23423) -- the same configuration as the frozen
demo_d baseline run, so identical inputs reproduce identical outputs, and
the identity-parameter spec for 2026-06-03 reproduces the frozen baseline
exactly. Flow XML entries, ids, attributes and turn-file ordering replicate
demo_d/gen_demand.py so that the routing input stream is bit-identical for
the identity case.

Vehicle model: SUMO defaults (Krauss, default passenger car, default
speedFactor) -- Stage 0 leaves capacity/speed parameters at network
defaults per the plan (section 5).
"""
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from harness import END, HOURS_ALL, SimResult, hour_base

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
TWIN = os.path.join(REPO, "demos", "demo_d_sumo_twin")
MPH = 2.23694


class SumoAdapter:
    def __init__(self, twin_dir=TWIN):
        self.twin = twin_dir
        self.net = os.path.join(twin_dir, "net", "i710_twin.net.xml")
        _venv = os.path.join(twin_dir, ".venv", "bin")
        # vendored copy: fall back to binaries on PATH (pip eclipse-sumo)
        self.venv = _venv if os.path.exists(os.path.join(_venv, "sumo")) \
            else ""
        import json
        self.man = json.load(open(os.path.join(twin_dir, "net",
                                               "manifest.json")))
        # station -> engine edge lookups (SUMO-specific; kept out of specs)
        self.or_edge = {d: {r["station"]: r["edge"]
                            for r in self.man[d]["on_ramps"]} for d in "NS"}
        self.fr_edges = {d: {r["station"]: (r["from_edge"], r["edge"],
                                            r["through_edge"])
                             for r in self.man[d]["off_ramps"]} for d in "NS"}

    # ------------------------------------------------------------ demand IO
    def _write_flows(self, spec, d, path):
        sd = spec["dirs"][d]
        man = self.man[d]
        flows = []
        for t in sorted(sd["entry_flow"]):
            flows.append((f"{d}_entry_{t}", man["source_edge"], t,
                          sd["entry_flow"][t]))
        for r in sd["on_ramps"]:
            edge = self.or_edge[d][r["station"]]
            for t in sorted(r["flow"]):
                flows.append((f"{d}_on_{r['station']}_{t}", edge, t,
                              r["flow"][t]))
        flows.sort(key=lambda x: x[2])  # jtrrouter wants departure-time order
        with open(path, "w") as f:
            f.write("<routes>\n")
            for fid, edge, b, n in flows:
                f.write(f'  <flow id="{fid}" from="{edge}" begin="{b}" '
                        f'end="{b + 300}" number="{n}" departLane="best" '
                        f'departSpeed="max" departPos="base"/>\n')
            f.write("</routes>\n")

    def _write_turns(self, spec, d, path):
        sd = spec["dirs"][d]
        p_of = {(r["station"], r["hour"]): r["p"] for r in sd["exit_probs"]}
        with open(path, "w") as f:
            f.write("<turns>\n")
            for h in HOURS_ALL:
                b = max(0, hour_base(h))
                e = min(END, hour_base(h + 1))
                f.write(f'  <interval begin="{b}" end="{e}">\n')
                for r in self.man[d]["off_ramps"]:
                    frm, ramp, thru = self.fr_edges[d][r["station"]]
                    p = p_of[(r["station"], h)]
                    f.write(f'    <edgeRelation from="{frm}" to="{ramp}" '
                            f'probability="{p:.4f}"/>\n')
                    f.write(f'    <edgeRelation from="{frm}" to="{thru}" '
                            f'probability="{1 - p:.4f}"/>\n')
                f.write("  </interval>\n")
            f.write("</turns>\n")

    def _write_detectors(self, workdir):
        """Copy the twin's e1 definitions, redirect output into workdir."""
        src = open(os.path.join(self.twin, "net", "detectors.add.xml")).read()
        out = os.path.join(workdir, "det.out.xml").replace("\\", "/")
        src = src.replace("../results/detectors.out.xml", out)
        path = os.path.join(workdir, "det.add.xml")
        with open(path, "w") as f:
            f.write(src)
        return path, out

    # ------------------------------------------------------------- run/parse
    def run(self, spec, workdir, keep_outputs=False):
        os.makedirs(workdir, exist_ok=True)
        t0 = time.time()
        routes = []
        for d in "NS":
            fpath = os.path.join(workdir, f"flows_{d}.xml")
            tpath = os.path.join(workdir, f"turns_{d}.xml")
            rpath = os.path.join(workdir, f"routes_{d}.rou.xml")
            self._write_flows(spec, d, fpath)
            self._write_turns(spec, d, tpath)
            sinks = [self.man[d]["sink_edge"]] + \
                [r["edge"] for r in self.man[d]["off_ramps"]]
            subprocess.run(
                [os.path.join(self.venv, "jtrrouter"),
                 "--net-file", self.net, "--route-files", fpath,
                 "--turn-ratio-files", tpath,
                 "--sink-edges", ",".join(sinks),
                 "--begin", "0", "--end", str(END),
                 "--output-file", rpath, "--no-warnings", "true"],
                check=True, capture_output=True)
            routes.append(rpath)
        dpath, dout = self._write_detectors(workdir)
        subprocess.run(
            [os.path.join(self.venv, "sumo"), "-n", self.net,
             "-r", ",".join(routes), "-a", dpath,
             "--begin", "0", "--end", str(END),
             "--statistic-output", os.path.join(workdir, "stats.xml"),
             "--log", os.path.join(workdir, "sumo.log"),
             "--no-step-log"],
            check=True, cwd=workdir, capture_output=True)
        flow, speed = self._parse_detectors(dout)
        stats = self._parse_stats(os.path.join(workdir, "stats.xml"))
        stats["wall_s"] = round(time.time() - t0, 1)
        if not keep_outputs:  # routes+detector XML are big; drop after parse
            for p in routes + [dout]:
                if os.path.exists(p):
                    os.remove(p)
        return SimResult(flow, speed, stats)

    @staticmethod
    def _parse_detectors(path):
        flow, num, den = {}, {}, {}
        for _, el in ET.iterparse(path):
            if el.tag != "interval":
                continue
            m = re.match(r"det_(\d+)_(\d+)", el.get("id"))
            st = m.group(1)
            t = int(float(el.get("begin")))
            n = int(el.get("nVehContrib"))
            v = float(el.get("speed"))
            flow[(st, t)] = flow.get((st, t), 0) + n
            if n > 0 and v >= 0:
                num[(st, t)] = num.get((st, t), 0.0) + n * v * MPH
                den[(st, t)] = den.get((st, t), 0) + n
            el.clear()
        speed = {k: num[k] / den[k] for k in num}
        return flow, speed

    @staticmethod
    def _parse_stats(path):
        root = ET.parse(path).getroot()
        veh = root.find("vehicles")
        tel = root.find("teleports")
        return dict(loaded=int(veh.get("loaded")),
                    inserted=int(veh.get("inserted")),
                    running=int(veh.get("running")),
                    waiting=int(veh.get("waiting")),
                    teleports=int(tel.get("total")))
