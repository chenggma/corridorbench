"""Seeded SUMO adapter: identical to the upstream Stage-0 adapter but
passes an explicit --seed to jtrrouter and sumo, for measuring
run-to-run score variability (a submission's score is otherwise a
single fixed-seed realization)."""
import os
import subprocess
import time

from . import paths

H, SA = paths.import_stage0()


class SeededAdapter(SA.SumoAdapter):
    def __init__(self, seed, twin_dir=None):
        super().__init__(**({"twin_dir": twin_dir} if twin_dir else {}))
        self.seed = int(seed)

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
                 "--seed", str(self.seed),
                 "--begin", "0", "--end", str(SA.END),
                 "--output-file", rpath, "--no-warnings", "true"],
                check=True, capture_output=True)
            routes.append(rpath)
        dpath, dout = self._write_detectors(workdir)
        subprocess.run(
            [os.path.join(self.venv, "sumo"), "-n", self.net,
             "-r", ",".join(routes), "-a", dpath,
             "--seed", str(self.seed),
             "--begin", "0", "--end", str(SA.END),
             "--statistic-output", os.path.join(workdir, "stats.xml"),
             "--log", os.path.join(workdir, "sumo.log"),
             "--no-step-log"],
            check=True, cwd=workdir, capture_output=True)
        flow, speed = self._parse_detectors(dout)
        stats = self._parse_stats(os.path.join(workdir, "stats.xml"))
        stats["wall_s"] = round(time.time() - t0, 1)
        stats["seed"] = self.seed
        if not keep_outputs:
            for p in routes + [dout]:
                if os.path.exists(p):
                    os.remove(p)
        return SA.SimResult(flow, speed, stats)
