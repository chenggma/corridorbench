#!/usr/bin/env python3
"""Generate docs/viewer.html: a single-file static corridor viewer
(ARC-2019 convention: the whole front-end is one HTML file).

Shows, per direction: the station ladder (Abs_PM order) with per-hour
GEH cells for whichever candidates have campaign runs, plus obs vs sim
hourly flow for a selectable station. All data inlined at build time --
no server, no external assets.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from corridorbench import campaign, metrics, paths, scoring, taskset  # noqa

DAY = "2026-06-03"
CANDS = ["identity", "scale_0p70", "scale_0p85", "scale_1p15",
         "scale_1p30", "prop0"]


def main():
    tasks, data = taskset.load_tasks()
    payload = {"day": DAY, "dirs": {}}
    for d in "NS":
        t = next(x for x in tasks if x.direction == d
                 and x.holdout_day == DAY)
        stations = [(st, pm) for st, pm, bi in data.domain_stations(d)
                    if not bi]
        rows = []
        for tag in CANDS:
            rd = os.path.join(paths.RESULTS, "campaign", f"{tag}__{DAY}")
            if not os.path.exists(os.path.join(rd, "flow.csv")):
                continue
            sf, ss = campaign.load_run(rd)
            g = metrics.station_hour_geh(sf, data.obs_flow[DAY],
                                         [s for s, _ in stations])
            cells = {f"{k[0]}_h{k[1]}": round(v, 1) for k, v in g.items()}
            cov, n = metrics.coverage(g)
            rows.append({"tag": tag, "cov": round(cov * 100, 1),
                         "cells": cells})
        obs_hourly = {}
        for st, _ in stations:
            obs_hourly[st] = [metrics.hourly_flows(
                lambda tt, s=st: data.obs_flow[DAY].get((s, tt)), h)[0]
                for h in metrics.HOURS_OBJ]
        payload["dirs"][d] = {
            "stations": [{"id": s, "abs_pm": pm,
                          "holdout": s in t.holdout_stations}
                         for s, pm in stations],
            "candidates": rows, "obs_hourly": obs_hourly,
            "hours": metrics.HOURS_OBJ}
    html = TEMPLATE.replace("__DATA__", json.dumps(payload))
    out = os.path.join(paths.REPO, "docs", "viewer.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write(html)
    print(out, len(html), "bytes")


TEMPLATE = """<!doctype html><meta charset="utf-8">
<title>CorridorBench I-710 viewer</title>
<style>
body{font:14px/1.4 -apple-system,system-ui,sans-serif;margin:24px;
     color:#111;background:#fafafa}
h1{font-size:20px} h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse;margin:8px 0}
td,th{border:1px solid #ccc;padding:3px 7px;text-align:right;
      font-variant-numeric:tabular-nums}
th{background:#eee} .hold{outline:2px dashed #7a5}
.g0{background:#c6e6c6}.g1{background:#f5eec0}.g2{background:#f3cfa8}
.g3{background:#eda9a0}.tag{font-weight:600;text-align:left}
small{color:#666}
</style>
<h1>CorridorBench &mdash; I-710 mid-corridor, holdout day <span id=day></span></h1>
<p><small>Cell = GEH of hourly flow vs PeMS (green &lt;5, yellow &lt;10,
orange &lt;15, red &ge;15). Dashed outline = sealed holdout station.
Coverage = share of station-hours GEH&lt;5 (interior). Practice target:
&ge;85% (FHWA Toolbox Vol III).</small></p>
<div id=root></div>
<script>
const D=__DATA__;
document.getElementById('day').textContent=D.day;
const root=document.getElementById('root');
for(const dir of ['N','S']){
 const dd=D.dirs[dir];
 const h2=document.createElement('h2');
 h2.textContent=`I-710 ${dir}-bound (${dd.candidates.length} candidates)`;
 root.appendChild(h2);
 for(const c of dd.candidates){
  const t=document.createElement('table');
  let hd='<tr><th class=tag>'+c.tag+' &mdash; cov '+c.cov+'%</th>';
  for(const h of dd.hours) hd+='<th>h'+String(h).padStart(2,'0')+'</th>';
  hd+='</tr>';
  let body='';
  for(const s of dd.stations){
   body+='<tr><td class="tag'+(s.holdout?' hold':'')+'">'+s.id+
        ' <small>pm '+s.abs_pm+'</small></td>';
   for(const h of dd.hours){
    const v=c.cells[s.id+'_h'+h];
    const cls=v==null?'':(v<5?'g0':v<10?'g1':v<15?'g2':'g3');
    body+='<td class="'+cls+'">'+(v==null?'&ndash;':v.toFixed(1))+'</td>';
   }
   body+='</tr>';
  }
  t.innerHTML=hd+body; root.appendChild(t);
 }
}
</script>
"""

if __name__ == "__main__":
    main()
