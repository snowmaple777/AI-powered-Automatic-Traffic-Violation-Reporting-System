"""Run the bundled traffic-light detector on images, videos or a webcam."""

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Image, video, directory or webcam index (0)")
    parser.add_argument("--weights", type=Path, default=ROOT / "models/traffic_light/traffic_light_best.pt")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold (0 to 1)")
    parser.add_argument("--imgsz", type=int, default=800, help="Inference image size")
    parser.add_argument("--device", default="cpu", help="cpu, 0 (CUDA GPU), or mps")
    parser.add_argument("--show", action="store_true", help="Display detections in a window")
    args = parser.parse_args()
    if not 0 <= args.conf <= 1:
        parser.error("--conf must be between 0 and 1")
    if args.imgsz <= 0:
        parser.error("--imgsz must be positive")
    if not args.weights.is_file():
        parser.error("Model file not found: {}".format(args.weights))
    return args


def main():
    args = parse_args()
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt") from exc

    model = YOLO(str(args.weights))
    source = int(args.source) if args.source.isdecimal() else args.source
    # Consume results one frame at a time to keep long videos from filling RAM.
    results = model.predict(
        source=source,
        conf=args.conf,
        imgsz=args.imgsz,
        device=args.device,
        stream=True,
        save=True,
        save_txt=True,
        save_conf=True,
        show=args.show,
        project=str(args.output),
        name="predict",
        exist_ok=False,
    )
    frames = 0
    save_dir = None
    for result in results:
        frames += 1
        save_dir = result.save_dir
    print("Processed {} image(s)/frame(s).".format(frames))
    if save_dir:
        print("Results saved to: {}".format(save_dir))


if __name__ == "__main__":
    main()
