"""向量化／裁切後的遮罩幾何必須與逐區塊、全畫面的參考算法一致。"""
import json
import unittest

import cv2
import numpy as np

from area_rules import extract_traffic_regions, geometry_options
from model_library.legacy import RoadMarkingSegmenter, mask_components
from road_marking_config import classes


def synthetic_mask(seed, h=360, w=640):
    rng = np.random.default_rng(seed)
    mask = np.zeros((h, w), np.uint8)
    for _ in range(15):
        cls = int(rng.integers(1, len(classes)))
        center = (float(rng.uniform(0, w)), float(rng.uniform(0, h)))
        size = (float(rng.uniform(5, 200)), float(rng.uniform(2, 30)))
        box = cv2.boxPoints((center, size, float(rng.uniform(-80, 80)))).astype(np.int32)
        cv2.fillPoly(mask, [box], cls)
    for name in ("crosswalk", "motor waiting zone"):
        x0, y0 = int(rng.uniform(0, w - 150)), int(rng.uniform(0, h - 50))
        for k in range(5):
            cv2.rectangle(mask, (x0 + k * 22, y0), (x0 + k * 22 + 10, y0 + 40), classes.index(name), -1)
    return mask


def reference_components(mask, min_pixels):
    records = []
    for class_id in [int(c) for c in np.unique(mask) if c]:
        binary = (mask == class_id).astype(np.uint8)
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
        components = []
        for label in range(1, count):
            if stats[label, cv2.CC_STAT_AREA] < min_pixels:
                continue
            ys, xs = np.where(labels == label)
            components.append({"bbox_xywh": [int(v) for v in stats[label, :4]],
                               "area": int(stats[label, cv2.CC_STAT_AREA]),
                               "centroid": centroids[label].tolist(),
                               "line_xyxy": RoadMarkingSegmenter._fit_line(xs, ys, mask.shape[1], mask.shape[0])})
        if components:
            records.append((class_id, int(binary.sum()), components))
    return records


def reference_regions(mask, markings, cfg):
    """舊版：全畫面閉運算後逐區塊取像素。"""
    h, w = mask.shape
    gap = max(1, round(min(h, w) * cfg["close_gap_ratio"]))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gap * 2 + 1, gap * 2 + 1))
    hulls = []
    for marking in markings:
        if marking["class_name"] not in ("crosswalk", "motor waiting zone"):
            continue
        binary = (mask == marking["class_id"]).astype(np.uint8)
        count, labels = cv2.connectedComponents(cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel))
        for label in range(1, count):
            ys, xs = np.where((labels == label) & (binary != 0))
            if len(xs) < cfg["min_region_pixels"]:
                continue
            hull = cv2.convexHull(np.column_stack((xs, ys)).astype(np.float32)).reshape(-1, 2)
            area = cv2.contourArea(hull)
            if (area <= 1 or area > h * w * cfg["max_region_frame_ratio"] or len(xs) / area < cfg["min_fill_ratio"]
                    or min(cv2.minAreaRect(hull)[1]) < cfg["min_region_side_px"]):
                continue
            hulls.append(hull.tolist())
    return hulls


class MaskGeometryTests(unittest.TestCase):
    def test_components_match_per_component_fitline(self):
        for seed in range(8):
            mask = synthetic_mask(seed)
            expected = reference_components(mask, 50)
            actual = mask_components(mask, classes, 50)
            self.assertEqual([(r["class_id"], r["pixels"]) for r in actual], [e[:2] for e in expected])
            for record, (_, _, reference) in zip(actual, expected):
                key = lambda c: (c["bbox_xywh"][1], c["bbox_xywh"][0], c["area"])
                self.assertEqual([c["bbox_xywh"] for c in record["components"]],
                                 [c["bbox_xywh"] for c in sorted(reference, key=key)])
                for got, ref in zip(record["components"], sorted(reference, key=key)):
                    self.assertLessEqual(max(abs(a - b) for a, b in zip(got["centroid"], ref["centroid"])), .006)
                    if ref["line_xyxy"] is not None:
                        x1, y1, x2, y2 = ref["line_xyxy"]
                        if abs(abs(y2 - y1) - abs(x2 - x1)) > 1:  # 45° 平手時端點選擇可能不同
                            self.assertLessEqual(max(abs(a - b) for a, b in zip(got["line_xyxy"], ref["line_xyxy"])), .05)

    def test_cropped_region_closing_matches_full_frame(self):
        cfg = geometry_options({})
        for seed in range(8):
            mask = synthetic_mask(seed)
            markings = mask_components(mask, classes, 50)
            actual = sorted(json.dumps(r["polygon"]) for r in extract_traffic_regions(mask, markings, cfg))
            expected = sorted(json.dumps(p) for p in reference_regions(mask, markings, cfg))
            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
