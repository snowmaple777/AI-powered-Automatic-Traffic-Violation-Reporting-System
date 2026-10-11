"""Algorithms migrated from perception/detect_and_annotate.py; no CLI side effects."""
import math
import re
from collections import Counter
import cv2
import numpy as np

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
        providers = ['CPUExecutionProvider']
        if str(device).startswith('cuda') and 'CUDAExecutionProvider' in ort.get_available_providers():
            device_id = int(str(device).split(':')[1]) if ':' in str(device) else 0
            providers.insert(0, ('CUDAExecutionProvider', {'device_id': device_id}))
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

class PlateOCRTracker:
    """
    車牌文字時序追蹤與快取管理器 (雙軌制：影片畫面顯示原版，外出資料採用台灣法規補償)：
    1. 結合車輛 track_id 進行抽樣辨識 (降頻避免每格推論)。
    2. 雙軌字串追蹤：
       - best_raw_text: 模型原汁原味辨識之車牌字串 (供影片標註框繪製)。
       - best_compensated_text: 經由台灣車牌法規檢驗、字元消歧義與連字號還原之標準字串 (供外出資料使用)。
    3. 保留最高信心度的有效辨識結果；只有嚴格更高的信心度才能更新文字。
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
        if not raw_text or not math.isfinite(conf) or conf < self.conf_thresh:
            return None, 0.0
        raw_clean = re.sub(r'[^A-Z0-9-]', '', raw_text.upper().strip()).strip('-')
        if len(raw_clean) < 4:
            return None, 0.0
        text = (TaiwanPlateValidator.validate_and_normalize(raw_clean)
                if self.enable_taiwan_filter else raw_clean)
        if not text:
            return None, 0.0
        if track_id is None:
            return raw_clean, conf

        rec = self.records.get(track_id)
        if rec and rec.get("veh_cls") is not None and veh_cls is not None:
            if (rec["veh_cls"] in (0, 1, 3)) != (veh_cls in (0, 1, 3)):
                self.records.pop(track_id)
                rec = None
        if rec is None:
            rec = self.records[track_id] = {
                "raw_history": [], "compensated_history": [], "confirmed": False,
                "best_raw_text": raw_clean, "best_compensated_text": text,
                "best_conf": conf,
            }
        rec.update(last_infer_frame=current_frame, last_plate_w=plate_w)
        if veh_cls is not None:
            rec["veh_cls"] = veh_cls
        # Keep text and confidence from the same observation; ties keep the label.
        if conf > rec["best_conf"]:
            rec.update(best_raw_text=raw_clean, best_compensated_text=text, best_conf=conf)
        rec["raw_history"] = (rec["raw_history"] + [(raw_clean, conf)])[-12:]
        rec["compensated_history"] = (rec["compensated_history"] + [(text, conf)])[-12:]
        support = [score for value, score in rec["compensated_history"]
                   if value == rec["best_compensated_text"]]
        rec["confirmed"] = bool(support) and (
            (len(support) >= 3 and sum(support) / len(support) >= .70)
            or (len(support) >= 2 and max(support) >= .88))
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

        # Transfer the complete winning observation, never a score alone.
        if from_rec["best_conf"] > to_rec["best_conf"]:
            for key in ("best_raw_text", "best_compensated_text", "best_conf", "confirmed"):
                to_rec[key] = from_rec[key]
        for key in ("raw_history", "compensated_history"):
            to_rec[key] = (to_rec[key] + from_rec[key])[-12:]
        if from_rec.get("last_infer_frame", 0) > to_rec.get("last_infer_frame", 0):
            to_rec["last_infer_frame"] = from_rec["last_infer_frame"]
            to_rec["last_plate_w"] = from_rec.get("last_plate_w")
        del self.records[from_tid]
