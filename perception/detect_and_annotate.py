import argparse
import cv2
import os
import sys
import time
import re
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

            # 2. 第八代/第七代機車 (6 碼: 3 英文 - 3 數字，例: MAY-123, AAA-001)
            if len(p1) == 3 and len(p2) == 3:
                p1_c = cls.fix_letters(p1)
                p2_c = cls.fix_digits(p2)
                c = f"{p1_c}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z]{3}-[0-9]{3}$', c):
                    return c

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
        elif len(pure) == 6:
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

class PlateOCRTracker:
    """
    車牌文字時序追蹤與快取管理器 (雙軌制：影片畫面顯示原版，外出資料採用台灣法規補償)：
    1. 結合車輛 track_id 進行抽樣辨識 (降頻避免每格推論)。
    2. 雙軌字串追蹤：
       - best_raw_text: 模型原汁原味辨識之車牌字串 (供影片標註框繪製)。
       - best_compensated_text: 經由台灣車牌法規檢驗、字元消歧義與連字號還原之標準字串 (供外出資料使用)。
    3. 採用多數決投票 (Temporal Voting)，消除單格反光或模糊導致的辨識錯誤。
    4. 車牌確認鎖定後 (Confirmed) 進入低頻複驗模式，大幅節省算力。
    """
    def __init__(self, interval=4, min_width=40, conf_thresh=0.50, max_dist=25.0, enable_taiwan_filter=True):
        self.interval = interval
        self.min_width = min_width
        self.conf_thresh = conf_thresh
        self.max_dist = max_dist
        self.enable_taiwan_filter = enable_taiwan_filter
        self.records = {}

    def should_infer(self, track_id, current_frame, plate_w, dist_m=None):
        if plate_w < self.min_width:
            return False
        if dist_m is not None and dist_m > self.max_dist:
            return False
        if track_id is None:
            return True
        rec = self.records.get(track_id)
        if not rec:
            return True
        if rec.get("confirmed", False):
            # 已確認車牌，每 30 幀才抽檢複驗一次
            return (current_frame - rec.get("last_infer_frame", 0)) >= 30
        return (current_frame - rec.get("last_infer_frame", 0)) >= self.interval

    def update(self, track_id, current_frame, raw_text, conf):
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

        if track_id not in self.records:
            self.records[track_id] = {
                "raw_history": [],
                "compensated_history": [],
                "confirmed": False,
                "best_raw_text": raw_clean,
                "best_compensated_text": compensated_clean or raw_clean,
                "best_conf": conf,
                "last_infer_frame": current_frame
            }
        rec = self.records[track_id]
        rec["last_infer_frame"] = current_frame

        rec["raw_history"].append((raw_clean, conf))
        if len(rec["raw_history"]) > 10:
            rec["raw_history"] = rec["raw_history"][-10:]

        if compensated_clean:
            rec["compensated_history"].append((compensated_clean, conf))
            if len(rec["compensated_history"]) > 10:
                rec["compensated_history"] = rec["compensated_history"][-10:]

        # 原始版多數決投票 (供影片畫面標註)
        counts_raw = Counter([h[0] for h in rec["raw_history"]])
        most_common_raw, freq_raw = counts_raw.most_common(1)[0]
        rec["best_raw_text"] = most_common_raw

        # 補償版多數決投票 (供外出資料使用)
        if rec["compensated_history"]:
            counts_comp = Counter([h[0] for h in rec["compensated_history"]])
            most_common_comp, freq_comp = counts_comp.most_common(1)[0]
            matching_confs = [h[1] for h in rec["compensated_history"] if h[0] == most_common_comp]
            avg_conf = sum(matching_confs) / len(matching_confs)
            max_conf = max(matching_confs)

            rec["best_compensated_text"] = most_common_comp
            rec["best_conf"] = avg_conf

            # 鎖定條件：同字串累計出現 >= 3 次且平均信心度 >= 0.70，或出現 >= 2 次且最高信心度 >= 0.90
            if (freq_comp >= 3 and avg_conf >= 0.70) or (freq_comp >= 2 and max_conf >= 0.90):
                rec["confirmed"] = True
        else:
            matching_confs = [h[1] for h in rec["raw_history"] if h[0] == most_common_raw]
            avg_conf = sum(matching_confs) / len(matching_confs)
            rec["best_compensated_text"] = most_common_raw
            rec["best_conf"] = avg_conf

        return rec["best_raw_text"], rec["best_conf"]

    def get_plate_raw(self, track_id):
        """取得原始未補償的車牌文字 (供影片畫面標註繪製)"""
        if track_id is not None and track_id in self.records:
            return self.records[track_id]["best_raw_text"], self.records[track_id]["best_conf"]
        return None, 0.0

    def get_plate(self, track_id):
        """取得經由台灣法規補償與消歧義修復後的車牌文字 (供外出資料/下游組員/API使用)"""
        if track_id is not None and track_id in self.records:
            return self.records[track_id]["best_compensated_text"], self.records[track_id]["best_conf"]
        return None, 0.0

    def get_all_confirmed(self):
        """取得所有已確認/最佳之合規車牌字典 (供終端報告與外出資料匯出)"""
        return {
            tid: (data["best_compensated_text"], data["best_conf"])
            for tid, data in self.records.items()
            if data.get("best_compensated_text")
        }

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
            v_plate_str = f" [{vehicle_plate_map[track_id]}]"
        
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
    parser = argparse.ArgumentParser(description="兩階段車輛追蹤與車牌辨識影片標註系統")
    parser.add_argument("--video", type=str, default="vid.mp4", help="輸入影片路徑")
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
    default_plate = "checkpoints/license-plate-finetune-v1s.onnx" if os.path.isfile("checkpoints/license-plate-finetune-v1s.onnx") else ("checkpoints/license-plate-finetune-v1s.pt" if os.path.isfile("checkpoints/license-plate-finetune-v1s.pt") else "license-plate-finetune-v1s.pt")
    default_vehicle = "checkpoints/yolo26s.onnx" if os.path.isfile("checkpoints/yolo26s.onnx") else ("checkpoints/yolo26s.pt" if os.path.isfile("checkpoints/yolo26s.pt") else "yolo26s.pt")
    parser.add_argument("--plate-model", type=str, default=default_plate, help="車牌模型路徑 (.onnx 或 .pt)")
    parser.add_argument("--vehicle-model", type=str, default=default_vehicle, help="車輛模型路徑 (.onnx 或 .pt)")
    parser.add_argument("--enable-depth", dest="enable_depth", action="store_true", default=True, help="啟用 Depth Anything V2 Small 深度公尺測距 (預設開啟)")
    parser.add_argument("--no-depth", dest="enable_depth", action="store_false", help="關閉深度測距")
    default_depth = "checkpoints/depth_anything_v2_metric_vkitti_vits.onnx"
    parser.add_argument("--depth-ckpt", type=str, default=default_depth, help="Depth Anything V2 權重路徑 (.onnx)")
    parser.add_argument("--depth-size", type=int, default=392, help="Depth 模型輸入尺寸 (266 極速, 392 平衡, 518 高精度)")
    parser.add_argument("--depth-interval", type=int, default=2, help="深度測距推論間隔影格數 (預設 2，即每隔 2 幀推論一次，中間格沿用前幀深度，大幅提速 40%%)")
    parser.add_argument("--enable-ocr", dest="enable_ocr", action="store_true", default=True, help="啟用 RapidOCR 車牌字元辨識 (預設開啟)")
    parser.add_argument("--no-ocr", dest="enable_ocr", action="store_false", help="關閉車牌字元辨識")
    parser.add_argument("--ocr-ckpt", type=str, default="checkpoints/ch_PP-OCRv4_rec_infer.onnx", help="RapidOCR 文字辨識 ONNX 權重路徑")
    parser.add_argument("--ocr-device", type=str, default="cuda", help="OCR 推論裝置 (cuda 或 cpu，預設 cuda)")
    parser.add_argument("--ocr-min-plate-w", type=int, default=40, help="觸發 OCR 之車牌最小像素寬度 (低於此寬度視為太遠太模糊略過)")
    parser.add_argument("--ocr-conf", type=float, default=0.50, help="OCR 文字辨識最低信心門檻")
    parser.add_argument("--ocr-interval", type=int, default=4, help="未鎖定前同輛車抽樣辨識間隔影格數 (預設每 4 幀抽檢一次)")
    parser.add_argument("--plate-max-dist", type=float, default=25.0, help="車牌偵測與文字辨識之統一最大距離門檻 (公尺，預設 25.0m，超過此距離不切圖不跑車牌 YOLO 亦不跑 OCR)")
    parser.add_argument("--vehicle-interval", type=int, default=2, help="車輛目標偵測間隔影格數 (預設 2，即每隔 2 幀跑一次 YOLO 追蹤，中間幀採時序軌跡內插，算力減半且畫面極致平滑)")
    parser.add_argument("--taiwan-plate-filter", dest="taiwan_plate_filter", action="store_true", default=True, help="啟用台灣車牌規格校驗與字元消歧義修復 (依據交通部號牌法規，預設開啟)")
    parser.add_argument("--no-taiwan-plate-filter", dest="taiwan_plate_filter", action="store_false", help="關閉台灣車牌規格校驗")
    parser.add_argument("--track", action="store_true", default=True, help="是否開啟 persist=True 追蹤記憶")
    args = parser.parse_args()

    if not os.path.isfile(args.video):
        raise FileNotFoundError(f"找不到輸入影片: {args.video}")

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
            from rapidocr_onnxruntime import RapidOCR
            ocr_kwargs = {
                "det_use_cuda": use_ocr_cuda,
                "cls_use_cuda": use_ocr_cuda,
                "rec_use_cuda": use_ocr_cuda
            }
            if os.path.isfile(args.ocr_ckpt):
                ocr_kwargs["rec_model_path"] = args.ocr_ckpt
                print(f"🔤 載入本地車牌辨識權重: {args.ocr_ckpt}")
            ocr_model = RapidOCR(**ocr_kwargs)
            ocr_tracker = PlateOCRTracker(
                interval=args.ocr_interval,
                min_width=args.ocr_min_plate_w,
                conf_thresh=args.ocr_conf,
                max_dist=args.plate_max_dist,
                enable_taiwan_filter=args.taiwan_plate_filter
            )
            tw_status = "已啟用 (字軌法規約束 + 消歧義修復)" if args.taiwan_plate_filter else "未啟用"
            print(f"🔤 文字辨識模組: RapidOCR 載入成功 (純辨識模式，使用裝置: {'CUDA' if use_ocr_cuda else 'CPU'}，台灣車牌語法校驗: {tw_status})！")
        except Exception as e:
            print(f"⚠️ RapidOCR 載入失敗: {e}，將略過車牌文字辨識。")
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

            if args.two_stage:
                # 🔍 第二階段：僅從車輛區域裁切 (Crop) 進行批次車牌偵測 (排除行人與腳踏車)
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

                        # 🛑 只有車輛才需要找車牌，行人 (0) 和腳踏車 (1) 直接跳過
                        if cls not in PLATE_VEHICLE_CLASSES:
                            continue

                        # 📏 統一車牌距離門檻：超過指定公尺距離 (預設 25m)，不切圖、不跑車牌 YOLO、不跑 OCR
                        if dist_m > 0 and dist_m > args.plate_max_dist:
                            continue

                        # 過濾尺寸過小的遠距微小車輛
                        if vw < args.min_vehicle_size or vh < args.min_vehicle_size:
                            continue

                        # 外擴緩衝邊距 (Padding)，防止車輛框裁切到貼邊車牌
                        pad_w = int(vw * args.crop_padding)
                        pad_h = int(vh * args.crop_padding)
                        cx1 = max(0, vx1 - pad_w)
                        cy1 = max(0, vy1 - pad_h)
                        cx2 = min(fw, vx2 + pad_w)
                        cy2 = min(fh, vy2 + pad_h)

                        crop = frame_item[cy1:cy2, cx1:cx2]
                        if crop.shape[0] < 10 or crop.shape[1] < 10:
                            continue

                        crops_to_infer.append(crop)
                        crop_metadata.append({
                            "frame_idx": i,
                            "offset_x": cx1,
                            "offset_y": cy1,
                            "track_id": tid,
                            "veh_w": vw,
                            "veh_h": vh,
                            "veh_box": (vx1, vy1, vx2, vy2),
                            "dist_m": dist_m
                        })

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
                        tid = meta["track_id"]
                        vw = meta["veh_w"]
                        vh = meta["veh_h"]

                        candidates = []
                        if crop_res.boxes is not None and len(crop_res.boxes) > 0:
                            for pb in crop_res.boxes:
                                px1, py1, px2, py2 = pb.xyxy[0].cpu().numpy().tolist()
                                pconf = float(pb.conf[0].cpu().item())
                                pcls = int(pb.cls[0].cpu().item())
                                pw = px2 - px1
                                ph = py2 - py1
                                aspect = pw / max(ph, 1)
                                area_ratio = (pw * ph) / float(vw * vh)

                                # 🛡️ 幾何特徵過濾：車牌長寬比約在 1.3~4.2，且佔車身面積約 0.5%~25%（排除水箱護罩、橫條、正方形噪點）
                                if 1.3 <= aspect <= 4.2 and 0.005 <= area_ratio <= 0.25:
                                    gx1 = ox + px1
                                    gy1 = oy + py1
                                    gx2 = ox + px2
                                    gy2 = oy + py2
                                    candidates.append((pconf, [gx1, gy1, gx2, gy2], pcls))

                        if candidates:
                            # 🎯 每車只保留最高信心度的一個車牌 (Top-1，杜絕一車多框與位置跳躍)
                            if args.top1_per_vehicle:
                                candidates.sort(key=lambda c: c[0], reverse=True)
                                candidates = [candidates[0]]

                            for pconf, gbox, pcls in candidates:
                                final_box = gbox
                                # 🌊 跨影格平滑追蹤 (Temporal EMA Smoothing - 徹底消除每格抖動)
                                if args.smooth and tid is not None:
                                    if tid in plate_tracker:
                                        prev_box = plate_tracker[tid]["box"]
                                        prev_cx = (prev_box[0] + prev_box[2]) / 2.0
                                        curr_cx = (gbox[0] + gbox[2]) / 2.0
                                        # 限制單格橫向位移不得超過車寬 40%，防止瞬移
                                        if abs(curr_cx - prev_cx) < vw * 0.4:
                                            final_box = [
                                                args.smooth_alpha * gbox[k] + (1.0 - args.smooth_alpha) * prev_box[k]
                                                for k in range(4)
                                            ]
                                    plate_tracker[tid] = {"box": final_box, "conf": pconf, "lost": 0, "v_box": meta["veh_box"]}

                                # 🔤 第三階段：車牌字元辨識 (RapidOCR Recognition - 純文字識別)
                                p_text = None
                                t_conf = 0.0
                                if ocr_tracker is not None and ocr_model is not None:
                                    pw = final_box[2] - final_box[0]
                                    ph = final_box[3] - final_box[1]
                                    curr_frame_idx = processed_count + f_idx
                                    if ocr_tracker.should_infer(tid, curr_frame_idx, pw, dist_m=meta.get("dist_m")):
                                        # 微外擴 Padding，防止車牌裁切緊貼字元邊緣
                                        pad_px = int(pw * 0.05)
                                        pad_py = int(ph * 0.08)
                                        f_item = frame_queue[f_idx]
                                        px1 = max(0, int(round(final_box[0] - pad_px)))
                                        py1 = max(0, int(round(final_box[1] - pad_py)))
                                        px2 = min(f_item.shape[1], int(round(final_box[2] + pad_px)))
                                        py2 = min(f_item.shape[0], int(round(final_box[3] + pad_py)))
                                        plate_roi = f_item[py1:py2, px1:px2]
                                        if plate_roi.shape[0] >= 10 and plate_roi.shape[1] >= 20:
                                            try:
                                                t_o0 = time.perf_counter()
                                                ocr_res, _ = ocr_model(plate_roi, use_det=False, use_cls=False)
                                                time_ocr_total += (time.perf_counter() - t_o0)
                                                if ocr_res and len(ocr_res) > 0 and ocr_res[0][0]:
                                                    ocr_tracker.update(tid, curr_frame_idx, ocr_res[0][0], float(ocr_res[0][1]))
                                            except Exception:
                                                pass

                                    if tid is not None:
                                        p_text, t_conf = ocr_tracker.get_plate_raw(tid)

                                batch_plate_boxes[f_idx].append(final_box + [pconf, pcls, tid, p_text, t_conf])

                        elif args.smooth and tid is not None and tid in plate_tracker and plate_tracker[tid]["lost"] < 2:
                            # 🔄 掉幀平滑補償 (Holdover)：若車輛仍被追蹤但車牌因反光短暫遺失 1~2 格，跟隨車輛位移維持，避免狂閃
                            prev_vbox = plate_tracker[tid]["v_box"]
                            curr_vbox = meta["veh_box"]
                            dx = curr_vbox[0] - prev_vbox[0]
                            dy = curr_vbox[1] - prev_vbox[1]
                            held_box = [
                                plate_tracker[tid]["box"][0] + dx,
                                plate_tracker[tid]["box"][1] + dy,
                                plate_tracker[tid]["box"][2] + dx,
                                plate_tracker[tid]["box"][3] + dy
                            ]
                            plate_tracker[tid]["lost"] += 1
                            plate_tracker[tid]["box"] = held_box
                            plate_tracker[tid]["v_box"] = curr_vbox
                            held_conf = plate_tracker[tid]["conf"] * 0.9
                            held_p_text = None
                            held_t_conf = 0.0
                            if ocr_tracker is not None:
                                held_p_text, held_t_conf = ocr_tracker.get_plate_raw(tid)
                            batch_plate_boxes[f_idx].append(held_box + [held_conf, 0, tid, held_p_text, held_t_conf])

                # 對每格車牌進行 NMS 去重
                for i in range(len(batch_plate_boxes)):
                    batch_plate_boxes[i] = filter_duplicate_boxes(batch_plate_boxes[i], iou_thresh=0.45)

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
                            tid = ids[b]
                            p_text = None
                            t_conf = 0.0
                            if ocr_tracker is not None and ocr_model is not None:
                                pw = xyxy[2] - xyxy[0]
                                curr_frame_idx = processed_count + i
                                if ocr_tracker.should_infer(tid, curr_frame_idx, pw):
                                    f_item = frame_queue[i]
                                    px1 = max(0, int(round(xyxy[0])))
                                    py1 = max(0, int(round(xyxy[1])))
                                    px2 = min(f_item.shape[1], int(round(xyxy[2])))
                                    py2 = min(f_item.shape[0], int(round(xyxy[3])))
                                    plate_roi = f_item[py1:py2, px1:px2]
                                    if plate_roi.shape[0] >= 10 and plate_roi.shape[1] >= 20:
                                        try:
                                            ocr_res, _ = ocr_model(plate_roi, use_det=False, use_cls=False)
                                            if ocr_res and len(ocr_res) > 0 and ocr_res[0][0]:
                                                ocr_tracker.update(tid, curr_frame_idx, ocr_res[0][0], float(ocr_res[0][1]))
                                        except Exception:
                                            pass
                                if tid is not None:
                                    p_text, t_conf = ocr_tracker.get_plate_raw(tid)
                            batch_plate_boxes[i].append(xyxy + [conf, cls, tid, p_text, t_conf])

            # 雙軌映射：影片畫面顯示原版 OCR 文字，下游外出資料採用台灣法規補償文字
            video_plate_map = {}
            vehicle_plate_map = {}
            if ocr_tracker is not None:
                for v_tid, rec in ocr_tracker.records.items():
                    if rec.get("best_raw_text"):
                        video_plate_map[v_tid] = rec["best_raw_text"]
                    if rec.get("best_compensated_text"):
                        vehicle_plate_map[v_tid] = rec["best_compensated_text"]

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
        print(f"  🔤 車牌辨識 (RapidOCR)     : {time_ocr_total*1000/processed_count:5.1f} ms/幀 ({time_ocr_total/t_sum*100:4.1f}%)")
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
