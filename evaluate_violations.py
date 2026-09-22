"""Run violation rules again from saved model output, without loading models."""

import argparse
import itertools
import json
from pathlib import Path
from contextlib import ExitStack

from violation_engine import RuleContext, ViolationEngine, create_rules


def main():
    parser = argparse.ArgumentParser(description="從模型 JSONL 輸出執行違規規則")
    parser.add_argument("input", type=Path, help="*_detections.jsonl")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rules", "--rule", default="red_light_stop_line_crossing",
                        help="逗號分隔的規則名稱；all 啟用所有已註冊規則")
    parser.add_argument("--crossing-direction", choices=["away", "toward"], default="away",
                        help="away：前方車輛遠離鏡頭；toward：朝向鏡頭")
    parser.add_argument("--diagnostics", type=Path, help="輸出逐幀規則診斷 JSONL")
    args = parser.parse_args()
    output = args.output or args.input.with_name(
        args.input.stem.removesuffix("_detections") + "_violations.jsonl")
    paths = [args.input.resolve(), output.resolve()]
    if args.diagnostics:
        paths.append(args.diagnostics.resolve())
    if len(set(paths)) != len(paths):
        parser.error("輸入、事件輸出與診斷檔必須是不同路徑")
    names = [name.strip() for name in args.rules.split(",") if name.strip()]
    with ExitStack() as resources, args.input.open(encoding="utf-8") as source:
        first_line = source.readline()
        if not first_line:
            raise SystemExit("輸入 JSONL 沒有資料")
        first = json.loads(first_line)
        frame = first["frame"]
        context = RuleContext(args.input.stem.removesuffix("_detections"), frame["fps"],
                              frame["width"], frame["height"])
        engine = ViolationEngine(create_rules(names, context, {
            "red_light_stop_line_crossing": {"crossing_direction":args.crossing_direction}}))
        diagnostics = resources.enter_context(args.diagnostics.open("w", encoding="utf-8")) if args.diagnostics else None
        with output.open("w", encoding="utf-8") as destination:
            observations = itertools.chain(
                [first], (json.loads(line) for line in source))
            for observation in observations:
                for event in engine.evaluate(observation):
                    destination.write(json.dumps(event, ensure_ascii=False) + "\n")
                if diagnostics:
                    diagnostics.write(json.dumps({"frame":observation["frame"]["index"],
                        "rules":{rule.rule_id:getattr(rule,"diagnostics",{}) for rule in engine.rules}},
                        ensure_ascii=False)+"\n")
            for event in engine.finalize():
                destination.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"違規候選事件：{output}")


if __name__ == "__main__":
    main()
