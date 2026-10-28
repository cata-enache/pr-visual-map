#!/usr/bin/env python3
"""Regression helper for rule-pack or generator changes.

  regress.py save   [--out-root DIR]   snapshot every map's graph.json as the baseline
  regress.py check  [--out-root DIR]   re-run every map with the skill's generator and list what changed

A map is any folder under --out-root (default $PR_VISUAL_MAP_OUT, else ~/pr-maps) with a graph.json.
Compared per block: label, kind, status, files, rule flags, must-read, review step; plus links by source.
Every difference should be intended; explain it before keeping a pack change.
"""
import argparse
import os
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = Path.home() / ".cache" / "pr-visual-map" / "baseline"


def facts(g: dict):
    nodes = {n["id"]: {"label": n["label"], "kind": n["kind"], "status": n["status"],
                       "files": sorted(f["path"] for f in n["files"]),
                       "rule flags": sorted((r["type"], r["severity"]) for r in n["risk"] if r.get("source") == "rule"),
                       "must-read": n["mustRead"], "step": n["reviewOrder"]}
             for n in g["nodes"] if n["kind"] != "group"}
    return nodes, Counter(e["source"] for e in g["edges"])


def maps(root: Path):
    return sorted(p.parent for p in root.glob("*/graph.json"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["save", "check"])
    ap.add_argument("--out-root", default=os.environ.get("PR_VISUAL_MAP_OUT", str(Path.home() / "pr-maps")))
    a = ap.parse_args()
    root = Path(a.out_root).expanduser()
    BASE.mkdir(parents=True, exist_ok=True)
    if a.mode == "save":
        for m in maps(root):
            (BASE / f"{m.name}.json").write_text((m / "graph.json").read_text(encoding="utf-8"), encoding="utf-8")
        print(f"saved {len(maps(root))} baseline(s) to {BASE}")
        return
    changed = 0
    for m in maps(root):
        g = json.loads((m / "graph.json").read_text(encoding="utf-8"))["meta"]
        cmd = [sys.executable, str(HERE / "make_map.py"), "--out", str(m)]
        cmd += ["--repo", g["repo"], "--pr", str(g["pr"])] if g.get("pr") else []
        if not g.get("pr"):
            print(f"== {m.name}: local-branch map, re-run it by hand (see its README)")
            continue
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0 or not (BASE / f"{m.name}.json").exists():
            print(f"== {m.name}: {'FAILED' if p.returncode else 'no baseline'}\n{p.stderr.strip()[-600:]}")
            changed += p.returncode != 0
            continue
        (bn, be), (nn, ne) = (facts(json.loads((BASE / f"{m.name}.json").read_text(encoding="utf-8"))),
                              facts(json.loads((m / "graph.json").read_text(encoding="utf-8"))))
        out = [f"  + block {i}" for i in sorted(set(nn) - set(bn))] + [f"  - block {i}" for i in sorted(set(bn) - set(nn))]
        out += [f"  {i} {k}: {bn[i][k]} -> {nn[i][k]}" for i in sorted(set(bn) & set(nn)) for k in bn[i] if bn[i][k] != nn[i][k]]
        if be != ne:
            out.append(f"  links: {dict(be)} -> {dict(ne)}")
        changed += bool(out)
        print(f"== {m.name}: {'no change' if not out else f'{len(out)} difference(s)'}")
        print("\n".join(out[:30]))
    sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
