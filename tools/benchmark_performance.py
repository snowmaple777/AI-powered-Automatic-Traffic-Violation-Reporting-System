"""Compare the original mixed CPU/GPU placement against ONNX CUDA modes."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import torch
import run_pipeline
from model_library import ModelPipeline, load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('video', type=Path)
    parser.add_argument('--mode', choices=['mixed', 'gpu', 'gpu-exhaustive'], required=True)
    parser.add_argument('--frames', type=int, default=30)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.frames <= 10:
        parser.error('Use more than 10 frames to measure after warmup')
    args.output.mkdir(parents=True, exist_ok=True)
    config = load_config(ROOT / 'configs/default.json')
    for name in ('vehicles', 'plates', 'depth'):
        params = config['models'][name]['params']
        params['device'] = 'cpu' if args.mode == 'mixed' else 'cuda:0'
        params['cuda_conv_search'] = 'EXHAUSTIVE' if args.mode == 'gpu-exhaustive' else 'HEURISTIC'
    config_path = args.output / 'config.json'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    timings = {}

    class TimedPipeline(ModelPipeline):
        def __init__(self, *pos, **kw):
            super().__init__(*pos, **kw)
            for name, model in self.models.items():
                timings[name] = []
                original = model.infer

                def measured(frame, context, upstream, infer=original, stage=name):
                    torch.cuda.synchronize()
                    start = time.perf_counter()
                    result = infer(frame, context, upstream)
                    torch.cuda.synchronize()
                    timings[stage].append(time.perf_counter() - start)
                    return result

                model.infer = measured

    run_pipeline.ModelPipeline = TimedPipeline
    options = run_pipeline.parse_args([str(args.video), '--device', 'cuda:0',
        '--max-frames', str(args.frames), '--model-config', str(config_path),
        '--output-dir', str(args.output), '--rules', 'all', '--no-violation-clips'])
    start = time.perf_counter()
    with ExitStack() as resources:
        run_pipeline.run(options, resources)
    wall = time.perf_counter() - start
    count = len(next(iter(timings.values())))
    if count <= 10:
        raise ValueError('Video must contain more than 10 decoded frames for a steady-state comparison')
    steady_seconds = sum(sum(values[10:]) for values in timings.values())
    report = dict(mode=args.mode, frames=count, wall_seconds=wall,
                  wall_fps=count / wall, warmup_frames=10,
                  model_fps_after_10_frames=(count - 10) / steady_seconds,
                  stage_ms_after_10_frames={k: 1000 * sum(v[10:]) / len(v[10:]) for k, v in timings.items()},
                  frame_stage_seconds=timings)
    (args.output / 'performance.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'frame_stage_seconds'}, indent=2))


if __name__ == '__main__':
    main()
