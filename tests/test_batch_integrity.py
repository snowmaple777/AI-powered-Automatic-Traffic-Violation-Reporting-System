"""批次輸出的完成範圍、錯誤與續跑身分，使用假模型而不載入 GPU。"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

import run_pipeline
from model_library.base import ModelResult
from model_library.registry import STAGES
from tools import batch_eval


class BatchIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "sample.mp4"
        self.source.write_bytes(b"input")
        self.config = self.root / "config.json"
        self.config.write_text('{"version":1,"models":{}}', encoding="utf-8")

    def run_fake(self, count=3, reported=3, limit=0, fps=10, mask=False):
        cap = Mock()
        cap.isOpened.return_value = True
        cap.get.side_effect = lambda key: {cv2.CAP_PROP_FPS: fps, cv2.CAP_PROP_FRAME_WIDTH: 32,
            cv2.CAP_PROP_FRAME_HEIGHT: 24, cv2.CAP_PROP_FRAME_COUNT: reported}.get(key, 0)
        frame = np.zeros((24, 32, 3), np.uint8)
        cap.read.side_effect = [(True, frame)] * count + [(False, None)]
        results = {s: ModelResult() for s in STAGES}
        if mask:
            results["road_markings"] = ModelResult([], {"mask": np.zeros((24, 32), np.uint8)})
        pipeline = Mock(models={}, infer=Mock(return_value=results))
        args = run_pipeline.parse_args([str(self.source), "--model-config", str(self.config),
            "--output-dir", str(self.root / "out"), "--device", "cpu", "--rules", "all",
            "--max-frames", str(limit), "--no-marking-compensation", "--no-violation-clips"])
        if mask:
            args.save_masks = self.root / "out/masks"
        with patch("run_pipeline.cv2.VideoCapture", return_value=cap), ExitStack() as resources:
            result = run_pipeline.run(args, resources, pipeline=pipeline)
        return result, cap

    def test_complete_output_can_be_validated(self):
        result, _ = self.run_fake()
        self.assertEqual(result["completion"]["status"], "complete")
        self.assertEqual(batch_eval.validate_outputs(self.root / "out", "sample", False)["frames"], 3)

    def test_zero_frames_fail(self):
        with self.assertRaisesRegex(RuntimeError, "任何影格"):
            self.run_fake(count=0)

    def test_early_decode_failure_fails(self):
        with self.assertRaisesRegex(RuntimeError, "截斷"):
            self.run_fake(count=2, reported=3)

    def test_limit_is_partial_and_does_not_decode_extra_frame(self):
        result, cap = self.run_fake(count=3, limit=1)
        self.assertEqual(cap.read.call_count, 1)
        self.assertEqual(result["completion"]["status"], "partial")
        self.assertEqual(batch_eval.validate_outputs(self.root / "out", "sample", False)["frames"], 1)

    def test_unknown_frame_count_is_not_a_verified_full_video(self):
        result, _ = self.run_fake(reported=0)
        self.assertEqual(result["completion"]["termination"], "unverified_eof")
        with self.assertRaises(ValueError):
            batch_eval.validate_outputs(self.root / "out", "sample", False)

    def test_invalid_fps_fails(self):
        for fps in (0, -1, float("nan")):
            with self.assertRaises(ValueError):
                self.run_fake(fps=fps)

    def test_mask_write_failure_propagates(self):
        with patch("run_pipeline.cv2.imwrite", return_value=False), self.assertRaises(OSError):
            self.run_fake(mask=True)

    def test_missing_mask_is_rejected(self):
        self.run_fake(mask=True)
        (self.root / "out/masks/000001.png").unlink()
        with self.assertRaisesRegex(ValueError, "遮罩數量"):
            batch_eval.validate_outputs(self.root / "out", "sample", True)

    def test_unicode_mask_directory_and_corrupt_mask(self):
        # 模擬正常輸出再複製到中文目錄，獨立驗證讀取端，不依賴 imwrite 的路徑支援。
        import shutil
        self.run_fake(mask=True)
        folder = self.root / "新增資料夾 (3)" / "測試輸出"
        shutil.copytree(self.root / "out", folder)
        self.assertEqual(batch_eval.validate_outputs(folder, "sample", True)["frames"], 3)
        (folder / "masks/000001.png").write_bytes(b"not a png")
        with self.assertRaisesRegex(ValueError, "無法解碼遮罩"):
            batch_eval.validate_outputs(folder, "sample", True)

    def test_missing_jsonl_frame_is_rejected(self):
        self.run_fake()
        p = self.root / "out/sample_detections.jsonl"
        p.write_text(p.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "幀數"):
            batch_eval.validate_outputs(self.root / "out", "sample", False)

    def make_done(self):
        self.run_fake()
        folder = self.root / "out"
        request = {"code": "a", "settings": "a", "input": "a", "max_frames": 0}
        batch_eval.write_json_atomic(folder / "done.json", {"version": 2, "status": "complete",
            "request": request, "outputs": batch_eval.output_inventory(folder)})
        return folder, request

    def test_resume_requires_exact_request(self):
        folder, request = self.make_done()
        self.assertEqual(batch_eval.verify_done(folder, request)["status"], "complete")
        for key in request:
            with self.assertRaisesRegex(ValueError, "處理範圍不同"):
                batch_eval.verify_done(folder, {**request, key: "changed"})

    def test_modified_output_rejects_resume(self):
        folder, request = self.make_done()
        (folder / "sample_violations.jsonl").write_text('{}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "輸出遭修改"):
            batch_eval.verify_done(folder, request)

    def test_legacy_done_is_not_trusted(self):
        self.root.joinpath("done.json").write_text('{"seconds":1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            batch_eval.verify_done(self.root)

    def test_partial_and_failed_attempts_are_not_scored(self):
        folder, request = self.make_done()
        done = json.loads((folder / "done.json").read_text(encoding="utf-8"))
        done["status"] = "partial"
        batch_eval.write_json_atomic(folder / "done.json", done)
        failed = self.root / ".attempts/failed"
        failed.mkdir(parents=True)
        (failed / "bad_violations.jsonl").write_text('invalid', encoding="utf-8")
        with patch.object(batch_eval, "EVAL_DIR", self.root.parent):
            batch_eval.summarize(self.root.name, verified_only=True)
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["rows"], [])

    def test_run_failure_is_kept_in_attempt_without_published_done(self):
        # 使用一支真實存在於標註中的來源，但只模擬 run，不載入模型。
        args = Mock(summary_only=False, only=["ms01"], model_config=self.config,
                    max_frames=0, label="test")
        with patch.object(batch_eval, "settings_fingerprint", return_value={}), \
             patch.object(batch_eval, "code_version", return_value={}), \
             patch("model_library.ModelPipeline"), patch("run_pipeline.run", side_effect=OSError("disk full")), \
             patch("run_pipeline.resolve_device", return_value="cpu"):
            with self.assertRaises(OSError):
                batch_eval.execute_batch(args, self.root)
        self.assertFalse((self.root / "ms01").exists())
        self.assertEqual(len(list((self.root / ".attempts").rglob("failure.json"))), 1)
        self.assertEqual(list(self.root.rglob("done.json")), [])


if __name__ == "__main__":
    unittest.main()
