"""Locate upstream assets (the I-710 twin + Stage-0 harness) and make the
proven calibration machinery importable without copying it.

CorridorBench v0.1 reuses, unmodified:
  - demos/demo_d_sumo_twin/       the SUMO network + frozen baseline
  - la_network/calibration/stage0 harness.py (boundary specs, Data loader)
                                  and sumo_adapter.py (jtrrouter+sumo)
Provenance for both lives in their own PROVENANCE.md files. Nothing in
CorridorBench edits upstream files.
"""
import os
import sys

HOME = os.path.expanduser("~")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_UPSTREAM = os.path.join(HOME, "Downloads", "RoamingSim-Amptrans")
_VENDORED = os.path.join(REPO, "data", "vendored")
# Prefer the original upstream tree when present (developer machine);
# fall back to the vendored mirror shipped in this repository
# (data/vendored/, sha256 manifest in PROVENANCE.json) so the
# benchmark is self-contained for everyone else.
RS = _UPSTREAM if os.path.exists(
    os.path.join(_UPSTREAM, "demos", "demo_d_sumo_twin", "net",
                 "i710_twin.net.xml")) else _VENDORED
TWIN = os.path.join(RS, "demos", "demo_d_sumo_twin")
STAGE0 = os.path.join(RS, "la_network", "calibration", "stage0")
# SUMO binaries: the demo venv when present, else whatever is on PATH
# (pip install eclipse-sumo provides sumo/jtrrouter)
_DEMO_BIN = os.path.join(_UPSTREAM, "demos", "demo_d_sumo_twin",
                         ".venv", "bin")
SUMO_BIN = _DEMO_BIN if os.path.exists(os.path.join(_DEMO_BIN, "sumo"))     else ""
RESULTS = os.path.join(REPO, "results")
EPISODES = os.path.join(REPO, "episodes")


def import_stage0():
    """Import (harness, sumo_adapter) from the upstream Stage-0 directory."""
    if STAGE0 not in sys.path:
        sys.path.insert(0, STAGE0)
    import harness            # noqa: E402
    import sumo_adapter       # noqa: E402
    return harness, sumo_adapter


def check_assets():
    missing = [p for p in
               [TWIN, STAGE0,
                os.path.join(TWIN, "net", "i710_twin.net.xml"),
                os.path.join(STAGE0, "data", "obs_ml_flow_2026-06-03.csv"),
                os.path.join(SUMO_BIN, "sumo")]
               if not os.path.exists(p)]
    return missing
