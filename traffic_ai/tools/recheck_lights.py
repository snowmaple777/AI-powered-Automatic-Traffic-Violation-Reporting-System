"""Compare traffic-light input resolutions without rerunning other models."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
from model_interfaces import TrafficLightDetector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, default=1600)
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()
    if args.output.resolve() == args.video.resolve() or not args.video.is_file():
        parser.error("輸入影片須存在，輸出不可覆蓋原影片")
    import torch
    device = ("cuda:0" if torch.cuda.is_available() else "cpu") if args.device == "auto" else args.device
    model = TrafficLightDetector(ROOT / "models/traffic_light/traffic_light_best.pt",
                                 device=device, imgsz=args.imgsz)
    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise ValueError("Cannot open video")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("w", encoding="utf-8") as output:
            index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                output.write(json.dumps({"frame": index, "traffic_lights": model.infer(frame)}) + "\n")
                index += 1
        print(f"Processed {index} frames")
    finally:
        cap.release()


if __name__ == "__main__":
    main()
