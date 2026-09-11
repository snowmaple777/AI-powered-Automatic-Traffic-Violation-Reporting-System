import argparse
import cv2
import os
import sys
import numpy as np
import torch
from ultralytics import YOLO

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

def draw_boxes(img, boxes, names, color, label_prefix=""):
    for box in boxes:
        x1, y1, x2, y2 = map(int, box[:4])
        conf = box[4]
        cls = int(box[5]) if len(box) > 5 else 0
        track_id = int(box[6]) if len(box) > 6 and box[6] is not None else None
        dist_m = float(box[7]) if len(box) > 7 and box[7] is not None else None
        
        class_name = names.get(cls, str(cls))
        dist_str = f" [{dist_m:.1f}m]" if (dist_m is not None and dist_m > 0) else ""
        
        # 如果有追蹤 ID (track_id)，顯示包含 ID 與距離的標籤 (例: car #1 [14.2m]: 0.85)
        if track_id is not None:
            if label_prefix:
                display_text = f"{label_prefix} #{track_id}: {conf:.2f}"
            else:
                display_text = f"{class_name} #{track_id}{dist_str}: {conf:.2f}"
        else:
            display_text = f"{label_prefix}: {conf:.2f}" if label_prefix else f"{class_name}{dist_str}: {conf:.2f}"
        
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        (w, h), _ = cv2.getTextSize(display_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img, (x1, max(0, y1 - 20)), (x1 + w + 6, max(0, y1)), color, -1)
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
    parser.add_argument("--imgsz-plate", type=int, default=640, help="車牌推論解析度 (裁切模式下符合原生 640 尺度)")
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
    parser.add_argument("--plate-model", type=str, default="license-plate-finetune-v1s.pt", help="車牌模型路徑")
    parser.add_argument("--vehicle-model", type=str, default="yolo26s.pt", help="車輛模型路徑")
    parser.add_argument("--enable-depth", dest="enable_depth", action="store_true", default=True, help="啟用 Depth Anything V2 Small 深度公尺測距 (預設開啟)")
    parser.add_argument("--no-depth", dest="enable_depth", action="store_false", help="關閉深度測距")
    parser.add_argument("--depth-ckpt", type=str, default="checkpoints/depth_anything_v2_metric_vkitti_vits.pth", help="Depth Anything V2 Small 權重路徑")
    parser.add_argument("--depth-size", type=int, default=392, help="Depth 模型輸入尺寸 (266 極速, 392 平衡, 518 高精度)")
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

    vehicle_model = YOLO(args.vehicle_model)
    plate_model = YOLO(args.plate_model)
    vehicle_model.to(device)
    plate_model.to(device)

    depth_model = None
    if args.enable_depth:
        if os.path.isfile(args.depth_ckpt):
            try:
                depth_repo_path = os.path.abspath("Depth-Anything-V2/metric_depth")
                if depth_repo_path not in sys.path:
                    sys.path.append(depth_repo_path)
                from depth_anything_v2.dpt import DepthAnythingV2
                depth_model = DepthAnythingV2(**{'encoder': 'vits', 'features': 64, 'out_channels': [48, 96, 192, 384], 'max_depth': 80})
                depth_model.load_state_dict(torch.load(args.depth_ckpt, map_location='cpu'))
                depth_model = depth_model.to(device).eval()
                print("📏 深度測距模組: Depth-Anything-V2-Metric-VKITTI-Small 載入成功！")
            except Exception as e:
                print(f"⚠️ 深度模組載入失敗: {e}，將略過深度測距。")
                depth_model = None
        else:
            print(f"⚠️ 找不到深度模型權重: {args.depth_ckpt}，將略過深度測距。")

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

    while True:
        ret, frame = cap.read()
        if ret:
            frame_queue.append(frame)
        
        if len(frame_queue) == batch_size or (not ret and len(frame_queue) > 0):
            # 🚗 第一階段：全目標偵測與追蹤 (行人、腳踏車、汽車、機車、公車、卡車)
            if args.track:
                v_results_batch = vehicle_model.track(
                    frame_queue, persist=True, classes=TARGET_CLASSES,
                    conf=args.conf_vehicle, imgsz=args.imgsz_vehicle,
                    device=device, verbose=False
                )
            else:
                v_results_batch = vehicle_model(
                    frame_queue, classes=TARGET_CLASSES,
                    conf=args.conf_vehicle, imgsz=args.imgsz_vehicle,
                    device=device, verbose=False
                )

            # 📏 深度估算：取得當前 Batch 各影格的真實公尺深度圖
            depth_maps = []
            if depth_model is not None:
                with torch.no_grad():
                    with torch.amp.autocast('cuda') if device == 'cuda' else torch.no_grad():
                        for f_item in frame_queue:
                            d_map = depth_model.infer_image(f_item, input_size=args.depth_size)
                            depth_maps.append(d_map)

            batch_vehicle_boxes = [[] for _ in range(len(frame_queue))]
            batch_plate_boxes = [[] for _ in range(len(frame_queue))]

            # 提取邊界框、追蹤 ID 與真實公尺距離
            for i, v_res in enumerate(v_results_batch):
                if v_res.boxes is not None and len(v_res.boxes) > 0:
                    boxes = v_res.boxes
                    ids = boxes.id.cpu().numpy().tolist() if boxes.id is not None else [None] * len(boxes)
                    for b in range(len(boxes)):
                        xyxy = boxes.xyxy[b].cpu().numpy().tolist()
                        conf = float(boxes.conf[b].cpu().item())
                        cls = int(boxes.cls[b].cpu().item())
                        tid = ids[b]

                        # 🎯 從深度圖取目標中央 50% 區域的中位數 (Median Depth)，免除邊界背景穿透干擾
                        dist_m = 0.0
                        if i < len(depth_maps) and depth_maps[i] is not None:
                            d_map = depth_maps[i]
                            x1, y1, x2, y2 = map(int, xyxy)
                            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                            rw = max(2, int((x2 - x1) * 0.5))
                            rh = max(2, int((y2 - y1) * 0.5))
                            rx1, rx2 = max(0, cx - rw // 2), min(d_map.shape[1], cx + rw // 2)
                            ry1, ry2 = max(0, cy - rh // 2), min(d_map.shape[0], cy + rh // 2)
                            roi = d_map[ry1:ry2, rx1:rx2]
                            if roi.size > 0:
                                dist_m = float(np.median(roi))

                        batch_vehicle_boxes[i].append(xyxy + [conf, cls, tid, dist_m])

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
                        vw = vx2 - vx1
                        vh = vy2 - vy1

                        # 🛑 只有車輛才需要找車牌，行人 (0) 和腳踏車 (1) 直接跳過
                        if cls not in PLATE_VEHICLE_CLASSES:
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
                            "veh_box": (vx1, vy1, vx2, vy2)
                        })

                if crops_to_infer:
                    # 批次送入車牌模型（原生 640 尺度，無尺度失配）
                    p_results = plate_model(
                        crops_to_infer,
                        imgsz=args.imgsz_plate,
                        conf=args.conf_plate,
                        batch=args.crop_batch_size,
                        device=device,
                        verbose=False
                    )

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

                                batch_plate_boxes[f_idx].append(final_box + [pconf, pcls, tid])

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
                            batch_plate_boxes[f_idx].append(held_box + [held_conf, 0, tid])

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
                            batch_plate_boxes[i].append(xyxy + [conf, cls, tid])

            # 繪製標註框並寫入影片，同時統計信心值
            for i, f in enumerate(frame_queue):
                v_boxes = batch_vehicle_boxes[i]
                p_boxes = batch_plate_boxes[i]

                for vb in v_boxes:
                    all_vehicle_confs.append(vb[4])
                for pb in p_boxes:
                    all_plate_confs.append(pb[4])

                if v_boxes:
                    f = draw_boxes(f, v_boxes, vehicle_model.names, color=(255, 140, 0))
                if p_boxes:
                    f = draw_boxes(f, p_boxes, plate_model.names, color=(0, 0, 255), label_prefix="Plate")

                out.write(f)

            processed_count += len(frame_queue)
            frame_queue.clear()

            if processed_count % 80 == 0 or processed_count == total_frames:
                current_plate_avg = (sum(all_plate_confs) / len(all_plate_confs)) if all_plate_confs else 0.0
                print(f"⚡ 進度: {processed_count}/{total_frames} 影格 | 當前車牌平均信心值: {current_plate_avg:.3f} (累計 {len(all_plate_confs)} 框)")

            if args.max_frames > 0 and processed_count >= args.max_frames:
                print(f"🛑 已達到最大指定影格數 ({args.max_frames})，提早結束處理。")
                break

        if not ret:
            break

    cap.release()
    out.release()
    print(f"\n✅ 處理完全結束！標註影片已儲存至: {args.output}")

    # 📊 輸出完整信心值統計報告
    print("\n" + "=" * 50)
    print("📊 偵測與信心值統計報告 (Detection & Confidence Summary)")
    print("=" * 50)
    print(f"總處理影格數: {processed_count} / {total_frames}")

    if all_vehicle_confs:
        avg_v = sum(all_vehicle_confs) / len(all_vehicle_confs)
        print(f"🚗 車輛偵測總次數: {len(all_vehicle_confs)} 框")
        print(f"   - 平均信心值: {avg_v:.3f}")
        print(f"   - 最高信心值: {max(all_vehicle_confs):.3f}")
        print(f"   - 最低信心值: {min(all_vehicle_confs):.3f}")
    else:
        print("🚗 車輛偵測: 未偵測到車輛")

    print("-" * 50)
    if all_plate_confs:
        avg_p = sum(all_plate_confs) / len(all_plate_confs)
        print(f"🪪 車牌偵測總次數: {len(all_plate_confs)} 框")
        print(f"   - 平均信心值: {avg_p:.3f}")
        print(f"   - 最高信心值: {max(all_plate_confs):.3f}")
        print(f"   - 最低信心值: {min(all_plate_confs):.3f}")
    else:
        print("🪪 車牌偵測: 未偵測到車牌")
    print("=" * 50 + "\n")

if __name__ == "__main__":
    main()
