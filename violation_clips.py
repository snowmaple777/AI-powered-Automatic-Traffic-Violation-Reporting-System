"""Export annotated event windows from the source video after rule finalization."""
import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

import cv2

from stream_source import open_capture
from violation_visualization import draw_violation_candidates, format_event_summary

RAW_JUMP_SECONDS = 10.  # 原始片段：下一段離目前位置超過這麼遠就直接定位，不逐格讀過去（長片）


def add_clip_arguments(parser):
    parser.add_argument('--violation-clips', action=argparse.BooleanOptionalAction, default=True,
                        help='自動輸出違規片段；--no-violation-clips 停用')
    parser.add_argument('--clip-before', type=float, default=5., help='事件前保留秒數，預設 5')
    parser.add_argument('--clip-after', type=float, default=5., help='事件後保留秒數，預設 5')
    parser.add_argument('--clip-raw', action='store_true',
                        help='片段不畫標註、保留原始畫面（長片切段：片段之後要再分析）；優先輸出 H.264')


class _RawWriter:
    """原始片段的寫入器：優先 H.264（Windows Media Foundation，瀏覽器可直接播），不行就 mp4v。
    Media Foundation 開不了非 ASCII 路徑（例如「新增資料夾」），先寫到系統暫存資料夾再搬到目的地。"""

    def __init__(self, path, fps, size):
        self.path, self.codec, self.temp = Path(path), 'mp4v', None
        self.writer = None
        msmf = getattr(cv2, 'CAP_MSMF', None)
        if msmf is not None and os.name == 'nt':
            handle, temp = tempfile.mkstemp(suffix='.mp4', prefix='clip_')
            os.close(handle)
            if temp.isascii():
                writer = cv2.VideoWriter(temp, msmf, cv2.VideoWriter_fourcc(*'avc1'), fps, size)
                if writer.isOpened():
                    self.writer, self.temp, self.codec = writer, temp, 'h264'
                else:
                    writer.release()
            if self.writer is None:
                os.remove(temp)
        if self.writer is None:
            self.writer = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*'mp4v'), fps, size)

    def isOpened(self):  # noqa: N802 （與 cv2.VideoWriter 同名）
        return self.writer.isOpened()

    def write(self, frame):
        self.writer.write(frame)

    def release(self):
        if self.writer is None:
            return
        self.writer.release()
        self.writer = None
        if self.temp:
            shutil.move(self.temp, self.path)
            self.temp = None


def validate_clip_options(before, after):
    if any(not math.isfinite(value) or value < 0 for value in (before, after)):
        raise ValueError('違規片段前後秒數必須是有限的非負數')


def _observations(path):
    with Path(path).open(encoding='utf-8') as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def export_violation_clips(video_path, observations_path, events, output_dir,
                           before_seconds=5., after_seconds=5., raw=False):
    """One video pass and bounded observation memory; return exported clip metadata.

    OpenCV does not copy source audio. Windows stop at the last analyzed frame,
    including when --max-frames was used.
    raw：不畫標註、保留原始畫面（長片切段，片段之後要再分析），優先 H.264；片段之間相隔較遠時直接定位，不逐格讀過去。
    """
    validate_clip_options(before_seconds, after_seconds)
    events = [e for e in events if e.get('status') in {'suspected', 'confirmed'}]
    if not events:
        return []
    first, last = None, None
    for record in _observations(observations_path):
        info = record['frame']
        if last is not None and info['index'] <= last['index']:
            raise ValueError('辨識資料的影格必須依序且不得重複')
        if first is None:
            first = info
        last = info
    if first is None:
        raise ValueError('沒有可用的逐幀辨識資料')

    with ExitStack() as resources:
        cap = open_capture(video_path)  # 串流模式傳入分段資料夾，從分段剪（4bq）
        resources.callback(cap.release)
        if not cap.isOpened():
            raise RuntimeError(f'無法開啟原影片：{video_path}')
        fps = cap.get(cv2.CAP_PROP_FPS)
        size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('原影片 FPS 無效')
        if size != (first['width'], first['height']) or not math.isclose(fps, first['fps'], rel_tol=.001):
            raise ValueError('原影片尺寸或 FPS 與辨識資料不符')
        jobs = []
        for event in sorted(events, key=lambda e: e['frame']):
            event_frame = int(event['frame'])
            if not first['index'] <= event_frame <= last['index']:
                raise ValueError(f'事件影格不在辨識範圍內：{event_frame}')
            jobs.append(dict(event=event,
                             start=max(first['index'], event_frame - math.ceil(before_seconds * fps)),
                             end=min(last['index'], event_frame + math.ceil(after_seconds * fps)),
                             writer=None, frames=0))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        # Separate runs keep old clips safe and avoid stale status files.
        run_dir = Path(tempfile.mkdtemp(prefix='run_', dir=output_dir))
        for index, job in enumerate(jobs, 1):
            event = job['event']
            # 短檔名：event_id 含來源檔名與物件編號（例如 cdy01_double_yellow_line_crossing_vehicle-000017_double-line-1_0_159），
            # 輸出放在較深的資料夾時會超過 Windows 260 字元路徑上限（4bu 原始片段、4bw 標註片段）。
            # 完整事件資訊（event_id、車輛、時間）在同名 .json／.txt 與 index.json。
            rule = re.sub(r'[^A-Za-z0-9_-]', '_', str(event.get('rule_id') or 'event'))
            job['path'] = run_dir / (f'{index:03d}_{event["status"]}.mp4' if raw else
                                     f'{index:03d}_{event["status"]}_{rule}.mp4')
        observations = _observations(observations_path)
        resources.callback(observations.close)
        current = next(observations, None)
        start, end = min(j['start'] for j in jobs), max(j['end'] for j in jobs)
        if start and not cap.set(cv2.CAP_PROP_POS_FRAMES, start):
            raise RuntimeError('無法定位違規片段起始影格')
        index = start - 1
        while index < end:
            index += 1
            if raw:
                upcoming = min((j['start'] for j in jobs if j['end'] >= index), default=end + 1)
                if upcoming > end:
                    break
                if upcoming - index > RAW_JUMP_SECONDS * fps:
                    if not cap.set(cv2.CAP_PROP_POS_FRAMES, upcoming):
                        raise RuntimeError('無法定位違規片段起始影格')
                    index = upcoming
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f'原影片在影格 {index} 提前結束，無法完成違規片段')
            while current and current['frame']['index'] < index:
                current = next(observations, None)
            obs = current['observations'] if current and current['frame']['index'] == index else {}
            vehicles = obs.get('rule_vehicles', obs.get('vehicles', []))
            for job in jobs:
                if not job['start'] <= index <= job['end']:
                    continue
                if job['writer'] is None:
                    writer = (_RawWriter(job['path'], fps, size) if raw else
                              cv2.VideoWriter(str(job['path']), cv2.VideoWriter_fourcc(*'mp4v'), fps, size))
                    resources.callback(writer.release)
                    if not writer.isOpened():
                        raise RuntimeError(f'無法建立違規片段：{job["path"]}')
                    job['writer'] = writer
                    job['codec'] = writer.codec if raw else 'mp4v'
                if raw:
                    job['writer'].write(frame)
                else:
                    annotated = draw_violation_candidates(frame.copy(), vehicles, [job['event']], index / fps,
                                                           hold_seconds=None, frame_index=index,
                                                           area_vehicles=obs.get('area_vehicles'))
                    job['writer'].write(annotated)
                job['frames'] += 1
                if index == job['end']:
                    job['writer'].release()
                    print(f'違規片段：{job["path"]}', flush=True)
        clips = []
        for job in jobs:
            metadata = dict(event=job['event'], source_video=str(Path(video_path).resolve()),
                            clip_path=str(job['path'].resolve()), start_frame=job['start'],
                            end_frame=job['end'], start_sec=job['start'] / fps,
                            end_sec=(job['end'] + 1) / fps, frame_count=job['frames'], fps=fps,
                            event_offset_sec=(job['event']['frame'] - job['start']) / fps,
                            audio_included=False, annotated=not raw, codec=job.get('codec', 'mp4v'))
            job['path'].with_suffix('.json').write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
            job['path'].with_suffix('.txt').write_text(
                format_event_summary(job['event']), encoding='utf-8-sig')
            clips.append(metadata)
        (run_dir / 'index.json').write_text(json.dumps(clips, ensure_ascii=False, indent=2), encoding='utf-8')
        return clips


def main():
    parser = argparse.ArgumentParser(description='從既有違規事件與辨識資料剪輯標記片段')
    parser.add_argument('video', type=Path)
    parser.add_argument('observations', type=Path, help='*_detections.jsonl')
    parser.add_argument('events', type=Path, help='*_violations.jsonl')
    parser.add_argument('--output-dir', type=Path)
    add_clip_arguments(parser)
    args = parser.parse_args()
    try:
        validate_clip_options(args.clip_before, args.clip_after)
    except ValueError as exc:
        parser.error(str(exc))
    if args.violation_clips:
        clips = export_violation_clips(args.video, args.observations, list(_observations(args.events)),
                                      args.output_dir or args.observations.parent / f'{args.video.stem}_violation_clips',
                                      args.clip_before, args.clip_after, raw=args.clip_raw)
        print(f'已輸出 {len(clips)} 個違規片段')


if __name__ == '__main__':
    main()
