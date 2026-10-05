# -*- coding: utf-8 -*-
"""
PP-OCRv6 台灣車牌專屬推論 Socket Server (獨立進程，透過 localhost socket 通訊)
"""

import os
import sys
import math
import re
import socket
import struct
import cv2
import numpy as np

# 確保優先載入 nvidia cudnn
cudnn_bin = r'C:\Users\User\AppData\Local\Programs\Python\Python310\lib\site-packages\nvidia\cudnn\bin'
if os.path.isdir(cudnn_bin) and hasattr(os, 'add_dll_directory'):
    try:
        os.add_dll_directory(cudnn_bin)
    except Exception:
        pass

import paddle.inference as paddle_infer

class TaiwanPlateValidator:
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
        '3': 'B',
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
        s = str(raw_text).upper().strip()
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
            if len(p1) == 3 and len(p2) == 4:
                p1_c = cls.fix_letters(p1)
                # 台灣新式 7 碼車牌 (XXX-0000) 尚未發放 G 字頭 (小客貨車目前僅發至 B/C 字頭)，
                # 當車牌左側受邊框或轉彎視角微遮擋時，B 極易被誤判為 G 或 6
                if p1_c[0] in ('G', '6', 'J'):
                    p1_c = 'B' + p1_c[1:]
                p2_c = cls.fix_digits(p2)
                c = f"{p1_c}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z]{3}-[0-9]{4}$', c):
                    return c
            if len(p1) == 3 and len(p2) == 3:
                p1_c = cls.fix_letters(p1)
                p2_c = cls.fix_digits(p2)
                c = f"{p1_c}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z]{3}-[0-9]{3}$', c):
                    return c
                p1_d = cls.fix_digits(p1)
                p2_l = cls.fix_letters(p2)
                c_rev = f"{p1_d}-{p2_l}"
                if re.match(r'^[0-9]{3}-[A-HJ-NP-Z]{3}$', c_rev):
                    return c_rev
            if len(p1) == 2 and len(p2) == 4:
                p2_c = cls.fix_digits(p2)
                c = f"{p1}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z0-9]{2}-[0-9]{4}$', c):
                    if re.search(r'[A-HJ-NP-Z]', p1) or p1 in ('22', '88', '66', '99'):
                        return c
            if len(p1) == 4 and len(p2) == 2:
                p1_c = cls.fix_digits(p1)
                c = f"{p1_c}-{p2}"
                if re.match(r'^[0-9]{4}-[A-HJ-NP-Z0-9]{2}$', c):
                    if re.search(r'[A-HJ-NP-Z]', p2) or p2 in ('22', '88', '66', '99'):
                        return c
            if len(p1) == 2 and len(p2) == 3:
                p2_c = cls.fix_digits(p2)
                c = f"{p1}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z0-9]{2}-[0-9]{3}$', c) and re.search(r'[A-HJ-NP-Z]', p1):
                    return c
            if len(p1) == 3 and len(p2) == 2:
                p1_c = cls.fix_digits(p1)
                c = f"{p1_c}-{p2}"
                if re.match(r'^[0-9]{3}-[A-HJ-NP-Z0-9]{2}$', c) and re.search(r'[A-HJ-NP-Z]', p2):
                    return c
            return None

        if '-' in s:
            valid = check_candidate(s)
            if valid:
                return valid
        pure = s.replace('-', '')
        if len(pure) == 7:
            valid = check_candidate(f"{pure[:3]}-{pure[3:]}")
            if valid:
                return valid
        elif len(pure) == 6:
            for c_str in (f"{pure[:3]}-{pure[3:]}", f"{pure[:2]}-{pure[2:]}", f"{pure[:4]}-{pure[4:]}"):
                valid = check_candidate(c_str)
                if valid:
                    return valid
        elif len(pure) == 5:
            for c_str in (f"{pure[:2]}-{pure[2:]}", f"{pure[:3]}-{pure[3:]}"):
                valid = check_candidate(c_str)
                if valid:
                    return valid
        return None

class PPOCRWorker:
    def __init__(self, model_dir="checkpoints/PP-OCRv6_taiwan_infer", dict_path="train_data/ppocrv6_dict.txt", device="cuda"):
        model_dir = os.path.abspath(model_dir)
        if os.path.isfile(model_dir):
            model_dir = os.path.dirname(model_dir)
        model_file = os.path.join(model_dir, "inference.json")
        params_file = os.path.join(model_dir, "inference.pdiparams")
        if not os.path.isfile(model_file) or not os.path.isfile(params_file):
            raise FileNotFoundError(f"找不到 Paddle Inference 模型: {model_file} 或 {params_file}")

        config = paddle_infer.Config(model_file, params_file)
        if device == "cuda":
            config.enable_use_gpu(100, 0)
            config.enable_memory_optim()
        else:
            config.disable_gpu()
            config.set_cpu_math_library_num_threads(4)

        self.predictor = paddle_infer.create_predictor(config)
        self.input_name = self.predictor.get_input_names()[0]
        self.output_name = self.predictor.get_output_names()[0]

        dict_cand = [
            dict_path,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), dict_path),
            "train_data/ppocrv6_dict.txt",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_data/ppocrv6_dict.txt")
        ]
        resolved_dict = next((p for p in dict_cand if os.path.isfile(p)), None)
        if not resolved_dict:
            raise FileNotFoundError(f"找不到字元字典檔案: {dict_path}")

        with open(resolved_dict, "r", encoding="utf-8") as f:
            lines = [line.strip("\r\n") for line in f]
        self.character = ["blank"] + lines + [" "]
        self.allowed_indices = np.array([0] + [i for i, c in enumerate(self.character) if c in '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-'], dtype=np.int64)
        self.mask = np.zeros(len(self.character), dtype=np.float32)
        self.mask[self.allowed_indices] = 1.0

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
        imgC, imgH, imgW = 3, 48, 320
        ratio = w / float(h)
        if math.ceil(imgH * ratio) > imgW:
            resized_w = imgW
        else:
            resized_w = max(16, int(math.ceil(imgH * ratio)))
        resized = cv2.resize(img, (resized_w, imgH), interpolation=cv2.INTER_LINEAR)
        resized = (resized.astype(np.float32) / 255.0 - 0.5) / 0.5
        padding_im = np.zeros((1, imgC, imgH, imgW), dtype=np.float32)
        padding_im[0, :, :, 0:resized_w] = resized.transpose((2, 0, 1))

        input_tensor = self.predictor.get_input_handle(self.input_name)
        input_tensor.reshape(padding_im.shape)
        input_tensor.copy_from_cpu(padding_im)
        self.predictor.run()

        output_tensor = self.predictor.get_output_handle(self.output_name)
        preds = output_tensor.copy_to_cpu()[0] # [T, 18710]

        # 模型輸出已具備 Softmax，直接以英數遮罩過濾非車牌字元
        masked_preds = preds * self.mask
        indices = np.argmax(masked_preds, axis=-1)

        res_chars = []
        conf_scores = []
        last_idx = -1
        for t, idx in enumerate(indices):
            if idx != 0 and idx != last_idx and idx < len(self.character):
                char = self.character[idx]
                if char not in ('blank', ' '):
                    res_chars.append(char)
                    allowed_sum = np.sum(preds[t, self.allowed_indices])
                    renorm_conf = preds[t, idx] / max(allowed_sum, 1e-6)
                    conf_scores.append(float(renorm_conf))
            last_idx = idx

        text = "".join(res_chars)
        conf = float(np.mean(conf_scores)) if conf_scores else 0.0
        return text, conf

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
            return "", 0.0

        def score_cand(txt, conf, valid_norm):
            if not valid_norm:
                return conf
            # 完整 7 碼新式車牌 (XXX-0000，含連字號長 8) 具備更高資訊完整度，避免被斜角漏字之 6 碼覆蓋
            bonus = 1.15 if len(valid_norm) == 8 else 1.0
            return conf + bonus

        # 第一步：原圖推論 (高速通道)
        orig_text, orig_conf = self._infer_single(img)
        orig_valid = TaiwanPlateValidator.validate_and_normalize(orig_text) if orig_text else None

        # 若原圖已符合標準長度且信心度高 (>= 0.88)，直接採用 (僅需 ~7ms)
        if orig_valid and orig_conf >= 0.88 and (len(orig_valid) == 8 or (img.shape[1] / max(1.0, float(img.shape[0]))) >= 1.9):
            return orig_text, orig_conf

        # 第二步：啟動自適應超解析銳化 + 保邊降噪 + 局部對比增強
        enh_img = self._enhance_plate(img)
        enh_text, enh_conf = self._infer_single(enh_img)
        enh_valid = TaiwanPlateValidator.validate_and_normalize(enh_text) if enh_text else None

        # 候選人優先權仲裁 (符合車牌規範格式獲得加權)
        s_orig = score_cand(orig_text, orig_conf, orig_valid)
        s_enh = score_cand(enh_text, enh_conf, enh_valid)

        if s_enh > s_orig:
            best_text, best_conf, best_valid = enh_text, enh_conf, enh_valid
            best_score = s_enh
        else:
            best_text, best_conf, best_valid = orig_text, orig_conf, orig_valid
            best_score = s_orig

        # 若車牌因轉彎/側面斜視角導致長寬比偏方 (< 1.95)，測試水平拉寬 1.35 倍還原擠壓字元
        h, w = img.shape[:2]
        if (w / max(1.0, float(h))) < 1.95 and (not best_valid or len(best_valid) < 8 or best_conf < 0.88):
            for st_src in (img, enh_img):
                st_img = cv2.resize(st_src, (int(round(st_src.shape[1] * 1.35)), st_src.shape[0]), interpolation=cv2.INTER_CUBIC)
                st_text, st_conf = self._infer_single(st_img)
                if st_text:
                    st_valid = TaiwanPlateValidator.validate_and_normalize(st_text)
                    st_score = score_cand(st_text, st_conf, st_valid)
                    if st_score > best_score:
                        best_score = st_score
                        best_text, best_conf, best_valid = st_text, st_conf, st_valid

        # 第三步：若仍不合規或信心度偏低 (< 0.70)，在增強影像上嘗試微調傾角校正
        if not best_valid or best_conf < 0.70:
            for angle in [-10, 10, -5, 5]:
                rot_img = self._rotate_crop(enh_img, angle)
                r_text, r_conf = self._infer_single(rot_img)
                if not r_text:
                    continue
                r_valid = TaiwanPlateValidator.validate_and_normalize(r_text)
                r_score = score_cand(r_text, r_conf, r_valid)
                if r_score > best_score:
                    best_score = r_score
                    best_text = r_text
                    best_conf = r_conf
                    best_valid = r_valid
                    if r_valid and r_conf >= 0.85:
                        break

        return (best_text or ""), float(best_conf)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True, help="Parent listening port")
    parser.add_argument("--model-dir", type=str, default="checkpoints/PP-OCRv6_taiwan_infer")
    parser.add_argument("--dict-path", type=str, default="train_data/ppocrv6_dict.txt")
    parser.add_argument("--device", type=str, default="cuda")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", args.port))

    try:
        worker = PPOCRWorker(model_dir=args.model_dir, dict_path=args.dict_path, device=args.device)
        sock.sendall(b"READY\n")
    except Exception as e:
        err_msg = f"ERROR: {e}\n".encode("utf-8")
        sock.sendall(err_msg)
        sock.close()
        sys.exit(1)

    while True:
        hdr = recv_exact(sock, 8)
        if not hdr:
            break
        h, w = struct.unpack(">II", hdr)
        if h == 0 or w == 0:
            break
        raw_len = h * w * 3
        img_bytes = recv_exact(sock, raw_len)
        if not img_bytes:
            break

        img = np.frombuffer(img_bytes, dtype=np.uint8).reshape((h, w, 3))
        text, conf = worker.predict(img)

        encoded_text = text.encode("utf-8")
        resp_hdr = struct.pack(">If", len(encoded_text), float(conf))
        sock.sendall(resp_hdr + encoded_text)

    sock.close()

if __name__ == "__main__":
    main()
