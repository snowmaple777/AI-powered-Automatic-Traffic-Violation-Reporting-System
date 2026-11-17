"""確認規則分階段評估；逐幀讀一次，多組規則獨立執行，不載入模型。

manifest 指定 sources（名稱、觀測資料夾、truth、primary_only）與 variants
（名稱 → rule_options 覆寫）；輸出完整事件、主類別／全部確認指標與事件差異。
長片未完整標註：只列事件率及已覆核誤報，不能當成完整負例。
"""
import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.metrics import evaluate
from tools.rule_eval import rule_options, git_state
from violation_engine import RuleContext, ViolationEngine, create_rules


def merge(base, extra):
    result = copy.deepcopy(base)
    for k, v in extra.items():
        result[k] = merge(result.get(k, {}), v) if isinstance(v, dict) else v
    return result


def metrics(truth, predictions, primary_only=False, rules=None):
    truth = copy.deepcopy(truth)
    if rules and 'all' not in rules:
        for spec in truth['videos'].values():
            spec['events'] = [e for e in spec.get('events', []) if e['rule_id'] in rules]
    selected = {v: [e for e in es if not primary_only or
                   e['rule_id'] == truth['videos'][v].get('category')]
                for v, es in predictions.items() if v in truth['videos']}
    return {status: evaluate(truth, {v: [e for e in es if status == 'all' or e['status'] == 'confirmed']
                                     for v, es in selected.items()})[1]
            for status in ('all', 'confirmed')}


def known_false_positives(review, predictions):
    matches = []
    for video, spec in review.get('videos', {}).items():
        for gt in spec.get('reviewed_events', []):
            if gt.get('judgement') != 'false_positive':
                continue
            # 同物件、同規則與鄰近時間；只列已知案例，不將未列出的事件當成真陰性。
            for e in predictions.get(video, []):
                if (e['rule_id'] == gt['rule_id'] and e['object_id'] == gt['object_id']
                        and abs(e['timestamp_sec'] - gt['timestamp_sec']) <= 3):
                    matches.append(dict(video=video, event=e))
    return matches


def replay_video(task):
    folder, dest, options, rules, resume = task
    name = folder.name
    dest.mkdir(parents=True, exist_ok=True)
    done = dest / 'done.json'
    source_path = folder / f'{name}_detections.jsonl'
    stat = source_path.stat()
    signature = [stat.st_size, stat.st_mtime_ns]
    if resume and done.exists():
        meta = json.loads(done.read_text())
        if meta['input'] != signature:
            raise ValueError(f'觀測輸入已變動：{source_path}')
        return name, meta['duration'], {v: json.loads((dest / f'{v}.json').read_text(encoding='utf-8')) for v in options}
    engines = None
    events = {v: [] for v in options}
    with source_path.open(encoding='utf-8') as stream:
        for line in stream:
            obs = json.loads(line)
            f = obs['frame']
            if f['index'] % 3000 == 0:
                (dest / 'progress.json').write_text(json.dumps(dict(frame=f['index'], seconds=f['timestamp_sec'])))
            if engines is None:
                context = RuleContext(name, f['fps'], f['width'], f['height'])
                engines = {v: ViolationEngine(create_rules(rules, context, cfg)) for v, cfg in options.items()}
            for v, engine in engines.items():
                events[v].extend(engine.evaluate(obs))
    if engines is None:
        raise ValueError(f'空觀測檔：{source_path}')
    for v, engine in engines.items():
        events[v].extend(engine.finalize())
        (dest / f'{v}.json').write_text(json.dumps(events[v], ensure_ascii=False), encoding='utf-8')
    seconds = f['timestamp_sec'] + 1 / f['fps']
    done.write_text(json.dumps(dict(input=signature, duration=seconds)))
    return name, seconds, events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--workers', type=int, default=1, choices=range(1, 9))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    args.out.mkdir(parents=True, exist_ok=True)
    code = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in
            ('violation_engine.py', 'area_rules.py', 'signal_state.py', 'double_lines.py',
             'tools/confirmation_eval.py', 'tools/rule_eval.py')}
    options = {name: merge(rule_options(), value) for name, value in manifest['variants'].items()}
    stamp = dict(manifest=manifest, code=code, options=options)
    stamp_path = args.out / 'manifest.json'
    if args.resume and stamp_path.exists() and json.loads(stamp_path.read_text(encoding='utf-8')) != stamp:
        raise ValueError('設定或程式已變動，不可接續舊實驗')
    stamp_path.write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.out / 'git.json').write_text(json.dumps(git_state()), encoding='utf-8')
    reports = {}
    for source in manifest['sources']:
        predictions = {v: {} for v in options}
        duration = 0
        folders = sorted(p for p in Path(source['path']).iterdir() if (p / f'{p.name}_detections.jsonl').is_file())
        tasks = [(folder, args.out / source['name'] / folder.name, options, manifest.get('rules', ['all']), args.resume)
                 for folder in folders]
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for n, (name, seconds, events) in enumerate(pool.map(replay_video, tasks), 1):
                duration += seconds
                for v in options:
                    predictions[v][name] = events[v]
                print(f"{source['name']} {n}/{len(folders)} {name} " + str({v: dict(Counter(e['status'] for e in es))
                      for v, es in events.items()}), flush=True)
        report = {}
        baseline = next(iter(options))
        truth = json.loads(Path(source['truth']).read_text(encoding='utf-8-sig')) if source.get('truth') else None
        for v, pred in predictions.items():
            flat = [e for es in pred.values() for e in es]
            row = dict(videos=len(pred), duration_seconds=duration,
                       counts=dict(Counter(e['rule_id'] + ':' + e['status'] for e in flat)),
                       events_per_hour=len(flat) / (duration / 3600),
                       confirmed_per_hour=sum(e['status'] == 'confirmed' for e in flat) / (duration / 3600))
            if truth:
                row['metrics'] = metrics(truth, pred, source.get('primary_only', False), manifest.get('rules'))
            if source.get('review'):
                review = json.loads(Path(source['review']).read_text(encoding='utf-8-sig'))
                row['known_fp'] = known_false_positives(review, pred)
            row['changed'] = {video: dict(before=predictions[baseline][video], after=es)
                              for video, es in pred.items() if es != predictions[baseline][video]}
            report[v] = row
        reports[source['name']] = report
        (args.out / 'report.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
        print(source['name'], json.dumps({v: {k: x for k, x in row.items() if k not in ('changed', 'known_fp')}
                                        for v, row in report.items()}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
