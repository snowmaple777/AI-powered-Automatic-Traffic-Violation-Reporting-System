"""規則層快速評估：直接以全片評估的 JSONL 重算違規規則（不需影片或模型），計算指標。

  python tools/rule_eval.py --source outputs/eval/baseline --label rules_v1
輸出 outputs/eval/<label>/<影片>/<影片>_violations.jsonl 與摘要，並與來源事件比較差異。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from violation_engine import RuleContext, ViolationEngine, create_rules  # noqa: E402
from double_lines import double_line_rule_options  # noqa: E402
from area_rules import area_rule_options  # noqa: E402


def rule_options():
    options = {"red_light_stop_line_crossing": {"crossing_direction": "away"}}
    options.update(area_rule_options(ROOT / "configs/area_rules.json"))
    options.update(double_line_rule_options(ROOT / "configs/double_line_rules.json"))
    return options


def git_state():
    """目前的 commit 與是否有未提交的修改，讓每次實驗都能對回程式版本。"""
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"commit": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


def run_rules(jsonl, out_dir, name, rules=("all",)):
    out_dir.mkdir(parents=True, exist_ok=True)
    events, engine = [], None
    with jsonl.open(encoding="utf-8") as source, \
         (out_dir / f"{name}_rule_diagnostics.jsonl").open("w", encoding="utf-8") as diagnostics:
        for line in source:
            observation = json.loads(line)
            if engine is None:
                frame = observation["frame"]
                engine = ViolationEngine(create_rules(list(rules), RuleContext(name, frame["fps"], frame["width"], frame["height"]), rule_options()))
            events += engine.evaluate(observation)
            diagnostics.write(json.dumps({"frame": observation["frame"]["index"],
                                          "rules": {r.rule_id: getattr(r, "diagnostics", {}) for r in engine.rules}},
                                         ensure_ascii=False) + "\n")
    events += engine.finalize()
    with (out_dir / f"{name}_violations.jsonl").open("w", encoding="utf-8") as target:
        for event in events:
            target.write(json.dumps(event, ensure_ascii=False) + "\n")
    return events


def brief(events):
    return [(e["rule_id"], e["status"], round(e["timestamp_sec"], 2), e.get("object_id")) for e in events]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=ROOT / "outputs/eval/baseline")
    parser.add_argument("--label", required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args(argv)
    out = ROOT / "outputs/eval" / args.label
    out.mkdir(parents=True, exist_ok=True)
    (out / "rule_options.json").write_text(json.dumps(
        {"source": str(args.source), "only": args.only, "git": git_state(), "options": rule_options()},
        ensure_ascii=False, indent=2), encoding="utf-8")
    changed = []
    for folder in sorted(p for p in args.source.iterdir() if (p / f"{p.name}_detections.jsonl").is_file()):
        name = folder.name
        if args.only and name not in args.only:
            continue
        events = run_rules(folder / f"{name}_detections.jsonl", out / name, name)
        before = [json.loads(l) for l in (folder / f"{name}_violations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        if brief(before) != brief(events):
            changed.append((name, brief(before), brief(events)))
    sys.path.insert(0, str(ROOT / "tools"))
    from batch_eval import summarize
    print(summarize(args.label))
    print(f"\n與來源（{args.source.name}）事件不同的影片：{len(changed)}")
    for name, before, after in changed:
        print(f"  {name}\n    前：{before}\n    後：{after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
