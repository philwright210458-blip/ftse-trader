"""Regenerate checked-in fictional examples using the actual Python engine.

No network, operational database or schedule is used. Node-only preview users
can inspect these frozen outputs without installing a second runtime.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ftse_research import ResearchConfig, ResearchEngine, ResearchStore
from ftse_research.replay import build_demo_replay
from ftse_research_cli import write_json


def main():
    bundle = build_demo_replay()
    with ResearchStore(":memory:") as store:
        engine = ResearchEngine(store, ResearchConfig(mode="synthetic-demo"))
        snapshot = engine.seed_demo("2026-10-16T15:46:00+00:00")
    write_json(ROOT / "dashboard/public/replay.json", bundle)
    write_json(ROOT / "dashboard/public/demo.json", {"revision": "DEMONSTRATION", "snapshot": snapshot})
    print(
        f"Generated fictional engine examples: {len(bundle['scenarios'])} scenarios, "
        f"{sum(len(item['frames']) for item in bundle['scenarios'])} replay frames; no live data or ledger changes."
    )


if __name__ == "__main__":
    main()
