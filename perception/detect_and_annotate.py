import argparse
import cv2
import os
import sys
import time
import re
import math
from collections import Counter
import numpy as np
import torch
from ultralytics import YOLO

# 🔇 關閉 ONNX Runtime 冗餘效能警告 (如 Memcpy nodes 提示)，保持終端輸出乾淨專注
os.environ["ORT_LOG_SEVERITY_LEVEL"] = "3"

# 🔧 若在 Windows 環境且支援 CUDA，將 PyTorch 內建之 CUDA DLL 加入搜尋路徑，供 ONNXRuntime-GPU 調用
if sys.platform == "win32" and torch.cuda.is_available():
    torch_lib = os.path.join(os.path.dirname(torch.__file__), 'lib')
    if hasattr(os, 'add_dll_directory') and os.path.isdir(torch_lib):
        try:
            os.add_dll_directory(torch_lib)
        except Exception:
            pass
    os.environ['PATH'] = torch_lib + os.pathsep + os.environ.get('PATH', '')

torch.set_num_threads(4)
# 🎯 偵測目標類別：行人 (0), 腳踏車 (1), 汽車 (2), 機車 (3), 公車 (5), 卡車 (7)
TARGET_CLASSES = [0, 1, 2, 3, 5, 7]
# 僅對具有車牌的車輛類別進行車牌裁切檢測 (排除行人與腳踏車)
PLATE_VEHICLE_CLASSES = [2, 3, 5, 7]

def filter_duplicate_boxes(boxes, iou_thresh=0.5):
    """
    對同一個影格內的車牌進行簡易 IoU 去重，避免相鄰重疊車輛重複檢測同一車牌。
    boxes: [ [x1, y1, x2, y2, conf, cls, track_id], ... ]
    """
    if len(boxes) <= 1:
        return boxes
    # 依信心度由高至低排序
    boxes = sorted(boxes, key=lambda b: b[4], reverse=True)
    kept = []
    for b in boxes:
        x1, y1, x2, y2 = b[:4]
        area1 = (x2 - x1) * (y2 - y1)
        overlap = False
        for k in kept:
            kx1, ky1, kx2, ky2 = k[:4]
            inter_x1 = max(x1, kx1)
            inter_y1 = max(y1, ky1)
            inter_x2 = min(x2, kx2)
            inter_y2 = min(y2, ky2)
            inter_w = max(0, inter_x2 - inter_x1)
            inter_h = max(0, inter_y2 - inter_y1)
            inter_area = inter_w * inter_h
            if inter_area > 0:
                area2 = (kx2 - kx1) * (ky2 - ky1)
                iou = inter_area / float(area1 + area2 - inter_area)
                if iou > iou_thresh:
                    overlap = True
                    break
        if not overlap:
            kept.append(b)
    return kept

def match_plate_to_vehicle(plate_box, vehicle_boxes):
    """
    將車牌框精準關聯至最合理的車輛框 (避免因裁切邊距或相鄰重疊造成的張冠李戴)。
    plate_box: [px1, py1, px2, py2, ...]
    vehicle_boxes: list of [vx1, vy1, vx2, vy2, conf, cls, track_id, dist_m]

    返回最佳車輛框 v_box 或 None
    """
    if not vehicle_boxes:
        return None

    px1, py1, px2, py2 = plate_box[:4]
    pcx = (px1 + px2) / 2.0
    pcy = (py1 + py2) / 2.0
    pw = max(1.0, px2 - px1)
    ph = max(1.0, py2 - py1)
    aspect = pw / ph

    candidates = []
    for v in vehicle_boxes:
        vx1, vy1, vx2, vy2 = v[:4]
        vcls = int(v[5]) if len(v) > 5 else 2
        vw = max(1.0, vx2 - vx1)
        vh = max(1.0, vy2 - vy1)

        # 1. 空間包含約束：
        # 水平方向：車牌中心必須在車輛寬度範圍內 (允許容差)
        # 機車騎士身軀窄且後座外送箱 (UberEats/Foodpanda)/貨架常向兩側外凸，放寬至 0.65 車寬
        tol_w = 0.65 * vw if vcls in (0, 3) else 0.08 * vw
        if not (vx1 - tol_w <= pcx <= vx2 + tol_w):
            continue

        # 垂直方向：車牌必須位於車身合理的下部/中下部 (排除車頂與擋風玻璃)
        # 對於機車/騎乘者 (cls in [0, 3])：車牌位於下部 (y1 + 0.25 * vh 到 y2 + 0.35 * vh)
        # 對於汽車/公車/卡車 (cls in [2, 5, 7])：y1 + 0.20 * vh 到 y2 + 0.15 * vh
        min_y_ratio = 0.25 if vcls in (0, 3) else 0.20
        max_y_ratio = 0.35 if vcls in (0, 3) else 0.15
        if not (vy1 + min_y_ratio * vh <= pcy <= vy2 + max_y_ratio * vh):
            continue

        # 2. 計算歸屬偏好評分 (Score 越小越優先匹配)：
        # - 面積懲罰：車輛框面積越小 (越緊湊包裹車牌)，越可能是真正的載體 (機車面積遠小於鄰近的大轎車)
        area = vw * vh
        # - 水平中心偏離懲罰：車牌越接近車輛水平中央，評分越好
        norm_dx = abs(pcx - (vx1 + vx2) / 2.0) / vw

        score = area * (1.0 + 3.0 * norm_dx)

        # - 形態學與類別先驗：
        # 台灣機車牌偏方正 (aspect 通常 < 2.1)，汽車牌偏長 (aspect 通常 >= 2.1)
        if aspect < 2.0:
            if vcls in (0, 3):  # 機車或機車騎士
                score *= 0.15   # 大幅優先匹配機車
            else:
                score *= 3.0
        elif aspect >= 2.3:
            if vcls in (0, 3):
                score *= 5.0    # 長條汽車牌極不可能屬於機車
            else:
                score *= 0.5

        candidates.append((score, v))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]

def fuse_rider_and_motorcycles(vehicle_boxes, rider_to_bike_history=None):
    """
    將同一輛機車與騎乘者 (person 與 motorcycle) 的偵測框融合成單一實體 (motorcycle)。
    徹底解決同車被切分為人與車兩個獨立 ID、導致車牌號碼歷史被拆分兩半的結構性問題。

    vehicle_boxes: list of [vx1, vy1, vx2, vy2, conf, cls, track_id, dist_m]
    rider_to_bike_history: 字典 {ptid: mtid}，跨影格記憶騎乘者所屬機車 ID
    返回:
        final_boxes: 融合後的車輛列表
        tid_mappings: 字典 {from_tid: to_tid}，紀錄哪些 person ID 被合併至 motorcycle ID
    """
    if not vehicle_boxes:
        return vehicle_boxes, {}

    persons = []
    motorcycles = []
    others = []

    for idx, vb in enumerate(vehicle_boxes):
        cls = int(vb[5]) if len(vb) > 5 else 2
        if cls == 0:
            persons.append((idx, vb))
        elif cls == 3:
            motorcycles.append((idx, vb))
        else:
            others.append(vb)

    if not persons and not motorcycles:
        return vehicle_boxes, {}

    matched_p_indices = set()
    fused_motorcycles = []
    tid_mappings = {}
    matched_m_tids = set()

    # 1. 空間幾何交集比對：將騎乘在同一輛機車上的騎士 (包含駕駛與後座乘客) 融合至該機車
    for m_idx, m_box in motorcycles:
        mx1, my1, mx2, my2 = m_box[:4]
        mconf = m_box[4]
        mtid = m_box[6]
        mdist = m_box[7] if len(m_box) > 7 else 0.0
        mw = max(1.0, mx2 - mx1)
        mh = max(1.0, my2 - my1)
        m_cy = (my1 + my2) / 2.0

        matched_p_for_m = []
        for p_idx, p_box in persons:
            if p_idx in matched_p_indices:
                continue
            px1, py1, px2, py2 = p_box[:4]
            pw = max(1.0, px2 - px1)
            ph = max(1.0, py2 - py1)
            p_cy = (py1 + py2) / 2.0

            # 騎乘者幾何約束：人重心通常高於機車重心 (若完全位於機車底部下方則排除)
            if p_cy > m_cy and py1 > my1 + 0.3 * mh:
                continue

            # 水平交集比例 (相對於騎士寬度與機車寬度)
            inter_x1 = max(mx1, px1)
            inter_x2 = min(mx2, px2)
            inter_w = max(0.0, inter_x2 - inter_x1)
            overlap_ratio = inter_w / min(mw, pw)

            # 垂直交集與垂直間隙
            inter_y1 = max(my1, py1)
            inter_y2 = min(my2, py2)
            inter_h = max(0.0, inter_y2 - inter_y1)
            y_gap = max(0.0, my1 - py2)

            # 騎乘狀態判斷：水平顯著交疊 (>= 35%) 且垂直方向交集或貼近 (間隙 < 車高 25%)
            if overlap_ratio >= 0.35 and (inter_h > 0 or y_gap < 0.25 * mh):
                matched_p_for_m.append((p_idx, p_box))

        if matched_p_for_m:
            for p_idx, _ in matched_p_for_m:
                matched_p_indices.add(p_idx)

            all_boxes = [m_box] + [p[1] for p in matched_p_for_m]
            fx1 = min(b[0] for b in all_boxes)
            fy1 = min(b[1] for b in all_boxes)
            fx2 = max(b[2] for b in all_boxes)
            fy2 = max(b[3] for b in all_boxes)
            fconf = max(b[4] for b in all_boxes)
            target_tid = mtid if mtid is not None else matched_p_for_m[0][1][6]

            for _, p_box in matched_p_for_m:
                ptid = p_box[6]
                if ptid is not None and target_tid is not None and ptid != target_tid:
                    tid_mappings[ptid] = target_tid
                if ptid is not None and target_tid is not None and rider_to_bike_history is not None:
                    rider_to_bike_history[ptid] = target_tid

            dists = [b[7] for b in all_boxes if len(b) > 7 and b[7] is not None and b[7] > 0]
            fdist = min(dists) if dists else mdist

            if target_tid is not None:
                matched_m_tids.add(target_tid)
            fused_motorcycles.append([fx1, fy1, fx2, fy2, fconf, 3, target_tid, fdist])
        else:
            if mtid is not None:
                matched_m_tids.add(mtid)
            fused_motorcycles.append(m_box)

    # 2. 時序記憶轉換：若某騎士之前已被確認騎乘某機車，但本幀機車主體短暫漏檢，自動延續為機車實體
    if rider_to_bike_history is not None:
        for p_idx, p_box in persons:
            if p_idx in matched_p_indices:
                continue
            ptid = p_box[6]
            if ptid is not None and ptid in rider_to_bike_history:
                assigned_mtid = rider_to_bike_history[ptid]
                if assigned_mtid not in matched_m_tids:
                    matched_p_indices.add(p_idx)
                    px1, py1, px2, py2 = p_box[:4]
                    pconf = p_box[4]
                    pdist = p_box[7] if len(p_box) > 7 else 0.0
                    ph = py2 - py1
                    # 向下延伸 25% 邊距涵蓋機車下盤
                    fused_motorcycles.append([px1, py1, px2, py2 + 0.25 * ph, pconf, 3, assigned_mtid, pdist])
                    matched_m_tids.add(assigned_mtid)

    # 3. 真正未騎乘機車的獨立行人 (如路旁行走行人) 保留原始 person 類別
    unmatched_persons = [p_box for p_idx, p_box in persons if p_idx not in matched_p_indices]

    final_boxes = others + fused_motorcycles + unmatched_persons
    return final_boxes, tid_mappings

class TaiwanPlateValidator:
    """
    台灣車牌語法校驗與字元消歧義修復器 (Taiwan License Plate Syntax Validator & Disambiguation)
    依據交通部公路局法規與公路監理號牌編碼規範：
    1. 支援主流 8 代新式汽機車 (3 英文 - 4 數字，如 ABC-1234, BXH-6208)
    2. 支援 8 代 / 7 代機車 (3 英文 - 3 數字，如 MAY-123, AAA-001)
    3. 支援 7 代舊式汽車 (前 2 代字 - 後 4 數字，如 AB-1234, 2R-1234, 22-1234)
    4. 支援 7 代舊式汽車反向 (前 4 數字 - 後 2 代字，如 1234-AB, 0001-DA, 0001-A2)
    5. 支援 7 代舊式營業車/計程車 (2 代字 - 3 數字 或 3 數字 - 2 代字，如 CA-001, 001-CA)
    6. 法規禁用字元約束：台灣號牌英文字軌全面禁用 'I' 與 'O'，徹底解決 1/I 與 0/O 混淆
    7. 字元位置先驗 (Positional Priors)：數字區段出現字母時進行確定性映射修復 (O/D->0, B->8, S->5, Z->2, I/L->1)
    8. 缺少連字號智慧還原：自動識別長度並插入 '-'
    """
    DIGIT_FIX = {
        'O': '0', 'D': '0', 'Q': '0',
        'I': '1', 'L': '1', 'J': '1',
        'Z': '2',
        'S': '5',
        'B': '8',
        'G': '6',
    }

    LETTER_FIX = {
        '8': 'B',
        '2': 'Z',
        '5': 'S',
        '0': 'D',
    }

    @classmethod
    def fix_digits(cls, s):
        return ''.join(cls.DIGIT_FIX.get(c, c) for c in s)

    @classmethod
    def fix_letters(cls, s):
        return ''.join(cls.LETTER_FIX.get(c, c) for c in s)

    @classmethod
    def validate_and_normalize(cls, raw_text):
        if not raw_text:
            return None
        # 轉大寫，並將常見分隔符號統一轉換為 '-'
        s = raw_text.upper().strip()
        s = re.sub(r'[\s·・._:—–]+', '-', s)
        s = re.sub(r'[^A-Z0-9-]', '', s).strip('-')
        if len(s) < 4:
            return None

        def check_candidate(cand):
            if not cand or '-' not in cand:
                return None
            parts = cand.split('-')
            if len(parts) != 2:
                return None
            p1, p2 = parts[0], parts[1]

            # 1. 第八代新式汽車 / 重機 / 白牌機車 (最主流 7 碼: 3 英文 - 4 數字，例: ABC-1234, BXH-6208)
            if len(p1) == 3 and len(p2) == 4:
                p1_c = cls.fix_letters(p1)
                p2_c = cls.fix_digits(p2)
                c = f"{p1_c}-{p2_c}"
                # 台灣字母無 I, O，此處嚴格檢查
                if re.match(r'^[A-HJ-NP-Z]{3}-[0-9]{4}$', c):
                    return c

            # 2. 第八代/第七代機車 (6 碼: 3 英文 - 3 數字 或 3 數字 - 3 英文)
            if len(p1) == 3 and len(p2) == 3:
                # 情況 A: 前 3 英文 - 後 3 數字 (例: MAY-123, AAA-001)
                p1_c = cls.fix_letters(p1)
                p2_c = cls.fix_digits(p2)
                c = f"{p1_c}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z]{3}-[0-9]{3}$', c):
                    return c
                # 情況 B: 前 3 數字 - 後 3 英文 (例: 589-HPG, 223-PNP)
                p1_d = cls.fix_digits(p1)
                p2_l = cls.fix_letters(p2)
                c_rev = f"{p1_d}-{p2_l}"
                if re.match(r'^[0-9]{3}-[A-HJ-NP-Z]{3}$', c_rev):
                    return c_rev

            # 3. 第七代舊式汽車 (6 碼: 前 2 碼代字 - 後 4 碼數字，例: AB-1234, 2R-1234)
            if len(p1) == 2 and len(p2) == 4:
                p2_c = cls.fix_digits(p2)
                c = f"{p1}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z0-9]{2}-[0-9]{4}$', c):
                    if re.search(r'[A-HJ-NP-Z]', p1) or p1 in ('22', '88', '66', '99'):
                        return c

            # 4. 第七代舊式汽車反向 (6 碼: 前 4 碼數字 - 後 2 碼代字，例: 1234-AB, 0001-DA, 0001-A2)
            if len(p1) == 4 and len(p2) == 2:
                p1_c = cls.fix_digits(p1)
                c = f"{p1_c}-{p2}"
                if re.match(r'^[0-9]{4}-[A-HJ-NP-Z0-9]{2}$', c):
                    if re.search(r'[A-HJ-NP-Z]', p2) or p2 in ('22', '88', '66', '99'):
                        return c

            # 5. 第七代舊式營業車 / 計程車 (5 碼: 2 碼代字 - 3 碼數字，例: CA-001, 2A-001)
            if len(p1) == 2 and len(p2) == 3:
                p2_c = cls.fix_digits(p2)
                c = f"{p1}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z0-9]{2}-[0-9]{3}$', c) and re.search(r'[A-HJ-NP-Z]', p1):
                    return c

            # 6. 第七代舊式營業車反向 (5 碼: 3 碼數字 - 2 碼代字，例: 001-CA, 001-2A)
            if len(p1) == 3 and len(p2) == 2:
                p1_c = cls.fix_digits(p1)
                c = f"{p1_c}-{p2}"
                if re.match(r'^[0-9]{3}-[A-HJ-NP-Z0-9]{2}$', c) and re.search(r'[A-HJ-NP-Z]', p2):
                    return c

            return None

        # 情境一：字串本身已有連字號 (或由空格、句點等符號轉換而來)
        if '-' in s:
            valid = check_candidate(s)
            if valid:
                return valid

        # 情境二：字串缺少連字號，按台灣號牌長度與字元先驗自動切割修復
        pure = s.replace('-', '')
        if len(pure) == 7:
            cand = f"{pure[:3]}-{pure[3:]}"
            valid = check_candidate(cand)
            if valid:
                return valid
            # 螺絲孔 / 邊緣陰影雜訊修復 (7 碼字串削去首尾雜訊嘗試 6 碼，如 J087-EPX -> 087-EPX)
            cand_trim_l = check_candidate(f"{pure[1:4]}-{pure[4:]}")
            if cand_trim_l:
                return cand_trim_l
            cand_trim_r = check_candidate(f"{pure[:3]}-{pure[3:-1]}")
            if cand_trim_r:
                return cand_trim_r
        elif len(pure) == 8:
            # 螺絲孔 / 邊緣陰影雜訊修復 (8 碼字串削去首尾雜訊嘗試 7 碼)
            cand_l = f"{pure[1:4]}-{pure[4:]}"
            valid = check_candidate(cand_l)
            if valid:
                return valid
            cand_r = f"{pure[:3]}-{pure[3:-1]}"
            valid = check_candidate(cand_r)
            if valid:
                return valid
        elif len(pure) == 6:
            # 優先嘗試完全自然匹配 (避免將 0380WH 誤切為 038-0WH 並被 fix_letters 強制改成 038-DWH)
            for c_str in (f"{pure[:4]}-{pure[4:]}", f"{pure[:2]}-{pure[2:]}", f"{pure[:3]}-{pure[3:]}"):
                p1, p2 = c_str.split('-')
                if len(p1) == 4 and len(p2) == 2 and p1.isdigit() and p2.isalpha():
                    if re.match(r'^[0-9]{4}-[A-HJ-NP-Z]{2}$', c_str):
                        return c_str
                if len(p1) == 2 and len(p2) == 4 and p1.isalpha() and p2.isdigit():
                    if re.match(r'^[A-HJ-NP-Z]{2}-[0-9]{4}$', c_str):
                        return c_str
                if len(p1) == 3 and len(p2) == 3:
                    if p1.isalpha() and p2.isdigit() and re.match(r'^[A-HJ-NP-Z]{3}-[0-9]{3}$', c_str):
                        return c_str
                    if p1.isdigit() and p2.isalpha() and re.match(r'^[0-9]{3}-[A-HJ-NP-Z]{3}$', c_str):
                        return c_str

            # 若無完全自然匹配，再按優先級別嘗試字元消歧義修復
            # 優先嘗試 3-3 (機車)
            valid = check_candidate(f"{pure[:3]}-{pure[3:]}")
            if valid:
                return valid
            # 次嘗試 2-4 (7代汽車)
            valid = check_candidate(f"{pure[:2]}-{pure[2:]}")
            if valid:
                return valid
            # 次嘗試 4-2 (7代汽車反向)
            valid = check_candidate(f"{pure[:4]}-{pure[4:]}")
            if valid:
                return valid
        elif len(pure) == 5:
            valid = check_candidate(f"{pure[:2]}-{pure[2:]}")
            if valid:
                return valid
            valid = check_candidate(f"{pure[:3]}-{pure[3:]}")
            if valid:
                return valid

        return None

class TaiwanLPRNetRecognizer:
    """
    專屬台灣車牌輕量化 LPRNet ONNX 推論引擎 (Pure ONNXRuntime-GPU/CPU):
    1. 針對台灣號牌專用 35 字元集 (0-9, A-Z，無 I/O) 端到端訓練之 CTC 識別模型。
    2. 超輕量模型 (1.25 MB, ~45 萬參數)，推論僅需 1~2 ms (比通用 OCR 快 10~20 倍)。
    3. 固定輸入尺寸 (94, 24)，支援原始影像與超解析對比增強雙路推論。
    4. 輸出介面相容 RapidOCR / PP-OCRv6：__call__(img) -> ([[text, conf]], 0.0)
    """
    CHARS = [
        '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
        'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K',
        'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V',
        'W', 'X', 'Y', 'Z', '-'
    ]
    BLANK_IDX = len(CHARS) - 1

    def __init__(self, model_path="checkpoints/taiwan_lprnet.onnx", device="cuda"):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.log_severity_level = 3
        providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if (device == 'cuda' and torch.cuda.is_available()) else ['CPUExecutionProvider']
        self.session = ort.InferenceSession(model_path, sess_options=so, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.img_size = (94, 24)

    def _infer_single(self, img):
        if img is None or img.size == 0 or img.shape[0] < 6 or img.shape[1] < 12:
            return None, 0.0
        
        resized = cv2.resize(img, self.img_size, interpolation=cv2.INTER_LINEAR)
        blob = (resized.astype(np.float32) - 127.5) * 0.0078125
        blob = np.transpose(blob, (2, 0, 1))[np.newaxis, ...]

        preds = self.session.run([self.output_name], {self.input_name: blob})[0]  # (1, 37, 18)
        logits = preds[0]  # (37, 18)

        # Softmax 機率分佈
        exp_logits = np.exp(logits - np.max(logits, axis=0, keepdims=True))
        probs = exp_logits / np.sum(exp_logits, axis=0, keepdims=True)

        pred_indices = np.argmax(logits, axis=0)  # (18,)
        decoded_chars = []
        char_confs = []
        pre_c = self.BLANK_IDX

        for t, c in enumerate(pred_indices):
            if c != self.BLANK_IDX and c != pre_c:
                decoded_chars.append(self.CHARS[c])
                char_confs.append(float(probs[c, t]))
            pre_c = c

        text = "".join(decoded_chars)
        avg_conf = float(np.mean(char_confs)) if char_confs else 0.0
        return text, avg_conf

    @staticmethod
    def _enhance_plate(img):
        """
        超解析雙三次插值 + 局部對比增強 (針對微小車牌與暗光補償)
        """
        if img is None or img.size == 0:
            return img
        h, w = img.shape[:2]
        if h < 36:
            scale = 36.0 / float(h)
            target_w = int(round(w * scale))
            up = cv2.resize(img, (target_w, 36), interpolation=cv2.INTER_CUBIC)
        else:
            up = img.copy()
        gaussian = cv2.GaussianBlur(up, (0, 0), 1.2)
        sharpened = cv2.addWeighted(up, 1.3, gaussian, -0.3, 0)
        lab = cv2.cvtColor(sharpened, cv2.COLOR_BGR2LAB)
        clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(4, 4))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    def predict(self, img):
        if img is None or img.size == 0 or img.shape[0] < 6 or img.shape[1] < 12:
            return None, 0.0

        orig_text, orig_conf = self._infer_single(img)
        orig_valid = TaiwanPlateValidator.validate_and_normalize(orig_text) if orig_text else None
        if orig_valid and orig_conf >= 0.75:
            return orig_text, orig_conf

        # 備援嘗試增強影像
        enh_img = self._enhance_plate(img)
        enh_text, enh_conf = self._infer_single(enh_img)
        enh_valid = TaiwanPlateValidator.validate_and_normalize(enh_text) if enh_text else None

        if enh_valid and not orig_valid:
            return enh_text, enh_conf
        if orig_valid and not enh_valid:
            return orig_text, orig_conf
        if enh_conf > orig_conf:
            return enh_text, enh_conf
        return orig_text, orig_conf

    def __call__(self, img, **kwargs):
        text, conf = self.predict(img)
        if text:
            return [[text, conf]], 0.0
        return None, 0.0

class ONNXPPOCRv6Recognizer:
    """
    PP-OCRv6 車牌純文字辨識 ONNX 推論引擎 (Pure ONNXRuntime-GPU/CPU):
    1. 採用 PaddleOCR 最新 PP-OCRv6 繁簡通用識別模型，字元集高達 18,710 字。
    2. 內建動態解析度前處理 (固定 H=48，動態 W 保持原始寬高比，雙線性插值，歸一化)。
    3. 英數字元先驗 Logits Masking：
       - 自動從 ONNX 詮釋資料 (Model Metadata) 讀取字元字典。
       - 車牌僅包含英數字元與連字號 ('0'-'9', 'A'-'Z', '-', blank)。
       - 在 CTC Greedy Decode 前將非英數之類別 logit 設為 -1e9。
       - 徹底杜絕中文、日文、特殊符號誤識別，大幅拉升車牌英數字信心度。
    4. 純 ONNXRuntime 執行，完全不需額外依賴外部雲端或龐大框架。
    """
    def __init__(self, model_path="checkpoints/PP-OCRv6_rec_small.onnx", device="cuda"):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.log_severity_level = 3
        providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if (device == 'cuda' and torch.cuda.is_available()) else ['CPUExecutionProvider']
        self.session = ort.InferenceSession(model_path, sess_options=so, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        
        meta = self.session.get_modelmeta().custom_metadata_map
        self.character = ['blank'] + meta['character'].splitlines() + [' ']
        self.allowed_indices = np.array([0] + [i for i, c in enumerate(self.character) if c in '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-'], dtype=np.int64)
        self.mask = np.full(len(self.character), -1e9, dtype=np.float32)
        self.mask[self.allowed_indices] = 0.0

    @staticmethod
    def _enhance_plate(img):
        """
        工業級車牌超解析自適應銳化與對比度增強 (Adaptive Plate Enhancement):
        1. 雙三次插值幾何放大 (Bicubic Upscaling):
           - 當車牌高度 h < 48px 時，雙三次插值放大至標準高度 48px，保留微小筆畫連續度。
        2. 雙邊保邊降噪 (Bilateral Filter):
           - 在銳化前先濾除行車記錄器常見之 H.264/JPEG 壓縮方塊雜點，避免雜訊被銳化放大為虛假筆畫。
        3. 自適應反遮罩銳化 (Adaptive Unsharp Mask):
           - 依據車牌清晰度 (Laplacian 方差) 動態調整銳化係數：
             - 嚴重模糊 (lap_var < 500)：強銳化 (係數 1.45, 模糊 -0.45)，拉大字元邊緣反差。
             - 中度模糊 (500 <= lap_var < 2500)：溫和銳化 (係數 1.25, 模糊 -0.25)。
             - 極度清晰 (lap_var >= 2500)：維持微調 (係數 1.10, 模糊 -0.10)，杜絕光暈 (Halo) 偽影。
        4. 車牌專用長條網格 CLAHE:
           - 轉至 LAB 色彩空間，採用 2x6 長寬比自適應網格強化字元與車牌底板局部對比度，克服大燈反光與逆光陰影。
        """
        if img is None or img.size == 0:
            return img
        h, w = img.shape[:2]
        if h < 48:
            scale = 48.0 / float(h)
            target_w = int(round(w * scale))
            up = cv2.resize(img, (target_w, 48), interpolation=cv2.INTER_CUBIC)
        else:
            up = img.copy()

        # 估算清晰度
        gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
        lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()

        # 雙邊濾波降噪 (抑制壓縮噪點，避免被銳化放大為虛假筆畫)
        smooth = cv2.bilateralFilter(up, d=5, sigmaColor=25, sigmaSpace=25)

        # 自適應銳化強度
        if lap_var < 500:
            blur = cv2.GaussianBlur(smooth, (0, 0), 1.2)
            sharpened = cv2.addWeighted(smooth, 1.45, blur, -0.45, 0)
        elif lap_var < 2500:
            blur = cv2.GaussianBlur(smooth, (0, 0), 1.0)
            sharpened = cv2.addWeighted(smooth, 1.25, blur, -0.25, 0)
        else:
            blur = cv2.GaussianBlur(smooth, (0, 0), 0.8)
            sharpened = cv2.addWeighted(smooth, 1.10, blur, -0.10, 0)

        # LAB 空間 CLAHE 強化局部亮度與對比度 (車牌長寬比自適應 2x6 網格)
        lab = cv2.cvtColor(sharpened, cv2.COLOR_BGR2LAB)
        clahe = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(2, 6))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        return enhanced

    def _infer_single(self, img):
        if img is None or img.size == 0 or img.shape[0] < 6 or img.shape[1] < 12:
            return None, 0.0
        h, w = img.shape[:2]
        ratio = w / float(h)
        img_h = 48
        base_w = int(math.ceil(img_h * ratio))
        # 確保寬度至少 120 像素，使 CTC 解碼器具備足夠時間步分離連續相同字元 (如 66, 88, 00)
        min_w = 120
        resized_w = max(min_w, min(base_w, 320))
        resized = cv2.resize(img, (resized_w, img_h), interpolation=cv2.INTER_LINEAR)
        resized = (resized.astype(np.float32) / 255.0 - 0.5) / 0.5
        norm_img = np.zeros((1, 3, img_h, resized_w), dtype=np.float32)
        norm_img[0] = resized.transpose((2, 0, 1))
        
        preds = self.session.run([self.output_name], {self.input_name: norm_img})[0]
        masked_preds = preds + self.mask
        pred_indices = masked_preds.argmax(axis=-1)[0]
        
        T = len(pred_indices)
        raw_probs = preds[0, np.arange(T), pred_indices]
        allowed_sums = preds[0][:, self.allowed_indices].sum(axis=1)
        renorm_probs = raw_probs / np.clip(allowed_sums, 1e-6, 1.0)
        
        text = []
        scores = []
        last_idx = 0
        for idx, rn_p in zip(pred_indices, renorm_probs):
            if idx != 0 and idx != last_idx:
                text.append(self.character[idx])
                scores.append(rn_p)
            last_idx = idx
            
        final_text = "".join(text)
        avg_score = float(np.mean(scores)) if scores else 0.0
        return final_text, avg_score

    @staticmethod
    def _rotate_crop(img, angle):
        h, w = img.shape[:2]
        center = (w / 2.0, h / 2.0)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        cos = np.abs(M[0, 0])
        sin = np.abs(M[0, 1])
        nw = int((h * sin) + (w * cos))
        nh = int((h * cos) + (w * sin))
        M[0, 2] += (nw / 2.0) - center[0]
        M[1, 2] += (nh / 2.0) - center[1]
        return cv2.warpAffine(img, M, (nw, nh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    def predict(self, img):
        if img is None or img.size == 0 or img.shape[0] < 6 or img.shape[1] < 12:
            return None, 0.0

        # 防護 2：優先使用超解析銳化 + CLAHE 增強之車牌影像推論
        enh_img = self._enhance_plate(img)
        enh_text, enh_conf = self._infer_single(enh_img)
        enh_valid = TaiwanPlateValidator.validate_and_normalize(enh_text) if enh_text else None
        
        # 若增強版已通過台灣車牌格式校驗且信心度高，直接返回
        if enh_valid and enh_conf >= 0.88:
            return enh_text, enh_conf

        # 備援嘗試原始原圖
        orig_text, orig_conf = self._infer_single(img)
        orig_valid = TaiwanPlateValidator.validate_and_normalize(orig_text) if orig_text else None
        if orig_valid and orig_conf >= 0.88:
            return orig_text, orig_conf

        # 綜合評估增強版與原始版之最佳結果
        if enh_valid and not orig_valid:
            best_text, best_conf, best_valid = enh_text, enh_conf, enh_valid
        elif orig_valid and not enh_valid:
            best_text, best_conf, best_valid = orig_text, orig_conf, orig_valid
        elif enh_conf >= orig_conf:
            best_text, best_conf, best_valid = enh_text, enh_conf, enh_valid
        else:
            best_text, best_conf, best_valid = orig_text, orig_conf, orig_valid

        best_score = best_conf + (1.0 if best_valid else 0.0)

        # 僅在未通過格式校驗或信心度偏低時，測試自適應傾角校正 (-14, 14, -8, 8)
        # 傾角校正基於增強版影像進行
        for angle in [-14, 14, -8, 8]:
            rot_img = self._rotate_crop(enh_img, angle)
            rot_text, rot_conf = self._infer_single(rot_img)
            if not rot_text or rot_conf < 0.40:
                continue
            rot_valid = TaiwanPlateValidator.validate_and_normalize(rot_text)
            rot_score = rot_conf + (1.0 if rot_valid else 0.0)
            if rot_score > best_score:
                best_score = rot_score
                best_text = rot_text
                best_conf = rot_conf
                if rot_valid and rot_conf >= 0.85:
                    break

        return best_text, best_conf

    def __call__(self, img, **kwargs):
        text, conf = self.predict(img)
        if text:
            return [[text, conf]], 0.0
        return None, 0.0

class PaddleInferPPOCRv6Recognizer:
    """
    PP-OCRv6 台灣車牌專屬微調推論引擎 (Paddle Inference GPU/CPU 獨立隔離進程):
    1. 採用經 1,930 張真實台灣車牌微調後之最優模型權重。
    2. 透過 Localhost Socket 與純淨 Paddle Inference 獨立進程通訊，徹底隔離 Windows 下 PyTorch 與 Paddle cuDNN DLL 衝突。
    3. 承襲英數字元 Logits Masking 與超解析銳化 + 多角度測試增強 (TTA)，推論僅需 ~9ms。
    """
    def __init__(self, model_dir="checkpoints/PP-OCRv6_taiwan_infer", dict_path="train_data/ppocrv6_dict.txt", device="cuda"):
        from ppocr_client import PaddleInferWorkerRecognizer
        self.worker = PaddleInferWorkerRecognizer(model_dir=model_dir, dict_path=dict_path, device=device)

    def predict(self, img):
        return self.worker.predict(img)

    def __call__(self, img, **kwargs):
        return self.worker(img, **kwargs)

    def close(self):
        if hasattr(self, 'worker') and self.worker:
            self.worker.close()

    def __del__(self):
        self.close()

class EasyOCRRecognizer:
    """
    EasyOCR 車牌文字辨識推論引擎 (PyTorch GPU / CPU - Ultralytics 官方方案):
    1. 採用 Ultralytics 官方影片推薦之 EasyOCR 架構。
    2. 內建台灣號牌字元白名單 (allowlist: 0-9, A-Z, -)，杜絕點號、逗號等特殊符號誤識。
    3. 支援雙排與多區塊文字自動聚合 (按垂直/水平坐標智慧排序拼接)。
    4. 支援超解析銳化 + LAB CLAHE 局部對比增強雙路推論。
    5. 介面完全相容：__call__(img, **kwargs) -> ([[text, conf]], 0.0)
    """
    def __init__(self, device="cuda"):
        import easyocr
        use_gpu = (device == "cuda" and torch.cuda.is_available())
        self.reader = easyocr.Reader(['en'], gpu=use_gpu)
        self.allowlist = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-'

    @staticmethod
    def _enhance_plate(img):
        """
        防護 2：超解析銳化 + CLAHE 局部自適應增強
        """
        if img is None or img.size == 0:
            return img
        h, w = img.shape[:2]
        if h < 48:
            scale = 48.0 / float(h)
            target_w = int(round(w * scale))
            up = cv2.resize(img, (target_w, 48), interpolation=cv2.INTER_CUBIC)
        else:
            up = img.copy()
        gaussian = cv2.GaussianBlur(up, (0, 0), 1.5)
        sharpened = cv2.addWeighted(up, 1.4, gaussian, -0.4, 0)
        lab = cv2.cvtColor(sharpened, cv2.COLOR_BGR2LAB)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    def _infer_single(self, img):
        if img is None or img.size == 0 or img.shape[0] < 6 or img.shape[1] < 12:
            return None, 0.0
        try:
            results = self.reader.readtext(
                img,
                allowlist=self.allowlist,
                paragraph=False,
                detail=1
            )
            if not results:
                return None, 0.0

            def get_box_center(b):
                pts = np.array(b[0])
                return np.mean(pts[:, 1]), np.mean(pts[:, 0])

            sorted_res = sorted(results, key=get_box_center)
            texts = []
            confs = []
            for item in sorted_res:
                t = re.sub(r'[^A-Z0-9-]', '', str(item[1]).upper().strip()).strip('-')
                if t:
                    texts.append(t)
                    confs.append(float(item[2]))

            if not texts:
                return None, 0.0

            merged_text = "-".join(texts) if len(texts) > 1 else texts[0]
            avg_conf = float(np.mean(confs)) if confs else 0.0
            return merged_text, avg_conf
        except Exception:
            return None, 0.0

    def predict(self, img):
        if img is None or img.size == 0 or img.shape[0] < 6 or img.shape[1] < 12:
            return None, 0.0

        # 防護 2：雙路推論 (優先超解析增強版，備援原圖版)
        enh_img = self._enhance_plate(img)
        enh_text, enh_conf = self._infer_single(enh_img)
        enh_valid = TaiwanPlateValidator.validate_and_normalize(enh_text) if enh_text else None

        if enh_valid and enh_conf >= 0.70:
            return enh_text, enh_conf

        orig_text, orig_conf = self._infer_single(img)
        orig_valid = TaiwanPlateValidator.validate_and_normalize(orig_text) if orig_text else None

        if enh_valid and not orig_valid:
            return enh_text, enh_conf
        if orig_valid and not enh_valid:
            return orig_text, orig_conf
        if enh_conf >= orig_conf:
            return enh_text, enh_conf
        return orig_text, orig_conf

    def __call__(self, img, **kwargs):
        text, conf = self.predict(img)
        if text:
            return [[text, conf]], 0.0
        return None, 0.0

class PlateOCRTracker:
    """
    車牌文字時序追蹤與快取管理器 (雙軌制：影片畫面顯示原版，外出資料採用台灣法規補償)：
    1. 結合車輛 track_id 進行抽樣辨識 (降頻避免每格推論)。
    2. 雙軌字串追蹤：
       - best_raw_text: 模型原汁原味辨識之車牌字串 (供影片標註框繪製)。
       - best_compensated_text: 經由台灣車牌法規檢驗、字元消歧義與連字號還原之標準字串 (供外出資料使用)。
    3. 採用字元級位置加權時序投票 (Positional Temporal Voting)，消除單格反光或模糊導致的局部字元錯誤。
    4. 車牌確認鎖定後 (Confirmed) 進入低頻複驗模式，大幅節省算力。
    """
    def __init__(self, interval=4, min_width=40, conf_thresh=0.50, max_dist=25.0, enable_taiwan_filter=True):
        self.interval = interval
        self.min_width = min_width
        self.conf_thresh = conf_thresh
        self.max_dist = max_dist
        self.enable_taiwan_filter = enable_taiwan_filter
        self.records = {}

    @staticmethod
    def assemble_composite_plate(history):
        """
        時序字元級位置加權多數決投票 (Positional Character-Level Voting):
        從歷史追蹤記錄 [(text, conf), ...] 中，按字串長度分群，
        並對各個字元位置進行信心度加權投票，組裝出抗單幀抖動與反光之最佳複合字串。
        """
        if not history:
            return None, 0.0
        
        valid = []
        for text, conf in history:
            cleaned = re.sub(r'[^A-Z0-9]', '', str(text).upper())
            if 5 <= len(cleaned) <= 8:
                valid.append((cleaned, float(conf)))
                
        if not valid:
            return history[-1][0], float(history[-1][1])
            
        lens = Counter([len(v[0]) for v in valid])
        target_len, _ = lens.most_common(1)[0]
        
        target_strings = [v for v in valid if len(v[0]) == target_len]
        
        composite_chars = []
        composite_confs = []
        for pos in range(target_len):
            pos_chars = [v[0][pos] for v in target_strings]
            pos_confs = [v[1] for v in target_strings]
            weights = {}
            for c, conf in zip(pos_chars, pos_confs):
                weights[c] = weights.get(c, 0.0) + conf
            best_char = max(weights.items(), key=lambda x: x[1])[0]
            composite_chars.append(best_char)
            char_confs = [conf for c, conf in zip(pos_chars, pos_confs) if c == best_char]
            composite_confs.append(sum(char_confs) / len(char_confs))
            
        assembled = "".join(composite_chars)
        avg_conf = sum(composite_confs) / len(composite_confs) if composite_confs else 0.0
        return assembled, avg_conf

    def should_infer(self, track_id, current_frame, plate_w, dist_m=None):
        if dist_m is not None and dist_m > self.max_dist:
            return False
        if track_id is None:
            return plate_w >= self.min_width

        rec = self.records.get(track_id)
        # 🎯 動態門檻 (Motion-Aware Resolution Gating):
        # - 若車輛正在「遠離中 (receding)」，寬度會逐格變小，放寬門檻至 45px 搶拍免得漏失
        # - 若車輛「接近中 (approaching)」或初次出現，使用標準黃金門檻 (預設 58px) 等待最佳畫質
        min_w = self.min_width
        if rec is not None:
            prev_w = rec.get("last_plate_w", None)
            if prev_w is not None and plate_w < prev_w and plate_w >= 45:
                min_w = 45

        if plate_w < min_w:
            return False

        if not rec:
            return True
        if rec.get("confirmed", False):
            # 已確認車牌，每 30 幀才抽檢複驗一次
            return (current_frame - rec.get("last_infer_frame", 0)) >= 30
        return (current_frame - rec.get("last_infer_frame", 0)) >= self.interval

    def update(self, track_id, current_frame, raw_text, conf, veh_cls=None, plate_w=None):
        if not raw_text or conf < self.conf_thresh:
            return None, 0.0

        # 1. 原始版 (Raw)：僅轉大寫、去空白與非英數符號，維持模型原本辨識輸出
        raw_clean = re.sub(r'[^A-Z0-9-]', '', raw_text.upper().strip()).strip('-')
        if len(raw_clean) < 4:
            return None, 0.0

        # 2. 補償版 (Compensated)：經由台灣車牌法規檢驗、字元消歧義與連字號還原
        if self.enable_taiwan_filter:
            compensated_clean = TaiwanPlateValidator.validate_and_normalize(raw_clean)
        else:
            compensated_clean = raw_clean

        if track_id is None:
            return raw_clean, conf

        # 檢驗車輛類別相容性與時序斷點 (防止 ByteTrack 重新循環使用 track_id 造成機車車牌繼承給汽車)
        if track_id in self.records:
            rec = self.records[track_id]
            rec_cls = rec.get("veh_cls")
            if rec_cls is not None and veh_cls is not None:
                is_rec_2w = rec_cls in (0, 1, 3)
                is_cur_2w = veh_cls in (0, 1, 3)
                if is_rec_2w != is_cur_2w:
                    del self.records[track_id]
            elif (current_frame - rec.get("last_infer_frame", 0)) > 45:
                del self.records[track_id]

        if track_id not in self.records:
            self.records[track_id] = {
                "raw_history": [],
                "compensated_history": [],
                "confirmed": False,
                "best_raw_text": raw_clean,
                "best_compensated_text": compensated_clean or raw_clean,
                "best_conf": conf,
                "last_infer_frame": current_frame,
                "veh_cls": veh_cls,
                "last_plate_w": plate_w
            }
        rec = self.records[track_id]
        rec["last_infer_frame"] = current_frame
        rec["veh_cls"] = veh_cls
        rec["last_plate_w"] = plate_w

        # 🛡️ 車牌確認鎖定與防遮擋保護 (Confirmed Plate Freeze Protection):
        # 若該車輛已被鎖定為合規台灣車牌 (如 AUY-6695)，任何低信心、殘字或短暫遮擋讀數不得破壞已確認車牌
        if rec.get("confirmed", False):
            confirmed_comp = rec.get("best_compensated_text")
            # 狀況 1：新讀數經過法規校驗後與已確認車牌相同，維持鎖定並更新最高信心
            if compensated_clean and compensated_clean == confirmed_comp:
                rec["best_conf"] = max(rec["best_conf"], conf)
                rec["pending_count"] = 0
                return rec["best_raw_text"], rec["best_conf"]

            # 狀況 2：新讀數為無法通過台灣法規校驗之殘字 (如遮擋導致的 UY6695, ALIY6695, PC9668)
            # 嚴格禁止寫入歷史，杜絕污染已確認車牌
            if not compensated_clean:
                return rec["best_raw_text"], rec["best_conf"]

            # 狀況 3：新讀數通過台灣法規校驗，但與確認車牌不同 (可能發生於追蹤目標切換)
            # 必須連續出現 >= 4 次相同的新高信心車牌 (conf >= 0.88)，才允許覆蓋已確認車牌
            if rec.get("pending_new_plate") == compensated_clean:
                rec["pending_count"] = rec.get("pending_count", 0) + 1
            else:
                rec["pending_new_plate"] = compensated_clean
                rec["pending_count"] = 1

            if rec["pending_count"] >= 4 and conf >= 0.88:
                rec["confirmed"] = True
                rec["best_compensated_text"] = compensated_clean
                rec["best_raw_text"] = compensated_clean
                rec["best_conf"] = conf
                rec["raw_history"] = [(raw_clean, conf)]
                rec["compensated_history"] = [(compensated_clean, conf)]
                rec["pending_count"] = 0

            return rec["best_raw_text"], rec["best_conf"]

        rec["raw_history"].append((raw_clean, conf))
        if len(rec["raw_history"]) > 12:
            rec["raw_history"] = rec["raw_history"][-12:]

        if compensated_clean:
            rec["compensated_history"].append((compensated_clean, conf))
            if len(rec["compensated_history"]) > 12:
                rec["compensated_history"] = rec["compensated_history"][-12:]

        # 1. 原始版字元級時序投票 (供影片畫面標註)
        composite_raw, composite_raw_conf = self.assemble_composite_plate(rec["raw_history"])
        rec["best_raw_text"] = composite_raw or raw_clean
        rec["best_conf"] = composite_raw_conf or conf

        # 2. 補償版多數決投票與規則消歧義 (供外出資料使用)
        if rec["compensated_history"]:
            counts_comp = Counter([h[0] for h in rec["compensated_history"]])
            most_common_comp, freq_comp = counts_comp.most_common(1)[0]
            matching_confs = [h[1] for h in rec["compensated_history"] if h[0] == most_common_comp]
            avg_conf = sum(matching_confs) / len(matching_confs)
            max_conf = max(matching_confs)

            rec["best_compensated_text"] = most_common_comp
            rec["best_conf"] = avg_conf

            # 鎖定條件：同字串累計出現 >= 3 次且平均信心度 >= 0.70，或出現 >= 2 次且最高信心度 >= 0.88
            if (freq_comp >= 3 and avg_conf >= 0.70) or (freq_comp >= 2 and max_conf >= 0.88):
                rec["confirmed"] = True
                rec["best_raw_text"] = most_common_comp
        else:
            # 若尚無完整合規字串，嘗試對複合原始字串進行語法修復
            cand_from_comp = TaiwanPlateValidator.validate_and_normalize(composite_raw) if composite_raw else None
            if cand_from_comp:
                rec["best_compensated_text"] = cand_from_comp
            else:
                rec["best_compensated_text"] = composite_raw
            rec["best_conf"] = composite_raw_conf

        return rec["best_raw_text"], rec["best_conf"]

    def get_plate_raw(self, track_id, veh_cls=None):
        """取得原始未補償的車牌文字 (供影片畫面標註繪製)"""
        if track_id is not None and track_id in self.records:
            rec = self.records[track_id]
            rec_cls = rec.get("veh_cls")
            if rec_cls is not None and veh_cls is not None:
                if (rec_cls in (0, 1, 3)) != (veh_cls in (0, 1, 3)):
                    return None, 0.0
            return rec["best_raw_text"], rec["best_conf"]
        return None, 0.0

    def get_plate(self, track_id, veh_cls=None):
        """取得經由台灣法規補償與消歧義修復後的車牌文字 (供外出資料/下游組員/API使用)"""
        if track_id is not None and track_id in self.records:
            rec = self.records[track_id]
            rec_cls = rec.get("veh_cls")
            if rec_cls is not None and veh_cls is not None:
                if (rec_cls in (0, 1, 3)) != (veh_cls in (0, 1, 3)):
                    return None, 0.0
            return rec["best_compensated_text"], rec["best_conf"]
        return None, 0.0

    def get_all_confirmed(self):
        """取得所有已確認/最佳之合規車牌字典 (供終端報告與外出資料匯出)"""
        return {
            tid: (data["best_compensated_text"], data["best_conf"])
            for tid, data in self.records.items()
            if data.get("best_compensated_text")
        }

    def merge_tracks(self, from_tid, to_tid):
        """
        將 from_tid (例如騎士) 的 OCR 辨識歷史與鎖定狀態合併至 to_tid (例如機車)。
        徹底杜絕同車不同 ID 分割車牌歷史的問題。
        """
        if from_tid is None or to_tid is None or from_tid == to_tid:
            return
        if from_tid not in self.records:
            return

        from_rec = self.records[from_tid]
        if to_tid not in self.records:
            self.records[to_tid] = from_rec
            self.records[to_tid]["veh_cls"] = 3
            del self.records[from_tid]
            return

        to_rec = self.records[to_tid]
        to_rec["veh_cls"] = 3

        # 合併歷史記錄 (按時間保留最近 16 筆)
        to_rec["raw_history"].extend(from_rec.get("raw_history", []))
        if len(to_rec["raw_history"]) > 16:
            to_rec["raw_history"] = to_rec["raw_history"][-16:]

        to_rec["compensated_history"].extend(from_rec.get("compensated_history", []))
        if len(to_rec["compensated_history"]) > 16:
            to_rec["compensated_history"] = to_rec["compensated_history"][-16:]

        # 若 from_rec 已經確認 (confirmed) 而 to_rec 尚未確認，優先繼承確認狀態
        if from_rec.get("confirmed", False) and not to_rec.get("confirmed", False):
            to_rec["confirmed"] = True
            to_rec["best_raw_text"] = from_rec.get("best_raw_text")
            to_rec["best_compensated_text"] = from_rec.get("best_compensated_text")
            to_rec["best_conf"] = max(to_rec.get("best_conf", 0.0), from_rec.get("best_conf", 0.0))
        elif not to_rec.get("confirmed", False):
            # 重新計算複合多數決時序投票
            composite_raw, composite_raw_conf = self.assemble_composite_plate(to_rec["raw_history"])
            if composite_raw:
                to_rec["best_raw_text"] = composite_raw
                to_rec["best_conf"] = max(to_rec.get("best_conf", 0.0), composite_raw_conf)
            if to_rec["compensated_history"]:
                counts_comp = Counter([h[0] for h in to_rec["compensated_history"]])
                most_common_comp, freq_comp = counts_comp.most_common(1)[0]
                matching_confs = [h[1] for h in to_rec["compensated_history"] if h[0] == most_common_comp]
                avg_conf = sum(matching_confs) / len(matching_confs)
                max_conf = max(matching_confs)
                to_rec["best_compensated_text"] = most_common_comp
                if (freq_comp >= 3 and avg_conf >= 0.70) or (freq_comp >= 2 and max_conf >= 0.88):
                    to_rec["confirmed"] = True
                    to_rec["best_raw_text"] = most_common_comp
        else:
            # to_rec 已經 confirmed，更新最高信心度
            to_rec["best_conf"] = max(to_rec.get("best_conf", 0.0), from_rec.get("best_conf", 0.0))

        to_rec["last_infer_frame"] = max(to_rec.get("last_infer_frame", 0), from_rec.get("last_infer_frame", 0))
        del self.records[from_tid]

class ONNXDepthAnythingV2:
    """
    純 ONNXRuntime-GPU/CPU 深度推論引擎，完全擺脫 Depth-Anything-V2 原始碼資料夾依賴。
    """
    def __init__(self, model_path, device='cuda'):
        import onnxruntime as ort
        if device == 'cuda':
            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        else:
            providers = ['CPUExecutionProvider']
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.session = ort.InferenceSession(model_path, sess_options=so, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

    def image2tensor(self, raw_image, input_size=392):
        img = cv2.cvtColor(raw_image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        h, w = raw_image.shape[:2]
        scale = input_size / min(h, w)
        th = int(round(h * scale / 14.0)) * 14
        tw = int(round(w * scale / 14.0)) * 14
        resized = cv2.resize(img, (tw, th), interpolation=cv2.INTER_CUBIC)
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        norm = (resized - mean) / std
        chw = np.transpose(norm, (2, 0, 1))[np.newaxis, ...]
        return chw, (h, w)

    def infer_image(self, raw_image, input_size=392):
        tensor, (h, w) = self.image2tensor(raw_image, input_size)
        depth_out = self.session.run([self.output_name], {self.input_name: tensor})[0]
        depth_map = cv2.resize(depth_out[0], (w, h), interpolation=cv2.INTER_LINEAR)
        return depth_map

def draw_boxes(img, boxes, names, color, label_prefix="", vehicle_plate_map=None):
    for box in boxes:
        x1, y1, x2, y2 = map(int, box[:4])
        conf = box[4]
        cls = int(box[5]) if len(box) > 5 else 0
        track_id = int(box[6]) if len(box) > 6 and box[6] is not None else None
        
        # 依據標註類別分別解析附加欄位 (避免車輛距離與車牌文字欄位索引混淆)
        if label_prefix and label_prefix.lower().startswith("plate"):
            dist_m = None
            plate_text = str(box[7]) if len(box) > 7 and box[7] is not None else None
            text_conf = float(box[8]) if len(box) > 8 and box[8] is not None else None
        else:
            dist_m = float(box[7]) if len(box) > 7 and box[7] is not None else None
            plate_text = None
            text_conf = None
        
        class_name = names.get(cls, str(cls))
        dist_str = f" [{dist_m:.1f}m]" if (dist_m is not None and dist_m > 0) else ""
        
        # 車輛框若已辨識出車牌文字，亦顯示在車輛標籤中 (例: car #1 [14.2m] [ABC-1234]: 0.85)
        v_plate_str = ""
        if vehicle_plate_map and track_id is not None and track_id in vehicle_plate_map:
            val = vehicle_plate_map[track_id]
            if isinstance(val, tuple):
                p_text, p_cls = val
                if p_cls is None or (p_cls in (0, 1, 3)) == (cls in (0, 1, 3)):
                    v_plate_str = f" [{p_text}]"
            else:
                v_plate_str = f" [{val}]"
        
        box_color = color
        if plate_text:
            # 車牌文字成功辨識：使用醒目的亮綠色
            box_color = (0, 220, 0)
            if text_conf and text_conf > 0:
                display_text = f"{plate_text}: {text_conf:.2f}"
            else:
                display_text = f"{plate_text}"
        elif track_id is not None:
            if label_prefix:
                display_text = f"{label_prefix} #{track_id}: {conf:.2f}"
            else:
                display_text = f"{class_name} #{track_id}{dist_str}{v_plate_str}: {conf:.2f}"
        else:
            display_text = f"{label_prefix}: {conf:.2f}" if label_prefix else f"{class_name}{dist_str}: {conf:.2f}"
        
        cv2.rectangle(img, (x1, y1), (x2, y2), box_color, 2)
        (w, h), _ = cv2.getTextSize(display_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img, (x1, max(0, y1 - 20)), (x1 + w + 6, max(0, y1)), box_color, -1)
        cv2.putText(img, display_text, (x1 + 3, max(14, y1 - 5)), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    return img

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    def find_default(rel_path):
        p1 = os.path.join(base_dir, rel_path)
        if os.path.exists(p1):
            return p1
        p2 = os.path.abspath(rel_path)
        if os.path.exists(p2):
            return p2
        return p1

    parser = argparse.ArgumentParser(description="兩階段車輛追蹤與車牌辨識影片標註系統")
    parser.add_argument("--video", type=str, default="vid2.mp4", help="輸入影片路徑")
    parser.add_argument("--output", type=str, default="annotated_output.mp4", help="輸出影片路徑")
    parser.add_argument("--conf-vehicle", type=float, default=0.25, help="車輛偵測信心門檻")
    parser.add_argument("--conf-plate", type=float, default=0.30, help="車牌偵測信心門檻 (兩階段裁切建議 0.30~0.35，徹底杜絕水箱罩/飾條誤檢)")
    parser.add_argument("--imgsz-vehicle", type=int, default=640, help="車輛推論解析度 (640 可維持極高速度)")
    parser.add_argument("--imgsz-plate", type=int, default=320, help="車牌推論解析度 (局部裁切模式下建議 320，速度提升 3~4 倍且精度無損)")
    parser.add_argument("--two-stage", dest="two_stage", action="store_true", default=True, help="啟用兩階段車輛局部裁切車牌偵測 (預設開啟)")
    parser.add_argument("--no-two-stage", dest="two_stage", action="store_false", help="關閉兩階段，使用舊版全圖車牌偵測")
    parser.add_argument("--top1-per-vehicle", dest="top1_per_vehicle", action="store_true", default=True, help="每輛車只保留最高信心度的一個車牌 (預設開啟，杜絕一車多框與框亂跳)")
    parser.add_argument("--no-top1", dest="top1_per_vehicle", action="store_false", help="允許每輛車出現多個車牌框")
    parser.add_argument("--smooth", dest="smooth", action="store_true", default=True, help="啟用跨影格時序平滑追蹤 (預設開啟，徹底解決車牌框每格抖動與瞬移)")
    parser.add_argument("--no-smooth", dest="smooth", action="store_false", help="關閉時序平滑追蹤")
    parser.add_argument("--smooth-alpha", type=float, default=0.65, help="平滑移動加權係數 (0.1~0.9，預設 0.65)")
    parser.add_argument("--crop-padding", type=float, default=0.08, help="車輛裁切邊距外擴比例 (預設 8%%)")
    parser.add_argument("--min-vehicle-size", type=int, default=50, help="車輛最小像素大小 (寬或高低於此值則略過車牌檢測)")
    parser.add_argument("--max-frames", type=int, default=0, help="最多處理影格數 (0 代表處理整部影片)")
    parser.add_argument("--batch-size", type=int, default=8, help="GPU 影格 Batch 大小")
    parser.add_argument("--crop-batch-size", type=int, default=16, help="車輛裁切塊 GPU Batch 大小")
    parser.add_argument("--device", type=str, default="cuda", help="推理裝置 (cuda 或 cpu)")
    default_plate = find_default("checkpoints/license-plate-finetune-v1s.onnx") if os.path.isfile(find_default("checkpoints/license-plate-finetune-v1s.onnx")) else find_default("license-plate-finetune-v1s.pt")
    default_vehicle = find_default("checkpoints/yolo26s.onnx") if os.path.isfile(find_default("checkpoints/yolo26s.onnx")) else find_default("yolo26s.pt")
    parser.add_argument("--plate-model", type=str, default=default_plate, help="車牌模型路徑 (.onnx 或 .pt)")
    parser.add_argument("--vehicle-model", type=str, default=default_vehicle, help="車輛模型路徑 (.onnx 或 .pt)")
    parser.add_argument("--enable-depth", dest="enable_depth", action="store_true", default=True, help="啟用 Depth Anything V2 Small 深度公尺測距 (預設開啟)")
    parser.add_argument("--no-depth", dest="enable_depth", action="store_false", help="關閉深度測距")
    default_depth = find_default("checkpoints/depth_anything_v2_metric_vkitti_vits.onnx")
    parser.add_argument("--depth-ckpt", type=str, default=default_depth, help="Depth Anything V2 權重路徑 (.onnx)")
    parser.add_argument("--depth-size", type=int, default=392, help="Depth 模型輸入尺寸 (266 極速, 392 平衡, 518 高精度)")
    parser.add_argument("--depth-interval", type=int, default=2, help="深度測距推論間隔影格數 (預設 2，即每隔 2 幀推論一次，中間格沿用前幀深度，大幅提速 40%%)")
    parser.add_argument("--enable-ocr", dest="enable_ocr", action="store_true", default=True, help="啟用車牌字元辨識 (預設開啟)")
    parser.add_argument("--no-ocr", dest="enable_ocr", action="store_false", help="關閉車牌字元辨識")
    parser.add_argument("--ocr-engine", type=str, choices=["ppocr", "easyocr", "lprnet"], default="ppocr", help="車牌辨識引擎 (預設 ppocr: 工業級高精大模型；easyocr: Ultralytics 官方方案；lprnet: 輕量自訓模型)")
    default_lprnet = find_default("checkpoints/taiwan_lprnet.onnx")
    tw_infer = find_default("checkpoints/PP-OCRv6_taiwan_infer")
    if os.path.isdir(tw_infer) and os.path.isfile(os.path.join(tw_infer, "inference.json")):
        default_ppocr = tw_infer
    elif os.path.isfile(find_default("checkpoints/PP-OCRv6_rec_small.onnx")):
        default_ppocr = find_default("checkpoints/PP-OCRv6_rec_small.onnx")
    else:
        default_ppocr = find_default("checkpoints/ch_PP-OCRv4_rec_infer.onnx")
    default_ocr = default_ppocr
    parser.add_argument("--ocr-ckpt", type=str, default="", help="車牌文字辨識 ONNX/Inference 權重路徑 (預設依 ocr-engine 自動選取)")
    parser.add_argument("--ocr-device", type=str, default="cuda", help="OCR 推論裝置 (cuda 或 cpu，預設 cuda)")
    parser.add_argument("--ocr-min-plate-w", type=int, default=58, help="觸發 OCR 之車牌最小像素寬度 (低於此寬度視為太遠太模糊略過，預設 58px)")
    parser.add_argument("--ocr-conf", type=float, default=0.50, help="OCR 文字辨識最低信心門檻")
    parser.add_argument("--ocr-interval", type=int, default=4, help="未鎖定前同輛車抽樣辨識間隔影格數 (預設每 4 幀抽檢一次)")
    parser.add_argument("--plate-max-dist", type=float, default=22.0, help="車牌偵測與文字辨識之統一最大距離門檻 (公尺，預設 22.0m，超過此距離不切圖不跑車牌 YOLO 亦不跑 OCR)")
    parser.add_argument("--vehicle-interval", type=int, default=2, help="車輛目標偵測間隔影格數 (預設 2，即每隔 2 幀跑一次 YOLO 追蹤，中間幀採時序軌跡內插，算力減半且畫面極致平滑)")
    parser.add_argument("--taiwan-plate-filter", dest="taiwan_plate_filter", action="store_true", default=True, help="啟用台灣車牌規格校驗與字元消歧義修復 (依據交通部號牌法規，預設開啟)")
    parser.add_argument("--no-taiwan-plate-filter", dest="taiwan_plate_filter", action="store_false", help="關閉台灣車牌規格校驗")
    parser.add_argument("--track", action="store_true", default=True, help="是否開啟 persist=True 追蹤記憶")
    args = parser.parse_args()

    if not args.ocr_ckpt:
        if args.ocr_engine == "lprnet":
            args.ocr_ckpt = default_lprnet
        elif args.ocr_engine == "ppocr":
            args.ocr_ckpt = default_ppocr
        else:
            args.ocr_ckpt = ""

    # 智慧路徑解析：若終端機位於子資料夾 (如 LPRNet_Pytorch)，自動重定向回專案根目錄
    base_dir = os.path.dirname(os.path.abspath(__file__))
    def resolve_path(p):
        if not p:
            return p
        if os.path.exists(p):
            return os.path.abspath(p)
        cand = os.path.join(base_dir, p)
        if os.path.exists(cand):
            return cand
        return p

    args.video = resolve_path(args.video)
    args.vehicle_model = resolve_path(args.vehicle_model)
    args.plate_model = resolve_path(args.plate_model)
    args.depth_ckpt = resolve_path(args.depth_ckpt)
    args.ocr_ckpt = resolve_path(args.ocr_ckpt)
    if not os.path.isabs(args.output) and not os.path.dirname(args.output):
        args.output = os.path.join(base_dir, args.output)

    if not os.path.isfile(args.video):
        raise FileNotFoundError(f"找不到輸入影片: {args.video} (搜尋路徑: {args.video} 或 {os.path.join(base_dir, args.video)})")

    device = args.device if torch.cuda.is_available() and args.device == "cuda" else "cpu"
    print(f"🚀 使用推理裝置: {device.upper()}")
    if device == "cuda":
        print(f"🎮 GPU 顯卡名稱: {torch.cuda.get_device_name(0)}")

    mode_str = "兩階段車輛裁切偵測 (Two-Stage Crop & Detect)" if args.two_stage else "單階段全圖獨立偵測 (Single-Stage)"
    print(f"⚙️ 執行模式: {mode_str}")
    print(f"📐 車輛推論尺寸: {args.imgsz_vehicle} | 車牌推論尺寸: {args.imgsz_plate}")

    vehicle_model = YOLO(args.vehicle_model, task="detect")
    plate_model = YOLO(args.plate_model, task="detect")
    if str(args.vehicle_model).lower().endswith(".pt"):
        vehicle_model.to(device)
    if str(args.plate_model).lower().endswith(".pt"):
        plate_model.to(device)

    depth_model = None
    if args.enable_depth:
        if os.path.isfile(args.depth_ckpt):
            if args.depth_ckpt.lower().endswith(".onnx"):
                try:
                    depth_model = ONNXDepthAnythingV2(args.depth_ckpt, device=device)
                    print(f"📏 深度測距模組: ONNX 格式 ({args.depth_ckpt}) 載入成功 (使用裝置: {device.upper()})！")
                except Exception as e:
                    print(f"⚠️ ONNX 深度模組載入失敗: {e}，將略過深度測距。")
                    depth_model = None
            else:
                try:
                    depth_repo_path = os.path.abspath("Depth-Anything-V2/metric_depth")
                    if depth_repo_path not in sys.path:
                        sys.path.append(depth_repo_path)
                    from depth_anything_v2.dpt import DepthAnythingV2
                    depth_model = DepthAnythingV2(**{'encoder': 'vits', 'features': 64, 'out_channels': [48, 96, 192, 384], 'max_depth': 80})
                    depth_model.load_state_dict(torch.load(args.depth_ckpt, map_location='cpu'))
                    depth_model = depth_model.to(device).eval()
                    print(f"📏 深度測距模組: PyTorch 格式 ({args.depth_ckpt}) 載入成功！")
                except Exception as e:
                    print(f"⚠️ PyTorch 深度模組載入失敗: {e}，將略過深度測距。")
                    depth_model = None
        else:
            print(f"⚠️ 找不到深度模型權重: {args.depth_ckpt}，將略過深度測距。")

    ocr_model = None
    ocr_tracker = None
    if args.enable_ocr:
        try:
            use_ocr_cuda = (args.ocr_device == "cuda" and torch.cuda.is_available())
            ocr_ckpt = args.ocr_ckpt
            if args.ocr_engine == "easyocr":
                ocr_model = EasyOCRRecognizer(device=args.ocr_device)
                print(f"🔤 車牌辨識模組: EasyOCR 引擎 (Ultralytics 官方方案) 載入成功 (使用裝置: {'CUDA' if use_ocr_cuda else 'CPU'}，英數先驗白名單: 已啟用)！")
            elif args.ocr_engine == "lprnet" and os.path.isfile(ocr_ckpt):
                ocr_model = TaiwanLPRNetRecognizer(ocr_ckpt, device=args.ocr_device)
                print(f"🔤 車牌辨識模組: 專屬台灣號牌 LPRNet ONNX 引擎 ({ocr_ckpt}) 載入成功 (使用裝置: {'CUDA' if use_ocr_cuda else 'CPU'}，推論僅需 ~1.2ms)！")
            elif os.path.isdir(ocr_ckpt) or (isinstance(ocr_ckpt, str) and (ocr_ckpt.endswith("inference.json") or "PP-OCRv6_taiwan_infer" in str(ocr_ckpt))):
                try:
                    ocr_model = PaddleInferPPOCRv6Recognizer(ocr_ckpt, device=args.ocr_device)
                    print(f"🔤 文字辨識模組: 台灣車牌專屬微調 PP-OCRv6 (Paddle Inference 獨立進程) 引擎 ({ocr_ckpt}) 載入成功 (使用裝置: {'CUDA' if use_ocr_cuda else 'CPU'}，英數先驗遮罩: 已啟用)！")
                except Exception as ex_ft:
                    print(f"⚠️ 台灣微調 PP-OCRv6 引擎載入異常: {ex_ft}，嘗試降級使用 ONNX 預訓練模型...")
                    default_onnx = find_default("checkpoints/PP-OCRv6_rec_small.onnx")
                    if os.path.isfile(default_onnx):
                        ocr_model = ONNXPPOCRv6Recognizer(default_onnx, device=args.ocr_device)
                    else:
                        from rapidocr_onnxruntime import RapidOCR
                        ocr_model = RapidOCR(rec_use_cuda=use_ocr_cuda, det_use_cuda=False, cls_use_cuda=False)
            elif os.path.isfile(ocr_ckpt):
                try:
                    ocr_model = ONNXPPOCRv6Recognizer(ocr_ckpt, device=args.ocr_device)
                    print(f"🔤 文字辨識模組: PP-OCRv6 ONNX 引擎 ({ocr_ckpt}) 載入成功 (使用裝置: {'CUDA' if use_ocr_cuda else 'CPU'}，英數先驗遮罩: 已啟用)！")
                except Exception as ex_v6:
                    print(f"⚠️ PP-OCRv6 引擎載入異常: {ex_v6}，降級使用 RapidOCR 通用載入...")
                    from rapidocr_onnxruntime import RapidOCR
                    ocr_model = RapidOCR(rec_model_path=ocr_ckpt, rec_use_cuda=use_ocr_cuda, det_use_cuda=False, cls_use_cuda=False)
            else:
                from rapidocr_onnxruntime import RapidOCR
                ocr_model = RapidOCR(rec_use_cuda=use_ocr_cuda, det_use_cuda=False, cls_use_cuda=False)

            ocr_tracker = PlateOCRTracker(
                interval=args.ocr_interval,
                min_width=args.ocr_min_plate_w,
                conf_thresh=args.ocr_conf,
                max_dist=args.plate_max_dist,
                enable_taiwan_filter=args.taiwan_plate_filter
            )
            tw_status = "已啟用 (字軌法規約束 + 消歧義修復)" if args.taiwan_plate_filter else "未啟用"
            print(f"🔤 車牌時序追蹤模組載入完成 (台灣車牌語法校驗: {tw_status}，字元級加權投票: 已啟用)！")
        except Exception as e:
            print(f"⚠️ OCR 載入失敗: {e}，將略過車牌文字辨識。")
            ocr_model = None
            ocr_tracker = None

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片: {args.video}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"影片資訊: {width}x{height} @ {fps:.2f} FPS, 總影格數: {total_frames}")

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(args.output, fourcc, fps, (width, height))

    batch_size = args.batch_size
    frame_queue = []
    processed_count = 0

    all_vehicle_confs = []
    all_plate_confs = []
    plate_tracker = {}
    rider_to_bike_history = {}
    last_depth_map = None

    start_total_time = time.perf_counter()
    time_vehicle_total = 0.0
    time_depth_total = 0.0
    time_plate_total = 0.0
    time_ocr_total = 0.0
    time_draw_total = 0.0

    while True:
        ret, frame = cap.read()
        if ret:
            frame_queue.append(frame)
        
        if len(frame_queue) == batch_size or (not ret and len(frame_queue) > 0):
            # 🚗 第一階段：全目標偵測與追蹤 (支援隔幀偵測 + 時序軌跡內插，算力減半)
            v_interval = max(1, args.vehicle_interval)
            if v_interval <= 1 or len(frame_queue) <= 1:
                key_indices = list(range(len(frame_queue)))
            else:
                key_indices = list(range(0, len(frame_queue), v_interval))

            infer_frames = [frame_queue[k] for k in key_indices]

            t_v0 = time.perf_counter()
            if args.track:
                v_results_sub = vehicle_model.track(
                    infer_frames, persist=True, classes=TARGET_CLASSES,
                    conf=args.conf_vehicle, imgsz=args.imgsz_vehicle,
                    device=device, verbose=False
                )
            else:
                v_results_sub = vehicle_model(
                    infer_frames, classes=TARGET_CLASSES,
                    conf=args.conf_vehicle, imgsz=args.imgsz_vehicle,
                    device=device, verbose=False
                )
            time_vehicle_total += (time.perf_counter() - t_v0)

            # 📏 深度估算：支援隔幀推論 (Temporal Depth Subsampling)，大幅減輕 ViT 計算負擔
            depth_maps = []
            if depth_model is not None:
                for f_idx, f_item in enumerate(frame_queue):
                    curr_abs_frame = processed_count + f_idx
                    if last_depth_map is None or (args.depth_interval <= 1) or (curr_abs_frame % args.depth_interval == 0):
                        t_d0 = time.perf_counter()
                        with torch.no_grad():
                            with torch.amp.autocast('cuda') if device == 'cuda' else torch.no_grad():
                                d_map = depth_model.infer_image(f_item, input_size=args.depth_size)
                        time_depth_total += (time.perf_counter() - t_d0)
                        last_depth_map = d_map
                    else:
                        d_map = last_depth_map
                    depth_maps.append(d_map)

            batch_vehicle_boxes = [[] for _ in range(len(frame_queue))]
            batch_plate_boxes = [[] for _ in range(len(frame_queue))]

            # 1. 提取關鍵幀 (Keyframes) 的邊界框、追蹤 ID 與真實公尺距離
            for sub_idx, k in enumerate(key_indices):
                v_res = v_results_sub[sub_idx]
                if v_res.boxes is not None and len(v_res.boxes) > 0:
                    boxes = v_res.boxes
                    ids = boxes.id.cpu().numpy().tolist() if boxes.id is not None else [None] * len(boxes)
                    for b in range(len(boxes)):
                        xyxy = boxes.xyxy[b].cpu().numpy().tolist()
                        conf = float(boxes.conf[b].cpu().item())
                        cls = int(boxes.cls[b].cpu().item())
                        tid = ids[b]

                        # 🎯 從深度圖取目標中央 50% 區域的中位數 (Median Depth)
                        dist_m = 0.0
                        if k < len(depth_maps) and depth_maps[k] is not None:
                            d_map = depth_maps[k]
                            x1, y1, x2, y2 = map(int, xyxy)
                            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                            rw = max(2, int((x2 - x1) * 0.5))
                            rh = max(2, int((y2 - y1) * 0.5))
                            rx1, rx2 = max(0, cx - rw // 2), min(d_map.shape[1], cx + rw // 2)
                            ry1, ry2 = max(0, cy - rh // 2), min(d_map.shape[0], cy + rh // 2)
                            roi = d_map[ry1:ry2, rx1:rx2]
                            if roi.size > 0:
                                dist_m = float(np.median(roi))

                        batch_vehicle_boxes[k].append(xyxy + [conf, cls, tid, dist_m])

            # 2. 若開啟隔幀推論 (v_interval > 1)，對中間未推論之影格進行時序軌跡內插
            if v_interval > 1 and len(key_indices) > 1:
                for idx in range(len(key_indices) - 1):
                    k1 = key_indices[idx]
                    k2 = key_indices[idx + 1]
                    map_k1 = {b[6]: b for b in batch_vehicle_boxes[k1] if b[6] is not None}
                    map_k2 = {b[6]: b for b in batch_vehicle_boxes[k2] if b[6] is not None}

                    for j in range(k1 + 1, k2):
                        alpha = float(j - k1) / float(k2 - k1)
                        for tid, b1 in map_k1.items():
                            if tid in map_k2:
                                b2 = map_k2[tid]
                                interp_coords = [(1.0 - alpha) * b1[p] + alpha * b2[p] for p in range(4)]
                                interp_conf = (1.0 - alpha) * b1[4] + alpha * b2[4]
                                interp_cls = b1[5]
                                interp_dist = (1.0 - alpha) * b1[7] + alpha * b2[7]
                                batch_vehicle_boxes[j].append(interp_coords + [interp_conf, interp_cls, tid, interp_dist])
                            else:
                                batch_vehicle_boxes[j].append(list(b1))
                        for tid, b2 in map_k2.items():
                            if tid not in map_k1:
                                batch_vehicle_boxes[j].append(list(b2))

                # 處理尾端影格 (若最後一格在最後一個關鍵幀之後)
                k_last = key_indices[-1]
                for j in range(k_last + 1, len(frame_queue)):
                    batch_vehicle_boxes[j] = [list(b) for b in batch_vehicle_boxes[k_last]]

            # 🛵 融合機車騎士與機車實體 (Rider-Motorcycle Fusion):
            # 消除同一輛車上騎士與機車各自獨立成框 (如 person #9 與 motorcycle #124 分離)
            # 統一融合為單一 motorcycle 實體框，並合併 track ID 與 OCR 時序歷史
            for i in range(len(frame_queue)):
                fused_v, tid_maps = fuse_rider_and_motorcycles(
                    batch_vehicle_boxes[i],
                    rider_to_bike_history=rider_to_bike_history
                )
                batch_vehicle_boxes[i] = fused_v
                if tid_maps:
                    for from_tid, to_tid in tid_maps.items():
                        if ocr_tracker is not None:
                            ocr_tracker.merge_tracks(from_tid, to_tid)
                        if args.smooth and from_tid in plate_tracker:
                            if to_tid not in plate_tracker:
                                plate_tracker[to_tid] = plate_tracker[from_tid]
                            del plate_tracker[from_tid]

            if args.two_stage:
                # 🔍 第二階段：僅從車輛區域裁切 (Crop) 進行批次車牌偵測 (排除非道路目標，納入機車騎士)
                crops_to_infer = []
                crop_metadata = []

                for i, frame_item in enumerate(frame_queue):
                    fh, fw = frame_item.shape[:2]
                    for v_box in batch_vehicle_boxes[i]:
                        vx1, vy1, vx2, vy2 = map(int, v_box[:4])
                        cls = v_box[5]
                        tid = v_box[6]
                        dist_m = float(v_box[7]) if len(v_box) > 7 and v_box[7] is not None else 0.0
                        vw = vx2 - vx1
                        vh = vy2 - vy1

                        # 🛑 車輛類別 (2, 3, 5, 7) 或道路行駛中之機車騎士 (0) 才需要裁切檢測車牌
                        is_target = (cls in PLATE_VEHICLE_CLASSES)
                        if not is_target and cls == 0:
                            # 位於道路下半部且具備足夠尺寸之機車騎士
                            if vy2 > fh * 0.40 and vw >= 35 and vh >= 60:
                                is_target = True

                        if not is_target:
                            continue

                        # 📏 統一車牌距離門檻：超過指定公尺距離 (預設 25m)，不切圖、不跑車牌 YOLO、不跑 OCR
                        if dist_m > 0 and dist_m > args.plate_max_dist:
                            continue

                        # 過濾尺寸過小的遠距微小車輛
                        if vw < args.min_vehicle_size or vh < args.min_vehicle_size:
                            continue

                        # 外擴緩衝邊距 (Padding)，防止車輛框裁切到貼邊車牌
                        # 對於機車騎士 (cls == 0) 與機車 (cls == 3)，後座外送箱 (UberEats/Foodpanda) 與後牌架容易向兩側延伸，適度外擴以防漏檢
                        if cls == 0:
                            pad_w = max(int(vw * 0.65), 100)
                            pad_h = int(vh * 0.10)
                            pad_h_down = int(vh * 0.35)
                        elif cls == 3:
                            pad_w = max(int(vw * 0.35), 60)
                            pad_h = int(vh * 0.10)
                            pad_h_down = max(int(vh * 0.25), 40)
                        else:
                            pad_w = int(vw * args.crop_padding)
                            pad_h = int(vh * args.crop_padding)
                            pad_h_down = pad_h

                        cx1 = max(0, vx1 - pad_w)
                        cy1 = max(0, vy1 - pad_h)
                        cx2 = min(fw, vx2 + pad_w)
                        cy2 = min(fh, vy2 + pad_h_down)

                        crop = frame_item[cy1:cy2, cx1:cx2]
                        if crop.shape[0] < 10 or crop.shape[1] < 10:
                            continue

                        crops_to_infer.append(crop)
                        crop_metadata.append({
                            "frame_idx": i,
                            "offset_x": cx1,
                            "offset_y": cy1,
                            "crop_w": cx2 - cx1,
                            "crop_h": cy2 - cy1,
                        })

                # 蒐集每格影格之全圖坐標候選車牌
                frame_raw_plates = [[] for _ in range(len(frame_queue))]
                if crops_to_infer:
                    # 批次送入車牌模型
                    t_p0 = time.perf_counter()
                    p_results = plate_model(
                        crops_to_infer,
                        imgsz=args.imgsz_plate,
                        conf=args.conf_plate,
                        batch=args.crop_batch_size,
                        device=device,
                        verbose=False
                    )
                    time_plate_total += (time.perf_counter() - t_p0)

                    for crop_res, meta in zip(p_results, crop_metadata):
                        f_idx = meta["frame_idx"]
                        ox = meta["offset_x"]
                        oy = meta["offset_y"]
                        cw = meta["crop_w"]
                        ch = meta["crop_h"]

                        if crop_res.boxes is not None and len(crop_res.boxes) > 0:
                            for pb in crop_res.boxes:
                                px1, py1, px2, py2 = pb.xyxy[0].cpu().numpy().tolist()
                                pconf = float(pb.conf[0].cpu().item())
                                pcls = int(pb.cls[0].cpu().item())
                                pw = px2 - px1
                                ph = py2 - py1
                                aspect = pw / max(ph, 1)

                                # 🛡️ 幾何特徵過濾：台灣車牌長寬比約在 0.95~4.5 (機車牌偏方 1.0~1.8，汽車牌 3.0~3.5)，排除噪點
                                if 0.95 <= aspect <= 4.5 and pw >= 16 and ph >= 8:
                                    gx1 = ox + px1
                                    gy1 = oy + py1
                                    gx2 = ox + px2
                                    gy2 = oy + py2
                                    frame_raw_plates[f_idx].append([gx1, gy1, gx2, gy2, pconf, pcls])

                # 🌐 全域道路車牌安全網 (Road-level Global Safety Net):
                # 避免外送機車未被 YOLO 檢出或邊界車輛漏檢，在道路可見區域 (下半部 65%) 全域補漏掃描
                road_crops = []
                road_meta = []
                for i, frame_item in enumerate(frame_queue):
                    fh, fw = frame_item.shape[:2]
                    r_y1 = int(fh * 0.35)
                    road_crops.append(frame_item[r_y1:, :])
                    road_meta.append({"frame_idx": i, "offset_y": r_y1})

                if road_crops:
                    t_r0 = time.perf_counter()
                    road_p_res = plate_model(road_crops, imgsz=640, conf=args.conf_plate, device=device, verbose=False)
                    time_plate_total += (time.perf_counter() - t_r0)
                    for r_res, r_m in zip(road_p_res, road_meta):
                        f_idx = r_m["frame_idx"]
                        oy = r_m["offset_y"]
                        if r_res.boxes is not None:
                            for pb in r_res.boxes:
                                px1, py1, px2, py2 = pb.xyxy[0].cpu().numpy().tolist()
                                pconf = float(pb.conf[0].cpu().item())
                                pcls = int(pb.cls[0].cpu().item())
                                pw = px2 - px1
                                ph = py2 - py1
                                aspect = pw / max(ph, 1)
                                if 0.95 <= aspect <= 4.5 and pw >= 16 and ph >= 8:
                                    gx1 = px1
                                    gy1 = oy + py1
                                    gx2 = px2
                                    gy2 = oy + py2
                                    frame_raw_plates[f_idx].append([gx1, gy1, gx2, gy2, pconf, pcls])

                    # 針對每一格影格進行車牌去重、車輛關聯匹配、時序追蹤與 OCR
                    for i in range(len(frame_queue)):
                        # 1. 跨裁切塊 IoU 去重，消除相鄰車輛裁切重複捕獲同一實體車牌
                        deduped_plates = filter_duplicate_boxes(frame_raw_plates[i], iou_thresh=0.45)
                        matched_tids = set()

                        for p_box in deduped_plates:
                            gx1, gy1, gx2, gy2, pconf, pcls = p_box[:6]

                            # 2. 空間幾何嚴格匹配：尋找最符合的車輛/騎士實體 (徹底杜絕張冠李戴)
                            matched_v = match_plate_to_vehicle(p_box, batch_vehicle_boxes[i])
                            tid = matched_v[6] if matched_v is not None else None
                            v_cls = matched_v[5] if matched_v is not None else None
                            dist_m = float(matched_v[7]) if (matched_v is not None and len(matched_v) > 7 and matched_v[7] is not None) else 0.0
                            v_box = matched_v[:4] if matched_v is not None else None
                            vw = (v_box[2] - v_box[0]) if v_box is not None else (gx2 - gx1) * 3.0

                            if tid is not None:
                                matched_tids.add(tid)

                            final_box = [gx1, gy1, gx2, gy2]
                            # 🌊 跨影格平滑追蹤 (Temporal EMA Smoothing - 消除每格抖動)
                            if args.smooth and tid is not None:
                                if tid in plate_tracker:
                                    prev_box = plate_tracker[tid]["box"]
                                    prev_cx = (prev_box[0] + prev_box[2]) / 2.0
                                    curr_cx = (gx1 + gx2) / 2.0
                                    # 限制單格橫向位移不得超過車寬 40%，防止瞬移
                                    if abs(curr_cx - prev_cx) < vw * 0.4:
                                        final_box = [
                                            args.smooth_alpha * final_box[k] + (1.0 - args.smooth_alpha) * prev_box[k]
                                            for k in range(4)
                                        ]
                                plate_tracker[tid] = {"box": final_box, "conf": pconf, "lost": 0, "v_box": v_box}

                            # 🔤 第三階段：車牌字元辨識 (PP-OCRv6 Recognition - 純文字識別)
                            p_text = None
                            t_conf = 0.0
                            if ocr_tracker is not None and ocr_model is not None:
                                pw = final_box[2] - final_box[0]
                                ph = final_box[3] - final_box[1]
                                curr_frame_idx = processed_count + i
                                if ocr_tracker.should_infer(tid, curr_frame_idx, pw, dist_m=dist_m):
                                    # 🛡️ 防護 1：外擴邊界保護 (Border Padding)
                                    # 水平兩側放寬至 10%，確保首尾字元 (如 B, E, 8, 數字) 筆畫 100% 納入；垂直保持 5% 避免上下包進過多底盤
                                    f_item = frame_queue[i]
                                    pad_x = max(4, int(pw * 0.10))
                                    pad_y = max(2, int(ph * 0.05))
                                    px1 = max(0, int(round(final_box[0] - pad_x)))
                                    py1 = max(0, int(round(final_box[1] - pad_y)))
                                    px2 = min(f_item.shape[1], int(round(final_box[2] + pad_x)))
                                    py2 = min(f_item.shape[0], int(round(final_box[3] + pad_y)))
                                    plate_roi = f_item[py1:py2, px1:px2]
                                    if plate_roi.shape[0] >= 8 and plate_roi.shape[1] >= 16:
                                        # 🔍 清晰度評估：若嚴重動態模糊 (Laplacian 方差 < 100)，略過此格等待更清晰幀
                                        gray_roi = cv2.cvtColor(plate_roi, cv2.COLOR_BGR2GRAY)
                                        lap_var = cv2.Laplacian(gray_roi, cv2.CV_64F).var()
                                        if lap_var >= 100.0:
                                            try:
                                                t_o0 = time.perf_counter()
                                                ocr_res, _ = ocr_model(plate_roi, use_det=False, use_cls=False)
                                                time_ocr_total += (time.perf_counter() - t_o0)
                                                if ocr_res and len(ocr_res) > 0 and ocr_res[0][0]:
                                                    ocr_tracker.update(tid, curr_frame_idx, ocr_res[0][0], float(ocr_res[0][1]), veh_cls=v_cls, plate_w=pw)
                                                    if tid is None:
                                                        p_text = ocr_res[0][0]
                                                        t_conf = float(ocr_res[0][1])
                                            except Exception:
                                                pass

                                if tid is not None:
                                    p_text, t_conf = ocr_tracker.get_plate_raw(tid, veh_cls=v_cls)

                            batch_plate_boxes[i].append(final_box + [pconf, pcls, tid, p_text, t_conf])

                        # 🔄 掉幀平滑補償 (Holdover)：若車輛仍被追蹤但車牌因反光短暫遺失 1~2 格，跟隨車輛位移維持
                        if args.smooth:
                            for tid, pt in list(plate_tracker.items()):
                                if tid not in matched_tids and pt["lost"] < 2:
                                    matching_v = [vb for vb in batch_vehicle_boxes[i] if vb[6] == tid]
                                    if matching_v:
                                        curr_vbox = matching_v[0][:4]
                                        prev_vbox = pt["v_box"]
                                        if prev_vbox is not None:
                                            dx = curr_vbox[0] - prev_vbox[0]
                                            dy = curr_vbox[1] - prev_vbox[1]
                                            held_box = [
                                                pt["box"][0] + dx,
                                                pt["box"][1] + dy,
                                                pt["box"][2] + dx,
                                                pt["box"][3] + dy
                                            ]
                                            # 空間幾何有效性檢查：補償框中心必須仍然位於該車身範圍內 (防止位移飄出車身)
                                            hcx = (held_box[0] + held_box[2]) / 2.0
                                            hcy = (held_box[1] + held_box[3]) / 2.0
                                            if not (curr_vbox[0] <= hcx <= curr_vbox[2] and curr_vbox[1] + 0.15 * (curr_vbox[3] - curr_vbox[1]) <= hcy <= curr_vbox[3] + 0.15 * (curr_vbox[3] - curr_vbox[1])):
                                                continue

                                            pt["lost"] += 1
                                            pt["box"] = held_box
                                            pt["v_box"] = curr_vbox
                                            held_conf = pt["conf"] * 0.85
                                            held_p_text = None
                                            held_t_conf = 0.0
                                            if ocr_tracker is not None:
                                                held_p_text, held_t_conf = ocr_tracker.get_plate_raw(tid, veh_cls=matching_v[0][5])
                                            batch_plate_boxes[i].append(held_box + [held_conf, 0, tid, held_p_text, held_t_conf])

                # 對每格車牌進行最終 NMS 去重 (消除真實檢測與平滑補償間的重疊)
                for i in range(len(batch_plate_boxes)):
                    batch_plate_boxes[i] = filter_duplicate_boxes(batch_plate_boxes[i], iou_thresh=0.40)

            else:
                # 傳統單階段全圖偵測模式 (Fallback)
                if args.track:
                    p_results_batch = plate_model.track(
                        frame_queue, persist=True, conf=args.conf_plate,
                        imgsz=args.imgsz_plate, device=device, verbose=False
                    )
                else:
                    p_results_batch = plate_model(
                        frame_queue, conf=args.conf_plate,
                        imgsz=args.imgsz_plate, device=device, verbose=False
                    )

                for i, p_res in enumerate(p_results_batch):
                    if p_res.boxes is not None:
                        boxes = p_res.boxes
                        ids = boxes.id.cpu().numpy().tolist() if boxes.id is not None else [None] * len(boxes)
                        for b in range(len(boxes)):
                            xyxy = boxes.xyxy[b].cpu().numpy().tolist()
                            conf = float(boxes.conf[b].cpu().item())
                            cls = int(boxes.cls[b].cpu().item())
                            # 空間幾何匹配關聯車輛
                            matched_v = match_plate_to_vehicle(xyxy, batch_vehicle_boxes[i])
                            tid = matched_v[6] if matched_v is not None else ids[b]
                            v_cls = matched_v[5] if matched_v is not None else None
                            dist_m = float(matched_v[7]) if (matched_v is not None and len(matched_v) > 7 and matched_v[7] is not None) else 0.0

                            p_text = None
                            t_conf = 0.0
                            if ocr_tracker is not None and ocr_model is not None:
                                pw = xyxy[2] - xyxy[0]
                                ph = xyxy[3] - xyxy[1]
                                curr_frame_idx = processed_count + i
                                if ocr_tracker.should_infer(tid, curr_frame_idx, pw, dist_m=dist_m):
                                    # 🛡️ 防護 1：外擴邊界保護 (Border Padding)
                                    # 徹底拔除向內縮切邏輯，向外擴張 5% 邊距，確保首尾字元完整納入 ROI
                                    f_item = frame_queue[i]
                                    pad_x = max(2, int(pw * 0.05))
                                    pad_y = max(2, int(ph * 0.06))
                                    px1 = max(0, int(round(xyxy[0] - pad_x)))
                                    py1 = max(0, int(round(xyxy[1] - pad_y)))
                                    px2 = min(f_item.shape[1], int(round(xyxy[2] + pad_x)))
                                    py2 = min(f_item.shape[0], int(round(xyxy[3] + pad_y)))
                                    plate_roi = f_item[py1:py2, px1:px2]
                                    if plate_roi.shape[0] >= 10 and plate_roi.shape[1] >= 20:
                                        try:
                                            ocr_res, _ = ocr_model(plate_roi, use_det=False, use_cls=False)
                                            if ocr_res and len(ocr_res) > 0 and ocr_res[0][0]:
                                                ocr_tracker.update(tid, curr_frame_idx, ocr_res[0][0], float(ocr_res[0][1]), veh_cls=v_cls)
                                        except Exception:
                                            pass
                                if tid is not None:
                                    p_text, t_conf = ocr_tracker.get_plate_raw(tid, veh_cls=v_cls)
                            batch_plate_boxes[i].append(xyxy + [conf, cls, tid, p_text, t_conf])

            # 雙軌映射：影片畫面顯示原版 OCR 文字，下游外出資料採用台灣法規補償文字
            video_plate_map = {}
            vehicle_plate_map = {}
            if ocr_tracker is not None:
                for v_tid, rec in ocr_tracker.records.items():
                    if rec.get("best_raw_text"):
                        video_plate_map[v_tid] = (rec["best_raw_text"], rec.get("veh_cls"))
                    if rec.get("best_compensated_text"):
                        vehicle_plate_map[v_tid] = (rec["best_compensated_text"], rec.get("veh_cls"))

            # 繪製標註框並寫入影片，同時統計信心值 (影片畫面使用 video_plate_map 繪製原版字串)
            t_w0 = time.perf_counter()
            for i, f in enumerate(frame_queue):
                v_boxes = batch_vehicle_boxes[i]
                p_boxes = batch_plate_boxes[i]

                for vb in v_boxes:
                    all_vehicle_confs.append(vb[4])
                for pb in p_boxes:
                    all_plate_confs.append(pb[4])

                if v_boxes:
                    f = draw_boxes(f, v_boxes, vehicle_model.names, color=(255, 140, 0), vehicle_plate_map=video_plate_map)
                if p_boxes:
                    f = draw_boxes(f, p_boxes, plate_model.names, color=(0, 0, 255), label_prefix="Plate")

                out.write(f)
            time_draw_total += (time.perf_counter() - t_w0)

            cur_batch_len = len(frame_queue)
            processed_count += cur_batch_len
            frame_queue.clear()

            # ⏱️ 即時速度與延遲計算
            elapsed_total = time.perf_counter() - start_total_time
            current_fps = processed_count / elapsed_total if elapsed_total > 0 else 0.0
            current_latency_ms = (elapsed_total / processed_count * 1000.0) if processed_count > 0 else 0.0
            pct = (processed_count / total_frames * 100.0) if total_frames > 0 else 0.0

            # 終端即時進度與速度指標輸出 (每 32 幀或完成時更新)
            if processed_count % 32 == 0 or processed_count == total_frames or (args.max_frames > 0 and processed_count >= args.max_frames):
                current_plate_avg = (sum(all_plate_confs) / len(all_plate_confs)) if all_plate_confs else 0.0
                print(f"⚡ [進度: {processed_count:4d}/{total_frames} ({pct:5.1f}%)] "
                      f"🚀 速度: {current_fps:5.1f} FPS ({current_latency_ms:5.1f} ms/幀) "
                      f"| 🪪 車牌平均信心值: {current_plate_avg:.3f}")

            if args.max_frames > 0 and processed_count >= args.max_frames:
                print(f"🛑 已達到最大指定影格數 ({args.max_frames})，提早結束處理。")
                break

        if not ret:
            break

    cap.release()
    out.release()
    total_wall_time = time.perf_counter() - start_total_time
    final_fps = processed_count / total_wall_time if total_wall_time > 0 else 0.0
    final_latency = (total_wall_time / processed_count * 1000.0) if processed_count > 0 else 0.0

    print(f"\n✅ 處理完全結束！標註影片已儲存至: {args.output}")

    # ⏱️ 輸出推論效能與耗時指標報告
    print("\n" + "=" * 55)
    print("⏱️  推論效能與耗時指標報告 (Performance & Benchmark)")
    print("=" * 55)
    print(f"總處理影格數: {processed_count} / {total_frames} 幀")
    print(f"總處理耗時:   {total_wall_time:.2f} 秒")
    print(f"🚀 平均推論速度: {final_fps:.1f} FPS (每秒處理影格數)")
    print(f"⚡ 平均每幀延遲: {final_latency:.1f} 毫秒 (ms/frame)")
    print("-" * 55)
    print("各核心階段耗時與佔比分析 (Latency Breakdown):")
    t_sum = time_vehicle_total + time_depth_total + time_plate_total + time_ocr_total + time_draw_total
    if t_sum > 0 and processed_count > 0:
        print(f"  🚗 車輛追蹤 (YOLO26s)      : {time_vehicle_total*1000/processed_count:5.1f} ms/幀 ({time_vehicle_total/t_sum*100:4.1f}%)")
        print(f"  📏 深度測距 (Depth V2)     : {time_depth_total*1000/processed_count:5.1f} ms/幀 ({time_depth_total/t_sum*100:4.1f}%)")
        print(f"  🪪 車牌定位 (Plate-320)    : {time_plate_total*1000/processed_count:5.1f} ms/幀 ({time_plate_total/t_sum*100:4.1f}%)")
        ocr_engine_name = "EasyOCR" if args.ocr_engine == "easyocr" else ("LPRNet" if args.ocr_engine == "lprnet" else "PP-OCRv6")
        print(f"  🔤 車牌辨識 ({ocr_engine_name:<8})   : {time_ocr_total*1000/processed_count:5.1f} ms/幀 ({time_ocr_total/t_sum*100:4.1f}%)")
        print(f"  🎨 標註繪製與影片編碼      : {time_draw_total*1000/processed_count:5.1f} ms/幀 ({time_draw_total/t_sum*100:4.1f}%)")
    print("=" * 55)

    # 📊 輸出完整信心值統計報告
    print("\n" + "=" * 55)
    print("📊 偵測、辨識與信心值統計報告 (Detection, OCR & Confidence Summary)")
    print("=" * 55)
    print(f"總處理影格數: {processed_count} / {total_frames}")

    if all_vehicle_confs:
        avg_v = sum(all_vehicle_confs) / len(all_vehicle_confs)
        print(f"🚗 車輛偵測總次數: {len(all_vehicle_confs)} 框")
        print(f"   - 平均信心值: {avg_v:.3f}")
        print(f"   - 最高信心值: {max(all_vehicle_confs):.3f}")
        print(f"   - 最低信心值: {min(all_vehicle_confs):.3f}")
    else:
        print("🚗 車輛偵測: 未偵測到車輛")

    print("-" * 55)
    if all_plate_confs:
        avg_p = sum(all_plate_confs) / len(all_plate_confs)
        print(f"🪪 車牌偵測總次數: {len(all_plate_confs)} 框")
        print(f"   - 平均信心值: {avg_p:.3f}")
        print(f"   - 最高信心值: {max(all_plate_confs):.3f}")
        print(f"   - 最低信心值: {min(all_plate_confs):.3f}")
    else:
        print("🪪 車牌偵測: 未偵測到車牌")

    if ocr_tracker is not None:
        confirmed_plates = ocr_tracker.get_all_confirmed()
        print("-" * 55)
        print(f"🔤 車牌文字辨識統計 (共辨識出 {len(confirmed_plates)} 輛車之車牌 - 出去資料採台灣法規補償輸出):")
        if confirmed_plates:
            for tid, (ptxt, pconf) in sorted(confirmed_plates.items()):
                raw_t = ocr_tracker.records[tid].get("best_raw_text", ptxt)
                print(f"   - 車輛 Track #{tid} ──▶ 外出車牌: {ptxt} (影片原版OCR: {raw_t} | 信心值: {pconf:.3f})")
        else:
            print("   - 尚無確認之車牌文字結果")
    print("=" * 55 + "\n")

if __name__ == "__main__":
    main()
