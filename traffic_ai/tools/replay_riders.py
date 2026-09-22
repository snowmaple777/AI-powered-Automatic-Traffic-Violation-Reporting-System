"""Add rider associations to saved detections and render the source video."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
from rider_tracking import RiderTracker, attach_riders, draw_rider_groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('detections', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / (args.video.stem+'_riders.mp4')
    data = args.output_dir / (args.video.stem+'_riders_detections.jsonl')
    if output.resolve() == args.video.resolve() or data.resolve() == args.detections.resolve():
        parser.error('Output must not overwrite input')
    cap = cv2.VideoCapture(str(args.video))
    writer = None
    counts = {}
    try:
        if not cap.isOpened():
            raise RuntimeError('Cannot open source video')
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        width, height = int(cap.get(3)), int(cap.get(4))
        tracker = RiderTracker(fps)
        writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*'mp4v'), fps, (width,height))
        if not writer.isOpened():
            raise RuntimeError('Cannot create output video')
        with args.detections.open(encoding='utf-8') as source, data.open('w',encoding='utf-8') as destination:
            for expected, line in enumerate(source):
                record = json.loads(line)
                if record['frame']['index'] != expected:
                    raise ValueError('Replay requires contiguous detections starting at frame zero')
                ok, frame = cap.read()
                if not ok:
                    raise ValueError('Video ends before detections')
                if (width,height) != (record['frame']['width'],record['frame']['height']):
                    raise ValueError('Detection and video dimensions differ')
                attach_riders(record,tracker)
                groups = record['observations']['rider_groups']
                draw_rider_groups(frame,groups)
                cv2.rectangle(frame,(0,0),(min(width-1,1050),35),(20,20,20),-1)
                cv2.putText(frame,'RIDER GROUPS: green=confirmed / orange=pending | dot=vehicle contact proxy',
                            (12,25),cv2.FONT_HERSHEY_SIMPLEX,.6,(255,255,255),2,cv2.LINE_AA)
                writer.write(frame)
                destination.write(json.dumps(record,ensure_ascii=False)+'\n')
                for g in groups:
                    if g['status']=='confirmed':
                        counts[g['object_id']] = counts.get(g['object_id'],0)+1
    finally:
        cap.release()
        if writer is not None:
            writer.release()
    print(json.dumps({'video':str(output),'data':str(data),'confirmed_frames':counts}))


if __name__=='__main__':
    main()
