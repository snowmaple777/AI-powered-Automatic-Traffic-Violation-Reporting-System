"""常駐推論程序：啟動時載入模型並暖機一次，之後逐一執行推論服務送來的工作。

由 inference_service.preload 啟動（不直接手動執行）：
    python -u pipeline_worker.py <pipeline_root> [run_pipeline 的額外參數…]

協定（每行一則，stdout 與 stderr 合併）：
    啟動成功  @@WORKER_READY {"device": ..., "load_seconds": ..., "warmup_seconds": ...}
    啟動失敗  @@WORKER_FAILED {"error": ...}，隨後結束
    工作      stdin 一行 {"argv": [run_pipeline 的參數]}；run_pipeline 的輸出照常印出，
              結束時印 @@JOB_END {"code": 0 或非 0, "error": ...}

模型設定（--model-config、模型門檻覆寫、--device）以啟動時的參數為準；工作的參數只換影片與規則等。
每個工作都交給 run_pipeline.run(..., pipeline=...)，它會先 reset 追蹤狀態（與批次評估重用模型的路徑相同）。
"""
import json
import sys
import time
import traceback
from contextlib import ExitStack
from pathlib import Path

READY, FAILED, END = "@@WORKER_READY", "@@WORKER_FAILED", "@@JOB_END"
WARMUP_FRAMES = 2
WARMUP_SIZE = (1080, 1920)


def emit(tag, **data):
    # 前面可能有函式庫印了不換行的片段；先換行，讓標記一定在行首（服務端也會在整行中尋找）。
    sys.stdout.flush()
    print("\n" + f"{tag} {json.dumps(data, ensure_ascii=False)}", flush=True)


def load(extra):
    """與 run_pipeline.run 相同的設定解析，只是模型只建立一次。"""
    import numpy as np
    import run_pipeline
    from model_library import FrameContext, ModelPipeline, load_config

    base = run_pipeline.parse_args(["__preload__"] + extra)
    config = load_config(base.model_config)
    run_pipeline.apply_overrides(config, base)
    device = run_pipeline.resolve_device(base.device)
    resources = ExitStack()
    started = time.perf_counter()
    pipeline = resources.enter_context(ModelPipeline(config, device))
    loaded = time.perf_counter()
    # 暖機：讓 GPU 先完成初始化，第一支影片的前幾幀不必再慢。之後 reset，不留下追蹤狀態。
    frame = np.zeros((*WARMUP_SIZE, 3), np.uint8)
    for i in range(WARMUP_FRAMES):
        pipeline.infer(frame, FrameContext(i, i / 30.0))
    pipeline.reset()
    info = dict(device=device, model_config=str(base.model_config),
                load_seconds=round(loaded - started, 1), warmup_seconds=round(time.perf_counter() - loaded, 1))
    return run_pipeline, pipeline, resources, info


def run_job(run_pipeline, pipeline, argv):
    try:
        with ExitStack() as job_resources:
            run_pipeline.run(run_pipeline.parse_args(argv), job_resources, pipeline=pipeline)
        return 0, None
    except SystemExit as e:  # run_pipeline 以 SystemExit 回報輸入錯誤
        if e.code in (None, 0):
            return 0, None
        message = e.code if isinstance(e.code, str) else f"結束代碼 {e.code}"
        print(message, flush=True)
        return (e.code if isinstance(e.code, int) else 1), message
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return 1, str(e) or type(e).__name__


def main():
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    try:
        run_pipeline, pipeline, resources, info = load(sys.argv[2:])
    except BaseException as e:  # noqa: BLE001
        traceback.print_exc()
        emit(FAILED, error=str(e) or type(e).__name__)
        return 1
    emit(READY, **info)
    with resources:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                argv = json.loads(line)["argv"]
            except (ValueError, KeyError, TypeError) as e:
                emit(END, code=2, error=f"工作格式錯誤：{e}")
                continue
            code, error = run_job(run_pipeline, pipeline, argv)
            emit(END, code=code, error=error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
