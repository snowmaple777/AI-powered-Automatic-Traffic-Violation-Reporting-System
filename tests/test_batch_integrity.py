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

    def run_fake(self, count=3, reported=3, limit=0, fps=10, mask=False, allow_short=False):
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
        args.allow_short_decode = allow_short
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

    def test_final_diagnostics_keep_frame_count_and_reject_truncated_or_extra_rows(self):
        self.run_fake()
        folder = self.root / "out"
        path = folder / "sample_rule_diagnostics.jsonl"
        original = path.read_text(encoding="utf-8")
        before = batch_eval.diagnose(folder, "sample", "red_light_stop_line_crossing")
        final = json.dumps({"frame": 2, "finalize": True, "rules": {
            "double_yellow_line_crossing": {"frame": 2, "state_changes": []}}}) + "\n"
        path.write_text(original + final, encoding="utf-8")
        self.assertEqual(batch_eval.validate_outputs(folder, "sample", False)["frames"], 3)
        self.assertEqual(batch_eval.diagnose(folder, "sample", "red_light_stop_line_crossing"), before)
        for bad in (original + final + final, original + final + original.splitlines()[0] + "\n",
                    "\n".join(original.splitlines()[:-1]) + "\n" + final,
                    original + final.replace('"frame": 2', '"frame": 1')):
            path.write_text(bad, encoding="utf-8")
            with self.assertRaises(ValueError):
                batch_eval.validate_outputs(folder, "sample", False)

    def test_early_decode_failure_fails(self):
        with self.assertRaisesRegex(RuntimeError, "截斷"):
            self.run_fake(count=2, reported=3)

    def test_short_decode_is_opt_in_and_recorded(self):
        result, _ = self.run_fake(count=2, reported=3, allow_short=True)
        self.assertEqual(result["completion"]["status"], "complete")
        self.assertEqual(result["completion"]["termination"], "short_eof")
        self.assertEqual(result["frames"], 2)
        folder = self.root / "out"
        with self.assertRaisesRegex(ValueError, "解碼範圍"):
            batch_eval.validate_outputs(folder, "sample", False)
        self.assertEqual(batch_eval.validate_outputs(folder, "sample", False, allow_short_decode=True)["frames"], 2)
        truth = {"videos": {"sample": {"events": [{"start_sec": 0.05, "end_sec": 0.1},
                                                  {"start_sec": 0.15, "end_sec": 0.3}]}}}
        (self.root / "sample").mkdir()
        for path in folder.iterdir():
            if path.is_file():
                (self.root / "sample" / path.name).write_bytes(path.read_bytes())
        notes = batch_eval.short_decode_notes(self.root, truth, ["sample"])
        self.assertEqual(notes, {"sample": {"frames": 2, "expected_frames": 3, "decoded_seconds": 0.2,
                                            "events_beyond": 1}})

    def test_short_decode_flag_does_not_accept_extra_frames(self):
        with self.assertRaisesRegex(RuntimeError, "截斷"):
            self.run_fake(count=4, reported=3, allow_short=True)

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

    def test_custom_truth_scores_unreviewed_videos_at_video_level(self):
        folder = self.root / "cy999"
        folder.mkdir()
        event = {"rule_id": "double_yellow_line_crossing", "status": "suspected", "timestamp_sec": 1.0}
        (folder / "cy999_violations.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        batch_eval.write_json_atomic(folder / "done.json", {"version": 2, "status": "complete",
            "request": {}, "outputs": batch_eval.output_inventory(folder)})
        truth = self.root / "pending.json"
        truth.write_text(json.dumps({"videos": {"cy999": {"file": "x.mp4", "reviewed": False, "events": [],
            "category": "double_yellow_line_crossing"}}}), encoding="utf-8")
        with patch.object(batch_eval, "EVAL_DIR", self.root.parent):
            text = batch_eval.summarize(self.root.name, verified_only=True, truth_path=truth)
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["rows"], [["cy999", "double_yellow_line_crossing", True, 1]])
        self.assertEqual(report["event_level"], {})  # 未覆核影片不計事件層級
        self.assertEqual(report["truth_identity"], batch_eval.file_identity(truth))
        self.assertIn("影片清單", text)
        # 指紋改用指定清單：執行中修改 eval/ground_truth.json 不會中斷這批。
        with patch("model_library.load_config", return_value={"models": {}}):
            keys = set(batch_eval.settings_fingerprint(self.config, truth)["sha256"])
            default_keys = set(batch_eval.settings_fingerprint(self.config)["sha256"])
        self.assertIn(batch_eval.truth_name(truth), keys)
        self.assertNotIn("eval/ground_truth.json", keys)
        self.assertIn("eval/ground_truth.json", default_keys)

    def test_run_failure_is_kept_in_attempt_without_published_done(self):
        # 使用一支真實存在於標註中的來源，但只模擬 run，不載入模型。
        args = Mock(summary_only=False, only=["ms01"], model_config=self.config,
                    max_frames=0, label="test", truth=batch_eval.DEFAULT_TRUTH)
        with patch.object(batch_eval, "settings_fingerprint", return_value={}), \
             patch.object(batch_eval, "code_version", return_value={}), \
             patch("model_library.ModelPipeline"), patch("run_pipeline.run", side_effect=OSError("disk full")), \
             patch("run_pipeline.resolve_device", return_value="cpu"):
            with self.assertRaises(OSError):
                batch_eval.execute_batch(args, self.root)
        self.assertFalse((self.root / "ms01").exists())
        self.assertEqual(len(list((self.root / ".attempts").rglob("failure.json"))), 1)
        self.assertEqual(list(self.root.rglob("done.json")), [])


    def test_publish_rename_retries_transient_permission_error(self):
        source, target = self.root / "staging", self.root / "final"
        source.mkdir()
        real_rename, calls = Path.rename, []

        def flaky(path, destination):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(13, "access denied")
            return real_rename(path, destination)

        with patch.object(Path, "rename", flaky), patch.object(batch_eval.time, "sleep") as sleep:
            batch_eval.rename_with_retry(source, target)
        self.assertTrue(target.is_dir())
        self.assertEqual((len(calls), sleep.call_count), (3, 2))
        source.mkdir()
        with patch.object(Path, "rename", side_effect=PermissionError(13, "denied")), \
             patch.object(batch_eval.time, "sleep"), self.assertRaises(PermissionError):
            batch_eval.rename_with_retry(source, self.root / "other", attempts=3)


if __name__ == "__main__":
    unittest.main()
