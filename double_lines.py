"""Finite double-line geometry and conservative frame-to-frame association.

Plain dictionaries are the persisted contract and the in-memory tracker state.
No crossing decisions or inferred/occluded line extensions belong in this layer.
"""
import json
import math
from pathlib import Path

import cv2
import numpy as np

from core.continuity import is_continuous
from core.geometry import project_points  # noqa: F401  舊模組仍由此匯入
from core.motion import homography, road_motion
from core.fastcopy import clone
from observation_schema import marking_evidence, marking_geometry_source


DOUBLE_LINE_CLASSES = {"solid double yellow", "solid double white"}

# All rule thresholds are overridable by configs/double_line_rules.json.
RULE_DEFAULTS = dict(
    min_line_observations=3, min_line_length_px=30.0, min_pair_gap_px=2.0,
    max_pair_angle_deg=20.0, max_pair_gap_ratio=4.0, endpoint_margin_px=6.0,
    # 4ay：側別邊距近乎為零（驗證要求 >0），車底點離開線帶即算該側；配準誤差項仍保留。
    side_margin_px=.01, margin_width_ratio=.001, max_motion_error_px=5.0,
    min_vehicle_confidence=.35, stable_observations=3, stable_seconds=.15,
    max_observation_gap_sec=.3, max_frame_gap_sec=.2, max_crossing_seconds=6.0,
    max_box_scale_ratio=1.7, max_step_box_ratio=1.0, rearm_seconds=1.0,
    heading_window_sec=.6, min_heading_displacement_px=8.0,
    heading_consistency=.8, max_lane_change_angle_deg=35.0,
    min_uturn_angle_deg=140.0, min_longitudinal_alignment=.7, emit_suspected=True,
    allow_semantic_band=True, max_partial_support_seconds=3.0,
    min_fragment_length_px=45.0, fragment_match_distance_px=12.0, max_occluded_gap_sec=.5,
    footprint_inset_ratio=.15,
    # 以下放寬條件只讓事件成為「疑似」：
    allow_motion_gaps=True, max_motion_gap_sec=1.0,  # 鏡頭運動短暫失敗時以「不動」近似，不清除候選
    max_extrapolation_ratio=.5,  # 車底超出雙線可見端點時，可沿端點方向外插（上限為車框高度的比例）
    max_line_handoff_px=25.0,  # 雙線追蹤換編號時，依位置把候選轉移到新編號的距離上限（0＝停用）
    # 確認需要直接觀測的標線：線本身至少 min_line_observations 次直接觀測，越線前後各至少一個樣本
    # 落在直接觀測的幾何上，且沒有樣本落在未對齊（鏡頭運動失效時沿用）的推移幾何上。不成立時最多為疑似。
    require_direct_marking_evidence=True, detailed_diagnostics=False,
    # 4az 方向分類取樣邊距（預設啟用，等於 4ay 以前的側別邊距）。側別邊距很小時（4ay），靠近線帶的橫移樣本
    # 會進入方向判斷，使變換車道／迴轉退回 complete_crossing。啟用時方向分類只用車底三點離線帶超過
    # 「固定值＋線帶寬度比例＋配準誤差」的樣本，至少 direction_min_samples 個；只影響 behavior／direction，
    # 不改變事件是否成立、時間、狀態與條件。
    direction_side_margin_px=4.0,  # 方向取樣的固定邊距（0＝停用，沿用原跨越前後全部樣本）
    direction_margin_width_ratio=.2,  # 方向取樣邊距中的線帶寬度比例
    direction_min_samples=2,  # 啟用時每側至少需要的方向樣本數
    # 4ba 起點與終點推定（方案二，預設關閉；同樣不寫入預設設定）。原規則某（車, 線）候選剛建立穩定起點
    # （seeding→armed）時，用目前這條線重新量測同車較早、經運動補償帶到目前座標的車框：另一側至少
    # direction_min_samples 個方向邊距外（4az）的樣本（彼此相距 1 秒內），與終點側方向邊距外樣本的間隔
    # 不超過此秒數（中間可缺車框或量不到位置）時，輸出疑似事件（stable_origin／complete_band_crossing=False，
    # 永遠不會確認）。需要 direction_side_margin_px > 0。
    endpoint_inference_seconds=0.0,  # 起點與終點最長間隔秒數（0＝停用）
    # 遠方小車防護：建立終點穩定時的車框高度至少為畫面高度的此比例（0＝不檢查）。試算的誤報多是壓在線
    # 遠端的遠方小車（車框約畫面高 5–6%），真違規車框 ≥ 8.8%；此值參考少量人工覆核選定，見 refactor_log 4ba。
    endpoint_min_box_height_ratio=.07,
    # 4bb 壓線停留跨越（預設關閉；同樣不寫入預設設定）。不經原規則狀態機，只看每台車逐幀相對「同一幀雙線」的
    # 側別序列（不分雙線追蹤編號；每幀量測本身不需要運動補償，鏡頭運動失效或線換編號時序列不中斷）：
    # 一側至少 direction_min_samples 個方向邊距外樣本（彼此 1 秒內）→ 至少 band_dwell_min_observations 次
    # 車底點落在線帶本身 → 另一側連續 stable_observations 次、跨 stable_seconds（其中至少 direction_min_samples
    # 個方向邊距外樣本）。只採用可見長度達 band_dwell_min_line_ratio × 畫面高度的雙線上的量測（排除碎片線）。
    # 輸出疑似事件（stable_origin=False，永遠不會確認）；需要 direction_side_margin_px > 0。
    band_dwell_seconds=0.0,  # 起點側最後一個方向樣本到終點側第一個方向樣本的最長秒數（0＝停用）
    band_dwell_min_observations=3,  # 起點與終點之間落在線帶本身的最少觀測次數
    band_dwell_min_line_ratio=.15,  # 採用量測的雙線最短可見長度（畫面高度比例）
    band_dwell_min_box_height_ratio=.07,  # 確認時車框高度下限（畫面高度比例；0＝不檢查）
    # 4bj 召回細節（預設關閉；等於預設值時不寫入規則設定與事件參數，輸出與先前逐位元相同）。
    # same_vehicle_merge_seconds：同一規則、同一車在上一筆雙線事件（以輸出時間計）之後此秒數內的疑似事件不再輸出
    #   （一次越線超車常被原規則、4ba、4bb 各判一次，或在線的遠端尖點反覆成立）；確認事件一律照常輸出。
    same_vehicle_merge_seconds=0.0,
    # band_dwell_last_band_time：4bb 事件時間取最後一個線帶樣本（原規則的事件時間也是最後一個跨越中樣本），
    #   而不是第一個線帶樣本；只改事件時間與 transition 快照，不改是否成立。
    band_dwell_last_band_time=False,
    # 4cb 壓線疑似（預設關閉；等於預設值時不寫入規則設定與事件參數）：不要求看到起點側（車一出現就已壓在線上、
    #   或由後方超車進入畫面），車底點落在線帶本身的觀測至少 band_dwell_min_observations 次、首末相距達此秒數即成立。
    #   沿用 4bb 的逐幀側別序列、線長（band_dwell_min_line_ratio）與車框高度（band_dwell_min_box_height_ratio）門檻；
    #   原規則對同一台車有跨越中的候選時交給原規則。只產生疑似（behavior=line_straddle，complete_band_crossing=False）。
    straddle_presence_seconds=0.0)

# 4ba 選項：等於預設值時不放進規則的設定字典（事件 evidence.parameters 不變）。
ENDPOINT_OPTION_KEYS = ("endpoint_inference_seconds", "endpoint_min_box_height_ratio")

# 4bb 選項：同 ENDPOINT_OPTION_KEYS。
# 4bj 選項：同 ENDPOINT_OPTION_KEYS。
RECALL_OPTION_KEYS = ("same_vehicle_merge_seconds", "band_dwell_last_band_time", "straddle_presence_seconds")
BAND_DWELL_OPTION_KEYS = ("band_dwell_seconds", "band_dwell_min_observations", "band_dwell_min_line_ratio",
                          "band_dwell_min_box_height_ratio")


# 4bg 雙線幾何選項（後處理，不是規則參數；寫在雙線設定檔的 "geometry" 子物件，預設關閉）。
# trim_wide_rows：過寬元件（某列寬度 > 畫面寬 WIDE_ROW_RATIO）原本整個丟棄；啟用時只去掉過寬的列，
# 保留最長一段連續的合格列（至少 MIN_TRIMMED_ROWS 列），沒有就仍丟棄。分割常把兩條漆線連同中間間隔
# 標成同一塊，彎道、近處或斜度大的雙線因此在畫面下方變寬；截掉那幾列後，其餘幾何與追蹤流程不變，
# 截短後的元件仍是單一語意帶，不會成為成對元件。只在 FrameProcessor 重算幾何時生效（run_pipeline、
# real_replay）；rule_eval 等只重算規則的工具沿用 JSONL 裡的幾何。評估見 refactor_log 4be／4bf／4bg。
GEOMETRY_DEFAULTS = dict(trim_wide_rows=False)
WIDE_ROW_RATIO = .08
MIN_TRIMMED_ROWS = 8


def validate_geometry_settings(overrides=None):
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - GEOMETRY_DEFAULTS.keys():
        raise ValueError("Unknown double-line geometry settings")
    settings = {**GEOMETRY_DEFAULTS, **overrides}
    if type(settings["trim_wide_rows"]) is not bool:
        raise ValueError("geometry.trim_wide_rows must be boolean")
    return settings


def double_line_geometry_options(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Double-line rule config requires version=1")
    return validate_geometry_settings(data.get("geometry"))


def validate_rule_settings(overrides=None):
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - RULE_DEFAULTS.keys():
        raise ValueError("Unknown double-line rule settings")
    settings = {**RULE_DEFAULTS, **overrides}
    for key, value in settings.items():
        if key in ("emit_suspected", "allow_semantic_band", "allow_motion_gaps", "require_direct_marking_evidence", "detailed_diagnostics",
                   "band_dwell_last_band_time"):
            if type(value) is not bool:
                raise ValueError(f"{key} must be boolean")
        elif type(value) not in (int, float) or not math.isfinite(value) or (value < 0 if key in ("footprint_inset_ratio", "max_extrapolation_ratio", "max_line_handoff_px",
                                                                                                         "direction_side_margin_px", "direction_margin_width_ratio",
                                                                                                         "endpoint_inference_seconds", "endpoint_min_box_height_ratio",
                                                                                                         "band_dwell_seconds", "band_dwell_min_line_ratio",
                                                                                                         "band_dwell_min_box_height_ratio", "same_vehicle_merge_seconds",
                                                                                                         "straddle_presence_seconds") else value <= 0):
            raise ValueError(f"{key} must be finite and positive")
    for key in ("endpoint_min_box_height_ratio", "band_dwell_min_line_ratio", "band_dwell_min_box_height_ratio"):
        if settings[key] >= 1:
            raise ValueError(f"{key} must be less than 1")
    if type(settings["band_dwell_min_observations"]) is not int or settings["band_dwell_min_observations"] < 1:
        raise ValueError("band_dwell_min_observations must be an integer >= 1")
    if settings["band_dwell_seconds"] > 0 and not settings["direction_side_margin_px"]:
        raise ValueError("band_dwell_seconds requires direction_side_margin_px > 0")
    if settings["straddle_presence_seconds"] > 0 and not settings["direction_side_margin_px"]:
        raise ValueError("straddle_presence_seconds requires direction_side_margin_px > 0")
    if settings["endpoint_inference_seconds"] > 0 and not settings["direction_side_margin_px"]:
        raise ValueError("endpoint_inference_seconds requires direction_side_margin_px > 0")
    for key in ("min_line_observations", "stable_observations", "direction_min_samples"):
        if type(settings[key]) is not int or settings[key] < 2:
            raise ValueError(f"{key} must be an integer >= 2")
    for key in ("min_vehicle_confidence", "heading_consistency", "min_longitudinal_alignment"):
        if settings[key] > 1:
            raise ValueError(f"{key} must be <= 1")
    if not 0 < settings["max_lane_change_angle_deg"] < settings["min_uturn_angle_deg"] <= 180:
        raise ValueError("Lane-change and U-turn angle ranges must not overlap")
    if settings["max_pair_angle_deg"] >= 90 or settings["max_pair_gap_ratio"] < 1 or settings["max_box_scale_ratio"] <= 1:
        raise ValueError("Invalid geometry or box stability limits")
    if settings["stable_seconds"] >= settings["max_crossing_seconds"]:
        raise ValueError("Stable duration must be shorter than crossing timeout")
    if settings["footprint_inset_ratio"] >= .5:
        raise ValueError("footprint_inset_ratio must be less than .5")
    return settings


def double_line_rule_options(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.pop("version", None) != 1:
        raise ValueError("Double-line rule config requires version=1")
    validate_geometry_settings(data.pop("geometry", None))  # 幾何選項由 double_line_geometry_options 讀取
    settings = validate_rule_settings(data)
    return {f"double_{color}_line_crossing": {"settings": settings}
            for color in ("yellow", "white")}


def _distance(first, second):
    """Compare curves only over shared visible y support; infinity means no match."""
    a, b = np.asarray(first, dtype=float), np.asarray(second, dtype=float)
    a, b = a[np.argsort(a[:, 1])], b[np.argsort(b[:, 1])]
    low, high = max(a[0, 1], b[0, 1]), min(a[-1, 1], b[-1, 1])
    if high - low < max(4, .5 * min(np.ptp(a[:, 1]), np.ptp(b[:, 1]))):
        return float("inf")
    ys = np.linspace(low, high, 12)
    return float(np.max(np.abs(np.interp(ys, a[:, 1], a[:, 0]) -
                               np.interp(ys, b[:, 1], b[:, 0]))))


def _longest_narrow_block(rows, limit):
    """回傳列寬都不超過 limit 的最長連續列（等長時取較下方者）；不足 MIN_TRIMMED_ROWS 列時回傳 None。"""
    best, start = (0, 0), None
    for i, row in enumerate(rows + [None]):
        narrow = row is not None and row[1] - row[0] <= limit
        if narrow and start is None:
            start = i
        elif not narrow and start is not None:
            best = max(best, (i - start, start))
            start = None
    length, start = best
    return rows[start:start + length] if length >= MIN_TRIMMED_ROWS else None


def extract_double_lines(mask, markings, min_area=40, trim_wide_rows=False):
    """Extract row envelopes from the current segmentation, never from fitLine.

    Nearby, mutually unique components of the same semantic double-line class
    may form a pair. Unpaired components stay provisional; this is not proof
    that two individual painted stripes were visible.
    """
    if mask is None:
        return []
    mask = np.asarray(mask)
    if mask.ndim != 2:
        raise ValueError("Double-line mask must be a 2D class-id array")
    output = []
    for marking in markings:
        if marking.get("class_name") not in DOUBLE_LINE_CLASSES:
            continue
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            (mask == marking["class_id"]).astype(np.uint8), 8)
        strips = []
        for label in range(1, count):
            x, y, width, height, area = map(int, stats[label])
            if area < min_area or height < 8:
                continue
            crop = labels[y:y + height, x:x + width] == label
            present = crop.any(axis=1)
            first = crop.argmax(axis=1) + x
            last = x + width - 1 - crop[:, ::-1].argmax(axis=1)
            rows = [[float(first[r]), float(last[r]), y + r] for r in np.flatnonzero(present).tolist()]
            # Very broad/branching components are not usable line geometry.
            if max(right - left for left, right, _ in rows) > mask.shape[1] * WIDE_ROW_RATIO:
                if not trim_wide_rows:
                    continue
                # 4bg：只去掉過寬的列（元件仍是 8 連通，保留的列在 y 上連續）
                rows = _longest_narrow_block(rows, mask.shape[1] * WIDE_ROW_RATIO)
                if rows is None:
                    continue
            strips.append(rows)
        curves = [[[.5 * (left + right), y] for left, right, y in rows]
                  for rows in strips]
        neighbors = {i: [j for j in range(len(strips)) if j != i and
                        _distance(curves[i], curves[j]) <= mask.shape[1] * .035]
                     for i in range(len(strips))}
        used = set()
        for i, rows in enumerate(strips):
            if i in used:
                continue
            members = [i]
            if len(neighbors[i]) == 1:
                j = neighbors[i][0]
                if neighbors[j] == [i] and j not in used:
                    members.append(j)
            used.update(members)
            # Paired geometry is limited to the common visible interval.
            start = max(strips[j][0][2] for j in members)
            end = min(strips[j][-1][2] for j in members)
            envelopes = []
            for row in range(start, end + 1):
                values = [strips[j][row - strips[j][0][2]] for j in members]
                envelopes.append([min(v[0] for v in values), max(v[1] for v in values), row])
            step = max(1, mask.shape[0] // 180)
            sampled = envelopes[::step]
            if sampled[-1] != envelopes[-1]:
                sampled.append(envelopes[-1])
            output.append({"class_id": int(marking["class_id"]),
                           "class_name": marking["class_name"],
                           "centerline": [[(l + r) / 2, y] for l, r, y in sampled],
                           "left_boundary": [[l, y] for l, _, y in sampled],
                           "right_boundary": [[r, y] for _, r, y in sampled],
                           "bbox_xyxy": [min(v[0] for v in envelopes), start,
                                         max(v[1] for v in envelopes), end],
                           "component_count": len(members),
                           "members": [
                               {"centerline": [[(l + r) / 2, y] for l, r, y in strips[j]],
                                "left_boundary": [[l, y] for l, _, y in strips[j]],
                                "right_boundary": [[r, y] for _, r, y in strips[j]]}
                               for j in members],
                           "geometry_status": "paired" if len(members) == 2 else "provisional",
                           "source": "segmentation_mask", "rule_eligible": False})
    return output


def finite_line_position(point, centerline):
    """Return signed pixel distance to a visible segment, or None past its ends.

    Sign follows stored point order (top to bottom). This geometric measurement
    alone does not establish vehicle side, tire contact, or a crossing event.
    """
    points = np.asarray(centerline, dtype=float)
    point = np.asarray(point, dtype=float)
    if len(points) < 2:
        return None
    starts, vectors = points[:-1], np.diff(points, axis=0)
    lengths2 = np.sum(vectors * vectors, axis=1)
    fractions = np.sum((point - starts) * vectors, axis=1) / np.maximum(lengths2, 1e-9)
    candidates = np.flatnonzero(lengths2 > 0)
    if not len(candidates):
        return None
    clamped = np.clip(fractions, 0, 1)
    offsets = point - (starts + clamped[:, None] * vectors)
    index = min(candidates, key=lambda i: np.linalg.norm(offsets[i]))
    if (index == 0 and fractions[index] < 0) or (index == len(vectors) - 1 and fractions[index] > 1):
        return None
    signed = (vectors[index, 0] * offsets[index, 1] -
              vectors[index, 1] * offsets[index, 0]) / np.sqrt(lengths2[index])
    lengths = np.sqrt(lengths2)
    progress = float(lengths[:index].sum() + clamped[index] * lengths[index])
    return {"signed_distance_px": float(signed), "segment_index": int(index),
            "projection": (point - offsets[index]).tolist(),
            "tangent": (vectors[index] / lengths[index]).tolist(),
            "endpoint_distance_px": min(progress, float(lengths.sum()) - progress)}


def row_line_position(point, centerline):
    """以「同一影像列」量測點到可見線段的位置（垂足落在端點外時的後備）。

    透視下雙線常是大斜率的斜線，車底點若離線較遠，垂直投影會越過端點，即使車底所在的
    影像列明明有可見的雙線。此時改取該列上的線位置作為投影點，帶號距離仍沿局部法向量量測，
    與兩側邊界的計算方式（同列內插）一致。點的列不在線段範圍內時回傳 None。
    """
    points = np.asarray(centerline, dtype=float)
    x, y = (float(v) for v in point)
    if len(points) < 2 or not points[0, 1] < y < points[-1, 1] or np.any(np.diff(points[:, 1]) <= 0):
        return None
    index = int(np.clip(np.searchsorted(points[:, 1], y) - 1, 0, len(points) - 2))
    vector = points[index + 1] - points[index]
    length = float(np.hypot(*vector))
    if length == 0:
        return None
    foot = points[index] + vector * (y - points[index, 1]) / vector[1]
    offset = np.array([x, y]) - foot
    lengths = np.hypot(*np.diff(points, axis=0).T)
    progress = float(lengths[:index].sum() + np.hypot(*(foot - points[index])))
    return {"signed_distance_px": float((vector[0] * offset[1] - vector[1] * offset[0]) / length),
            "segment_index": index, "projection": foot.tolist(), "tangent": (vector / length).tolist(),
            "endpoint_distance_px": min(progress, float(lengths.sum()) - progress), "row_projection": True}


def line_quality(line, settings):
    """Recheck persisted geometry so replay thresholds apply to old observations."""
    semantic = line.get("component_count") == 1 and settings["allow_semantic_band"]
    if line.get("source") != "segmentation_mask" or (line.get("component_count") != 2 and not semantic):
        return "unverified_pair"
    if line.get("observed_frames", 0) < settings["min_line_observations"]:
        return "unstable_line"
    try:
        center = np.asarray(line["centerline"], dtype=float)
        members = line["members"]
        if len(members) != line["component_count"]:
            return "unverified_pair"
        paths = [center] + [np.asarray(line[key], dtype=float) for key in ("left_boundary", "right_boundary")]
        paths += [np.asarray(member[key], dtype=float) for member in members
                  for key in ("centerline", "left_boundary", "right_boundary")]
        if any(p.ndim != 2 or p.shape[1] != 2 or len(p) < 2 or not np.isfinite(p).all()
               or not np.all(np.diff(p[:, 1]) > 0) for p in paths):
            return "invalid_geometry"
        if np.linalg.norm(np.diff(center, axis=0), axis=1).sum() < settings["min_line_length_px"]:
            return "short_line"
        if any(p[0, 1] > center[0, 1] or p[-1, 1] < center[-1, 1] for p in paths):
            return "unsupported_geometry"
        if semantic:
            return None  # Semantic double-line band; cannot confirm two physical stripes.
        # Include all stored vertices: a sparse fixed grid can miss a local kink.
        ys = np.unique(np.concatenate([p[:, 1] for p in paths]))
        ys = ys[(ys >= center[0, 1]) & (ys <= center[-1, 1])]
        a, b = (np.asarray(m["centerline"], dtype=float) for m in members)
        ax, bx = np.interp(ys, a[:, 1], a[:, 0]), np.interp(ys, b[:, 1], b[:, 0])
        if ax.mean() > bx.mean():
            ax, bx, members = bx, ax, members[::-1]
        right = np.asarray(members[0]["right_boundary"], dtype=float)
        left = np.asarray(members[1]["left_boundary"], dtype=float)
        gap = np.interp(ys, left[:, 1], left[:, 0]) - np.interp(ys, right[:, 1], right[:, 0])
        if gap.min() < settings["min_pair_gap_px"] or gap.max() / gap.min() > settings["max_pair_gap_ratio"]:
            return "invalid_pair_gap"
        angles = np.abs(np.arctan(np.diff(ax) / np.diff(ys)) - np.arctan(np.diff(bx) / np.diff(ys)))
        if np.degrees(angles).max() > settings["max_pair_angle_deg"]:
            return "nonparallel_pair"
    except (KeyError, ValueError, TypeError, IndexError):
        return "invalid_geometry"
    return None


def project_line_geometry(line, matrix):
    """把一條雙線（含 members）的所有路徑以單應矩陣帶到目前影像座標；回傳新物件。

    投影後非單調或沒有共同支援時丟 ValueError。端點只修剪到共同範圍，不延伸。
    """
    anchor = clone(line)
    for part in [anchor] + anchor.get("members", []):
        for key in ("centerline", "left_boundary", "right_boundary"):
            part[key] = project_points(part[key], matrix)
            path = np.asarray(part[key])
            if np.any(np.diff(path[:, 1]) < -.5):
                raise ValueError("Non-monotone projection")
            # Pixel-quantized edges can swap adjacent rows by a fraction
            # of a pixel after homography; canonicalize that ordering.
            _, unique = np.unique(path[:, 1], return_index=True)
            part[key] = path[unique].tolist()
    # Perspective rotation moves boundary endpoints to different rows.
    # Trim all paths to shared support instead of extending endpoints.
    parts = [anchor] + anchor.get("members", [])
    paths = [np.asarray(part[key]) for part in parts
             for key in ("centerline", "left_boundary", "right_boundary")]
    low, high = max(p[0, 1] for p in paths), min(p[-1, 1] for p in paths)
    if high <= low:
        raise ValueError("No common projected support")
    for part in parts:
        for key in ("centerline", "left_boundary", "right_boundary"):
            p = np.asarray(part[key])
            ys = np.unique(np.r_[low, p[(p[:, 1] > low) & (p[:, 1] < high), 1], high])
            part[key] = np.column_stack((np.interp(ys, p[:, 1], p[:, 0]), ys)).tolist()
    return anchor


def resolve_rule_lines(lines, memory, matrix, frame, settings, vehicles=()):
    """Maintain finite observed support through matching collinear fragments.

    Never extend an anchor beyond its previously observed ends. Partial support
    expires even when fragments remain visible; events using it are suspected.
    Complete occlusion is bounded by a separate short timeout and requires
    overlap with a detected vehicle. Motion failure clears memory in the rule.
    """
    now = frame["timestamp_sec"]
    current = [clone(line) for line in lines if line.get("source_frame") == frame["index"]]
    anchors = []
    for old in memory.values():
        try:
            anchors.append(project_line_geometry(old, matrix))
        except (ValueError, KeyError):
            continue
    candidates = []
    for line in current:
        points = np.asarray(line["centerline"])
        length = float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
        matches = []
        if length >= settings["min_fragment_length_px"]:
            for index, anchor in enumerate(anchors):
                if anchor["class_name"] != line["class_name"]:
                    continue
                samples = points[np.linspace(0, len(points) - 1, min(len(points), 20)).astype(int)]
                distances = [finite_line_position(p, anchor["centerline"]) for p in samples]
                valid = [d for d in distances if d is not None]
                if (len(valid) >= .8 * len(samples) and
                        np.quantile([abs(d["signed_distance_px"]) for d in valid], .8) <= settings["fragment_match_distance_px"]):
                    matches.append(index)
        if len(matches) > 1:
            ranked = sorted(matches, key=lambda j: np.linalg.norm(
                np.diff(np.asarray(anchors[j]["centerline"]), axis=0), axis=1).sum(), reverse=True)
            lengths = [np.linalg.norm(np.diff(np.asarray(anchors[j]["centerline"]), axis=0), axis=1).sum()
                       for j in ranked]
            if lengths[0] > 1.5 * lengths[1]:
                matches = ranked[:1]  # Prefer the full finite anchor over its own short fragments.
        candidates.append((line, length, matches))
    output, consumed = [], set()
    for index, anchor in enumerate(anchors):
        matches = [(i, line, length) for i, (line, length, ids) in enumerate(candidates) if ids == [index]]
        if not matches:
            points = np.asarray(anchor["centerline"])
            covered = np.zeros(len(points), dtype=bool)
            for vehicle in vehicles:
                box = vehicle.get("bbox_xyxy", [])
                if len(box) == 4:
                    x1, y1, x2, y2 = box
                    covered |= ((points[:, 0] >= x1) & (points[:, 0] <= x2) &
                                (points[:, 1] >= y1) & (points[:, 1] <= y2))
            if (np.linalg.norm(np.diff(points, axis=0), axis=1).sum() >= settings["min_fragment_length_px"]
                    and covered.mean() >= .2 and now - anchor.get("last_visible_time", -1e9) <= settings["max_occluded_gap_sec"]
                    and now - anchor["support_since"] <= settings["max_partial_support_seconds"]):
                anchor.update(partial_support=True, source_frame=frame["index"], matched_fragment_ids=[])
                output.append(anchor)
            continue
        i, best, length = max(matches, key=lambda item: item[2])
        old_length = np.linalg.norm(np.diff(np.asarray(anchor["centerline"]), axis=0), axis=1).sum()
        if length >= .75 * old_length:
            result = clone(best)
            result["support_since"] = now
        elif now - anchor["support_since"] <= settings["max_partial_support_seconds"]:
            result = anchor
            result["partial_support"] = True
            # Correct accumulated road-registration drift using visible fragments.
            # Only normal displacement is corrected; observed finite ends are not extended.
            offsets = []
            for _, fragment, _ in matches:
                for point in fragment["centerline"][::max(1, len(fragment["centerline"]) // 20)]:
                    position = finite_line_position(point, anchor["centerline"])
                    if position and abs(position["signed_distance_px"]) <= settings["fragment_match_distance_px"]:
                        offsets.append(np.asarray(point) - position["projection"])
            if offsets:
                correction = np.median(offsets, axis=0)
                for part in [result] + result.get("members", []):
                    for key in ("centerline", "left_boundary", "right_boundary"):
                        part[key] = (np.asarray(part[key]) + correction).tolist()
        else:
            continue
        result.update(track_id=anchor["track_id"], observed_frames=anchor["observed_frames"] + 1,
                      source_frame=frame["index"], last_visible_time=now,
                      matched_fragment_ids=[m[1]["track_id"] for m in matches])
        # Record the last fully observed geometry separately from projection time.
        result.setdefault("geometry_source_frame", best["source_frame"])
        if not result.get("partial_support"):
            result["geometry_source_frame"] = frame["index"]
        output.append(result)
        consumed.update(m[0] for m in matches)
    for i, (line, _, _) in enumerate(candidates):
        if i not in consumed:
            # Use an independent identity after an expired partial anchor.
            line.update(track_id=f"{line['track_id']}@{frame['index']}", support_since=now,
                        geometry_source_frame=frame["index"], last_visible_time=now)
            output.append(line)
    memory.clear()
    for line in output:
        points = np.asarray(line["left_boundary"] + line["right_boundary"])
        line["bbox_xyxy"] = [*points.min(axis=0).tolist(), *points.max(axis=0).tolist()]
    memory.update({line["track_id"]: clone(line) for line in output})
    return output


def _extend_line(line, y_target, max_extension):
    """沿可見端點的方向，把中心線與兩側邊界外插到 y_target（只用於被車身遮住的端點）。

    以端點附近的直線外插。
    超出 max_extension 像素、或端點附近不足以估計方向時回傳 None。
    """
    paths = {key: np.asarray(line[key], dtype=float) for key in ("centerline", "left_boundary", "right_boundary")}
    top, bottom = paths["centerline"][0, 1], paths["centerline"][-1, 1]
    beyond = y_target - bottom if y_target > bottom else top - y_target
    if beyond <= 0:
        return line
    if beyond > max_extension:
        return None
    extended = dict(line)
    for key, path in paths.items():
        tail = path[-5:] if y_target > bottom else path[:5]
        if len(tail) < 2 or np.ptp(tail[:, 1]) < 1:
            return None
        slope, intercept = np.polyfit(tail[:, 1], tail[:, 0], 1)
        point = [[slope * y_target + intercept, y_target]]
        extended[key] = (np.vstack([path, point]) if y_target > bottom else np.vstack([point, path])).tolist()
    return extended


def _position_failure(detail, reason, line, settings, *, box=None, point=None,
                      original_line=None, position=None, bbox_reason=None, extrapolated=False):
    """只描述已作出的拒絕；距離沿影像 y 軸，比例分母為車框高度，不是建議外插量。"""
    if detail is None:
        return
    path = line.get("centerline", [])
    original = (original_line or line).get("centerline", [])
    ends = [list(map(float, p)) for p in (path[0], path[-1])] if len(path) >= 2 else None
    original_ends = [list(map(float, p)) for p in (original[0], original[-1])] if len(original) >= 2 else None
    height = float(box[3]-box[1]) if box is not None else None
    pad = settings["endpoint_margin_px"]+1
    gap = required = None
    if point is not None and original_ends:
        y, top, bottom = float(point[1]), original_ends[0][1], original_ends[1][1]
        gap = max(top-y, y-bottom, 0.)
        required = max(top-(y-pad), (y+pad)-bottom, 0.)
    detail.update(reason=reason, bbox_reason=bbox_reason,
                  failed_point=list(map(float, point)) if point is not None else None,
                  line_endpoints=ends, original_line_endpoints=original_ends,
                  endpoint_distance_px=float(position["endpoint_distance_px"]) if position is not None else None,
                  endpoint_margin_px=settings["endpoint_margin_px"], extrapolated=bool(extrapolated),
                  extension_limit_ratio=settings.get("max_extrapolation_ratio", 0),
                  extension_limit_px=settings.get("max_extrapolation_ratio", 0)*height+pad if height else None,
                  support_gap_px=gap, required_extension_px=required,
                  required_extension_ratio=required/height if required is not None and height else None,
                  ratio_denominator="bbox_height", distance_axis="image_y",
                  extension_policy="occluded_far_endpoint_only")


def write_final_double_line_diagnostics(stream, rules):
    """只在詳細診斷啟用時補寫片尾清除；不改原逐幀格式或事件。"""
    if stream is None:
        return
    final = {r.rule_id: r.final_diagnostics for r in rules
             if isinstance(getattr(r, "final_diagnostics", None), dict) and r.final_diagnostics}
    if final:
        stream.write(json.dumps({"frame": next(iter(final.values()))["frame"],
                                 "finalize": True, "rules": final}, ensure_ascii=False)+"\n")


def vehicle_line_position(vehicle, line, settings, motion_error=0, *, diagnostics=None):
    """Inset bottom-edge proxy must clear the entire band; not tire localization.

    車底超出雙線遠端可見端點、且該端點被車身遮住時，若設定允許，沿端點方向外插後再判斷，
    回傳值的 extrapolated 會標示為 True（事件因此只能是疑似）。
    """
    try:
        box = np.asarray(vehicle.get("bbox_xyxy", []), dtype=float)
    except (TypeError, ValueError):
        _position_failure(diagnostics, "invalid_bbox", line, settings, bbox_reason="conversion_failed")
        return None
    if box.shape != (4,) or not np.isfinite(box).all() or box[2] <= box[0] or box[3] <= box[1]:
        _position_failure(diagnostics, "invalid_bbox", line, settings, bbox_reason="invalid_shape_or_extent")
        return None
    x1, y1, x2, y2 = box
    original_line = line
    extrapolated = False
    ratio = settings.get("max_extrapolation_ratio", 0)
    if ratio > 0:
        center = np.asarray(line.get("centerline", []), dtype=float)
        if center.ndim == 2 and len(center) >= 2:
            # 只外插遠端（影像上方）端點，且該端點必須落在車框內：前車遮住其後方的雙線時，
            # 可見線段止於車身下緣附近。近端（影像下方）端點之外沒有遮擋者，代表雙線本來就從
            # 車輛前方才開始，不外插。
            margin = settings["endpoint_margin_px"] + 1
            top_x, top_y = center[0]
            width = x2 - x1  # 透視下車身側面也會遮住鄰近的雙線，水平方向放寬一個車寬
            occluded_by_vehicle = x1 - width <= top_x <= x2 + width and y1 <= top_y <= y2 + margin
            if occluded_by_vehicle and y2 < top_y + margin:
                target = y2 - margin
                extended = _extend_line(line, target, ratio * (y2 - y1) + margin)
                if extended is not None and extended is not line:
                    line, extrapolated = extended, True
    inset = (x2 - x1) * settings["footprint_inset_ratio"]
    points = [[x1 + inset, y2], [(x1 + x2) / 2, y2], [x2 - inset, y2]]
    sides, measurements, direction_sides = [], [], []
    direction_margin = settings.get("direction_side_margin_px", RULE_DEFAULTS["direction_side_margin_px"])
    for point in points:
        position = finite_line_position(point, line["centerline"])
        if position is None or position["endpoint_distance_px"] < settings["endpoint_margin_px"]:
            position = row_line_position(point, line["centerline"])
        if position is None or position["endpoint_distance_px"] < settings["endpoint_margin_px"]:
            if diagnostics is not None:
                path = line["centerline"]
                reason = ("endpoint_margin" if position is not None else
                          "outside_near_support" if point[1] > path[-1][1] else
                          "outside_far_support" if point[1] < path[0][1] else "unsupported_line_projection")
                _position_failure(diagnostics, reason, line, settings, box=box, point=point,
                                  original_line=original_line, position=position, extrapolated=extrapolated)
            return None
        foot = np.asarray(position["projection"])
        tangent = np.asarray(position["tangent"])
        normal = np.array([-tangent[1], tangent[0]])
        limits = []
        for key in ("left_boundary", "right_boundary"):
            path = np.asarray(line[key], dtype=float)
            boundary = [np.interp(foot[1], path[:, 1], path[:, 0]), foot[1]]
            limits.append(float(np.dot(np.asarray(boundary) - foot, normal)))
        low, high = sorted(limits)
        margin = settings["side_margin_px"] + (high - low) * settings["margin_width_ratio"] + motion_error
        distance = position["signed_distance_px"]
        sides.append(-1 if distance < low - margin else 1 if distance > high + margin else 0)
        if direction_margin:
            wide = direction_margin + (high - low) * settings.get(
                "direction_margin_width_ratio", RULE_DEFAULTS["direction_margin_width_ratio"]) + motion_error
            direction_sides.append(-1 if distance < low - wide else 1 if distance > high + wide else 0)
        measurements.append({**position, "in_band": low <= distance <= high})
    result = {"side": sides[0] if sides[0] == sides[1] == sides[2] else 0,
            "point": points[1], "center_in_band": measurements[1]["in_band"],
            "band_points": sum(m["in_band"] for m in measurements),  # 落在線帶本身（不含邊距）的車底點數
            "tangent": measurements[1]["tangent"], "box": box.tolist(), "extrapolated": extrapolated}
    if direction_margin:  # 4az：停用（0）時不加欄位
        result["direction_side"] = direction_sides[0] if direction_sides[0] == direction_sides[1] == direction_sides[2] else 0
    return result



def classify_maneuver(before, after, tangent, settings):
    """Infer maneuver from camera-compensated ground trajectories, not box heading.

    4az：direction_side_margin_px > 0 時只用離線帶夠遠的樣本（direction_clear），門檻改為 direction_min_samples。
    """
    vectors = []
    needed = settings["stable_observations"]
    if settings.get("direction_side_margin_px", RULE_DEFAULTS["direction_side_margin_px"]):
        before = [s for s in before if s.get("direction_clear")]
        after = [s for s in after if s.get("direction_clear")]
        needed = settings.get("direction_min_samples", RULE_DEFAULTS["direction_min_samples"])
    for samples in (before, after):
        if len(samples) < needed:
            return "complete_crossing", {"reason": "insufficient_direction_samples"}
        points = np.asarray([s["point"] for s in samples], dtype=float)
        vector = points[-1] - points[0]
        distance = np.linalg.norm(vector)
        travel = np.linalg.norm(np.diff(points, axis=0), axis=1).sum()
        if distance < settings["min_heading_displacement_px"] or distance / max(travel, 1e-6) < settings["heading_consistency"]:
            return "complete_crossing", {"reason": "unstable_or_small_displacement"}
        vectors.append(vector / distance)
    angle = float(np.degrees(np.arccos(np.clip(np.dot(*vectors), -1, 1))))
    longitudinal = [float(np.dot(vector, tangent)) for vector in vectors]
    aligned = min(map(abs, longitudinal)) >= settings["min_longitudinal_alignment"]
    maneuver = "complete_crossing"
    if aligned and longitudinal[0] * longitudinal[1] < 0 and angle >= settings["min_uturn_angle_deg"]:
        maneuver = "u_turn"
    elif aligned and longitudinal[0] * longitudinal[1] > 0 and angle <= settings["max_lane_change_angle_deg"]:
        maneuver = "lane_change"
    return maneuver, {"method": "camera_compensated_ground_trajectory", "angle_degrees": angle,
                      "longitudinal_directions": longitudinal, "inferred": True}


def attach_double_lines(record, mask, state, geometry=None):
    """Mutate the frame record and a caller-owned dict; retain observed tracks only.

    geometry：validate_geometry_settings() 的結果（None＝預設，等同 4bg 以前的行為）。

    Motion maps previous-frame coordinates into the current frame. Missing or
    ambiguous matches get new IDs; IDs are never reused within a video.
    """
    frame = record["frame"]
    observations = record["observations"]
    diagnostics = record.setdefault("postprocessing", {})
    motion = road_motion(record)
    reset_reason = None
    matrix = None
    motion_fallback = None
    if state.get("frame") is None:
        reset_reason = "first_frame"
    elif not is_continuous({"index": state["frame"], "timestamp_sec": state["time"],
                            "width": state["size"][0], "height": state["size"][1]}, frame, .2):
        reset_reason = "discontinuous_input"
    else:
        matrix = homography(motion) if motion.get("valid") else None
        if matrix is None:
            # 鏡頭運動估計失敗時不重設追蹤：以「鏡頭不動」配對，容許距離放寬（見下方）。
            # 雙線在相鄰幀間位移很小，用位置延續編號，比每次失敗都換新編號更能保留規則候選。
            motion_fallback = "motion_unavailable" if not motion.get("valid") else "invalid_motion"
    if mask is not None and np.asarray(mask).shape != (frame["height"], frame["width"]):
        mask, reset_reason = None, "mask_shape_mismatch"
    lines = extract_double_lines(mask, observations.get("road_markings_raw",
                                                          observations.get("road_markings", [])),
                                 trim_wide_rows=bool((geometry or {}).get("trim_wide_rows")))
    # 標線降頻時本幀遮罩可能是推移估計：observed_frames 仍計入（追蹤品質，行為不變），
    # direct_observed_frames 只計模型直接觀測，供規則的確認條件使用。
    evidence = marking_evidence(record)
    direct = bool(evidence["direct"])
    previous = [] if reset_reason else state.get("tracks", [])
    projected = []
    for track in previous:
        points = np.asarray(track["centerline"], dtype=float)
        if matrix is not None:
            homogeneous = np.column_stack((points, np.ones(len(points)))) @ matrix.T
            if np.any(np.abs(homogeneous[:, 2]) < 1e-6):
                continue
            points = homogeneous[:, :2] / homogeneous[:, 2:]
        if np.isfinite(points).all() and np.all(np.diff(points[:, 1]) > 0):
            projected.append((track, points))
    # 有運動補償時容許 1% 畫面寬；沒有時（鏡頭不動近似）容許 2.5%。
    tolerance = max(3, frame["width"] * (.01 if matrix is not None else .025))
    matches = [[j for j, (track, points) in enumerate(projected)
                if track["class_name"] == line["class_name"] and
                track["component_count"] == line["component_count"] and
                _distance(points, line["centerline"]) <= tolerance]
               for line in lines]
    if matrix is not None and previous and not all(matches):
        # 運動估計「有效」但尺度抖動時，近處的線可能投影偏離而配不上；對沒有配對的線，
        # 再以未補償的位置（2.5% 畫面寬）嘗試，命中時同樣標記為未經運動補償。
        raw_tracks = [(track, np.asarray(track["centerline"], dtype=float)) for track in previous]
        index = {id(track): j for j, (track, _) in enumerate(projected)}
        for i, line in enumerate(lines):
            if matches[i]:
                continue
            found = [index[id(track)] for track, points in raw_tracks
                     if id(track) in index and track["class_name"] == line["class_name"] and
                     track["component_count"] == line["component_count"] and
                     _distance(points, line["centerline"]) <= max(3, frame["width"] * .025)]
            if found:
                matches[i] = found
                line["_uncompensated_match"] = True
    for line, choices in zip(lines, matches):
        unique = len(choices) == 1 and sum(choices[0] in others for others in matches) == 1
        if unique:
            old = projected[choices[0]][0]
            line.update(track_id=old["track_id"], observed_frames=old["observed_frames"] + 1,
                        direct_observed_frames=old.get("direct_observed_frames", 0) + direct)
        else:
            state["next_id"] = state.get("next_id", 0) + 1
            line.update(track_id=f"double-line-{state['next_id']}", observed_frames=1,
                        direct_observed_frames=int(direct))
        line.update(geometry_source=marking_geometry_source(evidence),
                    marking_source_frame=evidence["source_frame"])
        uncompensated = motion_fallback or line.pop("_uncompensated_match", False)
        line.update(source_frame=frame["index"], tracking_status=(
            ("matched_without_motion" if uncompensated else "matched") if unique else "new"))
        line["rule_eligible"] = line_quality(line, RULE_DEFAULTS) is None
    observations["double_lines"] = lines
    diagnostics["double_lines"] = {
        "version": 1, "status": "observed" if lines else
        ("geometry_unavailable" if mask is None else "no_double_lines"),
        "reset_reason": reset_reason, "motion_fallback": motion_fallback, "observed_count": len(lines),
        "geometry_source": marking_geometry_source(evidence),
        "eligibility": "evaluated_by_rule",
        "motion_source": "ego_motion" if "ego_motion" in diagnostics else "road_marking_compensation"}
    state.update(frame=frame["index"], time=frame["timestamp_sec"],
                 size=[frame["width"], frame["height"]], tracks=clone(lines))


def draw_double_lines(frame, lines):
    for line in lines:
        color = (0, 220, 255) if line["class_name"] == "solid double yellow" else (255, 255, 255)
        points = np.rint(line["centerline"]).astype(np.int32)
        cv2.polylines(frame, [points], False, color, 2)
        cv2.putText(frame, line["track_id"], tuple(points[-1]),
                    cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1, cv2.LINE_AA)
    return frame
