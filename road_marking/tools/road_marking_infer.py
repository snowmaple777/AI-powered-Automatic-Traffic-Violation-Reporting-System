"""Verified road-marking video/frame-folder inference; no training path."""
import argparse,csv,json,math,re,time,traceback
from pathlib import Path
import numpy as np
from road_marking_core import (RoadMarkingModel,ProbabilityEMA,MajorityVote,EventTracker,
    load_settings,verification_settings,require_verification,path,digest,signature,save_json,imread_bgr)


class FrameSource:
    def __init__(self,source,fps=None):
        import cv2
        self.path=Path(source).expanduser().resolve();self.capture=None
        if self.path.is_dir():
            def natural(p):
                tokens=tuple((0,int(x)) if x.isdigit() else (1,x.casefold()) for x in re.split(r'(\d+)',p.name))
                return tokens,p.name.casefold(),p.name
            self.files=sorted([p for p in self.path.iterdir() if p.is_file() and p.suffix.lower() in ['.jpg','.jpeg','.png','.bmp','.tif','.tiff','.webp']],key=natural)
            if not self.files:raise ValueError('No image frames in the input folder (non-recursive)')
            if fps is None:raise ValueError('Frame-folder input needs actual --fps or input.fps_override; frame rate is never guessed')
            self.fps=float(fps);self.total=len(self.files)
            self.manifest=dict(kind='frame_folder',path=str(self.path),frames=[dict(filename=p.name,sha256=digest(p)) for p in self.files])
        else:
            if not self.path.is_file():raise FileNotFoundError(self.path)
            self.capture=cv2.VideoCapture(str(self.path))
            if not self.capture.isOpened():raise ValueError('Cannot open video')
            metadata_fps=float(self.capture.get(cv2.CAP_PROP_FPS));self.fps=float(fps) if fps is not None else metadata_fps
            self.total=max(0,int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT)));self.files=None
            self.manifest=dict(kind='video',path=str(self.path),sha256=digest(self.path),metadata_fps=metadata_fps,metadata_frames=self.total)
        if not math.isfinite(self.fps) or self.fps<=0:raise ValueError('Invalid actual source FPS')
        self.manifest.update(fps=self.fps,fps_overridden=fps is not None,frame_numbering='0-based processed/source frame index; no stride or input scaling')
    def __iter__(self):
        if self.files is not None:
            for i,p in enumerate(self.files):yield i,p.name,imread_bgr(p)
        else:
            i=0
            while True:
                ok,frame=self.capture.read()
                if not ok:
                    if self.total>0 and i<self.total:raise ValueError(f'Video decode ended early: {i}/{self.total}')
                    break
                yield i,f'{self.path.name}#{i}',frame;i+=1
    def close(self):
        if self.capture is not None:self.capture.release()
    def verify_unchanged(self):
        if self.files is None:
            if digest(self.path)!=self.manifest['sha256']:raise ValueError('Source video changed while running')
        else:
            current={p.name for p in self.path.iterdir() if p.is_file() and p.suffix.lower() in ['.jpg','.jpeg','.png','.bmp','.tif','.tiff','.webp']}
            if current!={p.name for p in self.files}:raise ValueError('Frame-folder membership changed during inference')
            for file,row in zip(self.files,self.manifest['frames']):
                if digest(file)!=row['sha256']:raise ValueError('Input frame changed: '+str(file))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings',default='configs/road_marking_infer.yaml')
    p.add_argument('--input',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--overlay',action='store_true');p.add_argument('--fps',type=float)
    p.add_argument('--max-frames',type=int,help='Explicit input subset only; omitted processes the complete source')
    p.add_argument('--check-only',action='store_true',help='Still requires passed model gate; decodes first frame, no NN forward')
    a=p.parse_args();settings=load_settings(a.settings)
    if a.max_frames is not None and a.max_frames<1:p.error('--max-frames must be positive')
    if not a.check_only and a.out.exists():raise FileExistsError('Choose a new output directory; existing results are never overwritten')
    source=FrameSource(a.input,a.fps if a.fps is not None else settings.get('input',{}).get('fps_override'))
    writer=None;owned_output=False
    try:
        model=RoadMarkingModel(settings,load=False);certificate,certificate_sha=require_verification(settings,model);model.load()
        iterator=iter(source)
        if a.check_only:
            i,name,frame=next(iterator);model.compose(dict(img=frame))
            print(f'PASS verified model + strict=True + unchanged pipeline + source decode: {name}, {frame.shape[1]}x{frame.shape[0]}, {source.fps:g} fps; no forward pass')
            return
        if a.out.exists():raise FileExistsError('Choose a new output directory; existing results are never overwritten')
        a.out.mkdir(parents=True);owned_output=True
        palette=None
        if a.overlay:
            from road_marking_overlay import load_palette,H264Writer,render
            palette=load_palette(settings['overlay']['palette'])
            save_json(a.out/'overlay_display.json',dict(palette_source=str(path(settings['overlay']['palette'])),
                palette_rgb=palette.tolist(),overlay=settings['overlay'],note='Display only; no model metainfo or verification fingerprint changes'))
        identity=dict(model_fingerprint=model.fingerprint,verification_payload_sha256=certificate_sha,cli_sha256=digest(Path(__file__)),
            profile=verification_settings(settings),source=source.manifest,overlay_enabled=a.overlay,
            statistics='class counts from EMA argmax; raw_present means after EMA, before event vote; includes background ID 0',
            frame_limit=a.max_frames,events='raw and voted runs; inclusive 0-based start/end frames; end_time_exclusive also recorded')
        identity['run_fingerprint']=signature({k:v for k,v in identity.items() if k!='overlay_enabled'});save_json(a.out/'run_identity.json',identity)
        save_json(a.out/'summary.json',dict(status='running',processed_frames=0))
        ema=ProbabilityEMA(settings['inference']['alpha']);vote=MajorityVote(settings['inference']['vote_window']);events=EventTracker(model.names,source.fps)
        processed=0;shape=None;began=time.perf_counter()
        count_tmp=a.out/'pixel_counts.csv.inprogress';timeline_tmp=a.out/'presence_timeline.csv.inprogress'
        import torch
        with count_tmp.open('w',encoding='utf-8-sig',newline='',buffering=1) as cf,timeline_tmp.open('w',encoding='utf-8-sig',newline='',buffering=1) as tf:
            count_writer=csv.DictWriter(cf,fieldnames=['frame','source_name','time_seconds','total_pixels']+[f'pixels_{c}' for c in range(25)])
            timeline_writer=csv.DictWriter(tf,fieldnames=['frame','source_name','time_seconds','class_id','class_name','pixels','raw_present','voted_present','positive_frames','window_frames'])
            count_writer.writeheader();timeline_writer.writeheader()
            with torch.inference_mode():
                for i,name,frame in iterator:
                    if a.max_frames is not None and processed>=a.max_frames:break
                    if shape is None:shape=frame.shape[:2]
                    if frame.shape[:2]!=shape:raise ValueError('Resolution changed; temporal pixels are not aligned. No automatic resize/reset is performed')
                    logits=model.predict(frame);prediction=ema.step(logits);del logits
                    counts=np.bincount(prediction.ravel(),minlength=25)
                    threshold=math.ceil(prediction.size*settings['inference']['presence_fraction'])
                    present=counts>=threshold;voted=vote.step(present)
                    count_writer.writerow(dict(frame=i,source_name=name,time_seconds=i/source.fps,total_pixels=prediction.size,
                        **{f'pixels_{c}':int(counts[c]) for c in range(25)}))
                    for c in range(25):
                        timeline_writer.writerow(dict(frame=i,source_name=name,time_seconds=i/source.fps,class_id=c,class_name=model.names[c],
                            pixels=int(counts[c]),raw_present=int(present[c]),voted_present='' if voted is None else int(voted[c]),
                            positive_frames=int(vote.positive[c]),window_frames=len(vote.queue)))
                    events.step(i,present,'raw');events.step(i,voted,'voted')
                    if a.overlay:
                        output=render(frame,prediction,palette,model.names,settings,i,i/source.fps)
                        if writer is None:writer=H264Writer(a.out/'overlay.inprogress.mp4',output.shape[1],output.shape[0],source.fps,settings)
                        writer.write(output)
                    processed+=1
                    if processed==1 or processed%10==0:print(f'{processed}/{source.total or "?"} frames; {time.perf_counter()-began:.1f}s',flush=True)
        if not processed:raise ValueError('No frames were processed')
        if a.max_frames is None and source.total>0 and processed!=source.total:raise ValueError('Incomplete input processing')
        source.verify_unchanged()
        fresh=RoadMarkingModel(load_settings(a.settings),load=False)
        if fresh.fingerprint!=model.fingerprint:raise ValueError('Model/settings/implementation changed during inference')
        verified_frames=None
        if writer is not None:
            verified_frames=writer.close();writer=None
            (a.out/'overlay.inprogress.mp4').replace(a.out/'overlay.mp4')
        rows=events.finish(processed-1)
        fields=['presence_source','class_id','class_name','start_frame','end_frame','start_time_seconds','end_time_seconds',
                'end_time_exclusive_seconds','duration_seconds','closed_at_eof']
        with (a.out/'events.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
        count_tmp.replace(a.out/'pixel_counts.csv');timeline_tmp.replace(a.out/'presence_timeline.csv')
        save_json(a.out/'summary.json',dict(status='complete',processed_frames=processed,source_frames=source.total,
            full_source=a.max_frames is None or (source.total>0 and processed==source.total),fps=source.fps,duration_seconds=processed/source.fps,
            classes=25,events=len(rows),alpha=settings['inference']['alpha'],vote_window=settings['inference']['vote_window'],
            presence_fraction=settings['inference']['presence_fraction'],probability_dtype='CUDA float16',network_dtype='float32',
            vote_warmup_blank_frames=min(processed,settings['inference']['vote_window']-1),overlay_decoded_frames=verified_frames,
            elapsed_seconds=time.perf_counter()-began,run_fingerprint=identity['run_fingerprint']))
        print('COMPLETE: '+str(a.out.resolve()),flush=True)
    except Exception as exc:
        if writer is not None:writer.abort()
        if owned_output:
            save_json(a.out/'summary.json',dict(status='failed',error=str(exc),traceback=traceback.format_exc()))
        raise
    finally:source.close()


if __name__=='__main__':main()
