"""用原影片與既有 JSONL 重算補償，不重跑模型；輸出可交給原違規引擎。"""
import argparse
from collections import Counter
from contextlib import ExitStack
import json
from pathlib import Path
import cv2
from observation_schema import SCHEMA_VERSION
from road_compensation import CompensationConfig, RoadMarkingCompensator, attach_compensation
from road_compensation.render import draw_compensation

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video",type=Path)
    parser.add_argument("detections",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--config",type=Path,default=ROOT/"configs/marking_compensation.json")
    parser.add_argument("--max-frames",type=int,default=0)
    parser.add_argument("--save-video",action="store_true")
    args=parser.parse_args()
    if args.output.resolve() in (args.detections.resolve(),args.video.resolve()):
        parser.error("輸出不能覆蓋來源檔案")
    if not args.video.is_file() or not args.detections.is_file():
        parser.error("影片與 JSONL 來源檔案都必須存在")
    if args.save_video and args.output.with_suffix(".mp4").resolve() in (args.video.resolve(),args.detections.resolve(),args.output.resolve()):
        parser.error("標記影片輸出不能覆蓋來源或 JSONL 輸出")
    if args.max_frames<0:
        parser.error("--max-frames 必須 >= 0")
    compensator=RoadMarkingCompensator(CompensationConfig.from_json(args.config))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    counts=Counter()
    with ExitStack() as resources:
        cap=cv2.VideoCapture(str(args.video))
        resources.callback(cap.release)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {args.video}")
        writer=None
        if args.save_video:
            writer=cv2.VideoWriter(str(args.output.with_suffix(".mp4")),cv2.VideoWriter_fourcc(*"mp4v"),
                                   cap.get(cv2.CAP_PROP_FPS) or 30.,
                                   (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))))
            resources.callback(writer.release)
            if not writer.isOpened():
                raise ValueError("Cannot create annotated video")
        source=resources.enter_context(args.detections.open(encoding="utf-8"))
        destination=resources.enter_context(args.output.open("w",encoding="utf-8"))
        for index,line in enumerate(source):
            if args.max_frames and index>=args.max_frames:
                break
            record=json.loads(line)
            ok,frame=cap.read()
            metadata=record["frame"]
            # 須使用相同影片且從第 0 幀開始；不允許錯位影像配上另一幀的標線。
            if not ok or metadata["index"]!=index or frame.shape[:2]!=(metadata["height"],metadata["width"]):
                raise ValueError(f"Video/JSONL frame mismatch at {index}")
            video_fps=cap.get(cv2.CAP_PROP_FPS)
            if video_fps>0 and (abs(video_fps-metadata["fps"])>.05 or abs(metadata["timestamp_sec"]-index/video_fps)>.002):
                raise ValueError(f"Video/JSONL timing mismatch at {index}")
            obs=record["observations"]
            result=compensator.process(frame,frame_index=index,timestamp_sec=metadata["timestamp_sec"],
                                      road_markings=obs.get("road_markings_raw",obs["road_markings"]),vehicles=obs["vehicles"])
            record["schema_version"]=SCHEMA_VERSION
            destination.write(json.dumps(attach_compensation(record,result),ensure_ascii=False,allow_nan=False)+"\n")
            counts["frames"]+=1
            counts["motion_valid"]+=int(result.diagnostics["motion"]["valid"])
            counts["usable_frames"]+=int(result.diagnostics["status"]=="usable")
            for item in result.compensated_markings:
                for component in item["components"]:
                    if "compensation" in component:
                        counts[component["compensation"]["source"]]+=1
            if writer:
                writer.write(draw_compensation(frame.copy(),result))
    print(json.dumps(dict(counts),ensure_ascii=False))


if __name__=="__main__":
    main()
