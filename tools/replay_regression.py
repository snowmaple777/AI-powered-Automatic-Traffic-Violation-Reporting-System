"""以既有 *_detections.jsonl 重跑全部違規規則，並與基準摘要比對。

不載入任何模型（不需要 torch），用於確認重構沒有意外改變規則行為。
  建立基準：python tools/replay_regression.py --save outputs/refactor_baseline/replay.json
  比對基準：python tools/replay_regression.py --compare outputs/refactor_baseline/replay.json
有差異時結束代碼為 1；刻意修改規則後，可用 --save 更新基準。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from violation_engine import RuleContext, ViolationEngine, create_rules  # noqa: E402
from double_lines import double_line_rule_options  # noqa: E402
from area_rules import area_rule_options  # noqa: E402

DEFAULT_GLOBS = ("outputs/*_detections.jsonl",
                 "outputs/area_video_review/*/*_detections.jsonl",
                 "outputs/double_line_rules_20261011/*_detections.jsonl")


def rule_options():
    options = {"red_light_stop_line_crossing": {"crossing_direction": "away"}}
    options.update(area_rule_options(ROOT / "configs/area_rules.json"))
    options.update(double_line_rule_options(ROOT / "configs/double_line_rules.json"))
    return options


def summarize(event):
    vehicle = (event.get("evidence") or {}).get("vehicle") or {}
    return {"rule_id": event.get("rule_id"), "status": event.get("status"),
            "object_id": event.get("object_id"), "frame": event.get("frame"),
            "behavior": event.get("behavior"), "plate": vehicle.get("plate_text")}


def replay(path, rules=("all",)):
    events, engine, frames = [], None, 0
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            observation = json.loads(line)
            if engine is None:
                frame = observation["frame"]
                context = RuleContext(Path(path).stem.removesuffix("_detections"),
                                      frame["fps"], frame["width"], frame["height"])
                engine = ViolationEngine(create_rules(list(rules), context, rule_options()))
            events.extend(engine.evaluate(observation))
            frames += 1
    if engine is not None:
        events.extend(engine.finalize())
    return frames, [summarize(event) for event in events]


def collect(paths):
    result = {}
    for path in paths:
        started = time.perf_counter()
        frames, events = replay(path)
        key = Path(path).relative_to(ROOT).as_posix()
        result[key] = {"frames": frames, "events": events}
        print(f"{key}: {frames} 幀，{len(events)} 個事件（{time.perf_counter()-started:.1f}s）", flush=True)
    return result


def compare(current, baseline):
    differences = []
    for key in sorted(set(current) | set(baseline)):
        if key not in baseline:
            differences.append(f"[新增檔案] {key}")
            continue
        if key not in current:
            differences.append(f"[缺少檔案] {key}")
            continue
        old, new = baseline[key]["events"], current[key]["events"]
        if old != new:
            differences.append(f"[事件不同] {key}")
            for item in old:
                if item not in new:
                    differences.append(f"    - {item}")
            for item in new:
                if item not in old:
                    differences.append(f"    + {item}")
    return differences


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", type=Path, help="指定 JSONL；預設掃描 outputs 內既有結果")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--save", type=Path)
    group.add_argument("--compare", type=Path)
    args = parser.parse_args(argv)
    paths = [p.resolve() for p in args.inputs] or sorted(
        {p.resolve() for pattern in DEFAULT_GLOBS for p in ROOT.glob(pattern)})
    if not paths:
        raise SystemExit("找不到任何 *_detections.jsonl")
    current = collect(paths)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已儲存基準：{args.save}（{len(current)} 個檔案）")
        return 0
    baseline = json.loads(args.compare.read_text(encoding="utf-8"))
    differences = compare({k: v for k, v in current.items() if not args.inputs or k in current},
                          {k: v for k, v in baseline.items() if not args.inputs or k in current})
    if differences:
        print("規則重算與基準不同：")
        print("\n".join(differences))
        return 1
    print(f"規則重算與基準一致（{len(current)} 個檔案）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
