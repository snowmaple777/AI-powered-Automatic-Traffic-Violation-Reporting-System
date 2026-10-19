"""Single-scale preview, position-only tracking; not a replacement for frozen evaluation."""
from pathlib import Path
import argparse,json,os,sys,time,subprocess
import numpy as np,cv2
ROOT=Path(__file__).resolve().parent
from track_utils import probe,frames,assignment
import shutil
SETTINGS=dict(imgsz=1280,detect_conf=.05,start_conf=.15,iou=.7,max_det=300,hold_seconds=.2,smooth_alpha=.6,min_iou=.1,max_center_distance=.6,max_size_ratio=2.5,spatial_dedup_iou=.7)
COLORS={'red':(0,0,255),'green':(0,220,0),'yellow':(0,230,255),'unknown':(180,180,180),'arrow':(255,180,80)}
def iou(a,b):
 inter=np.maximum(np.minimum(a[2:],b[2:])-np.maximum(a[:2],b[:2]),0).prod();return float(inter/max(np.prod(a[2:]-a[:2])+np.prod(b[2:]-b[:2])-inter,1e-9))
class PositionTracker:
 def __init__(self,cfg=SETTINGS):self.cfg=cfg;self.tracks=[];self.next_id=1
 def update(self,ts,dets):
  cfg=self.cfg;ds=[]
  for d in sorted(dets,key=lambda d:-d['conf']):
   if d['class_name'] not in ['red','green','yellow'] or d['conf']<cfg['detect_conf']:continue
   if not any(iou(np.array(d['box']),np.array(x['box']))>=cfg['spatial_dedup_iou'] for x in ds):ds.append(d)
  self.tracks=[t for t in self.tracks if ts-t['last_time']<=cfg['hold_seconds']+1e-8]
  cost=np.full((len(self.tracks),len(ds)),1e6)
  for i,t in enumerate(self.tracks):
   a=np.array(t['last_raw']);sa=np.maximum(a[2:]-a[:2],1)
   for j,d in enumerate(ds):
    b=np.array(d['box']);sb=np.maximum(b[2:]-b[:2],1);dist=np.linalg.norm((a[:2]+a[2:]-b[:2]-b[2:])/2)/max(np.linalg.norm((sa+sb)/2),1);ov=iou(a,b)
    if max(np.max(sa/sb),np.max(sb/sa))<=cfg['max_size_ratio'] and (ov>=cfg['min_iou'] or dist<=cfg['max_center_distance']):cost[i,j]=1-(.7*ov+.3*max(0,1-dist/cfg['max_center_distance']))
  used=set();observed={}
  if cost.size:
   ii,jj=assignment(1e6-cost)
   for i,j in zip(ii,jj):
    if cost[i,j]>=1e6:continue
    t=self.tracks[i];d=ds[j];a=cfg['smooth_alpha'];t['box']=a*np.array(d['box'])+(1-a)*t['box'];t['last_raw']=d['box'];t['last_time']=ts;used.add(j);observed[t['id']]=d
  for j,d in enumerate(ds):
   if j in used or d['conf']<cfg['start_conf']:continue
   t=dict(id=self.next_id,box=np.array(d['box'],dtype=float),last_raw=d['box'],last_time=ts);self.next_id+=1;self.tracks.append(t);observed[t['id']]=d
  result=[]
  for t in self.tracks:
   d=observed.get(t['id']);result.append(dict(track_id=t['id'],box=t['box'].tolist(),observed=d is not None,state=d['class_name'] if d else 'unknown',raw_conf=d['conf'] if d else None,raw_box=d['box'] if d else None,gap_seconds=ts-t['last_time']))
  return result

def draw(im,box,label,color,dashed=False):
 h,w=im.shape[:2];x1,y1,x2,y2=np.round(box).astype(int);x1,x2=np.clip([x1,x2],0,w-1);y1,y2=np.clip([y1,y2],0,h-1)
 if dashed:
  for x in range(x1,x2,12):
   cv2.line(im,(x,y1),(min(x+6,x2),y1),color,2);cv2.line(im,(x,y2),(min(x+6,x2),y2),color,2)
  for y in range(y1,y2,12):
   cv2.line(im,(x1,y),(x1,min(y+6,y2)),color,2);cv2.line(im,(x2,y),(x2,min(y+6,y2)),color,2)
 else:cv2.rectangle(im,(x1,y1),(x2,y2),color,2)
 cv2.putText(im,label,(int(x1),max(16,int(y1)-6)),cv2.FONT_HERSHEY_SIMPLEX,.48,color,1,cv2.LINE_AA)

def main():
 p=argparse.ArgumentParser();p.add_argument('--model',type=Path,default=ROOT/'models/traffic_light_best.pt');p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--device',default='cpu');p.add_argument('--max-frames',type=int);p.add_argument('--dry-run',action='store_true');p.add_argument('--ffmpeg',default='ffmpeg');p.add_argument('--ffprobe',default='ffprobe');a=p.parse_args()
 assert a.model.is_file() and a.source.is_file();cfg={'ffmpeg':shutil.which(a.ffmpeg),'ffprobe':shutil.which(a.ffprobe)}
 if not all(cfg.values()):p.error('ffmpeg/ffprobe missing: install FFmpeg or pass --ffmpeg and --ffprobe executable paths')
 if a.max_frames is not None and a.max_frames<=0:p.error('--max-frames must be positive')
 meta=probe(a.source,cfg)
 print(json.dumps(dict(frames=meta['frame_count'],fps=meta['fps'],settings=SETTINGS,device=a.device)),flush=True)
 if a.dry_run:return
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);(out/'runtime').mkdir();os.environ['YOLO_CONFIG_DIR']=str(out/'runtime')
 from ultralytics import YOLO
 from ultralytics.utils import LOGGER
 import logging,hashlib
 LOGGER.setLevel(logging.ERROR);model=YOLO(str(a.model));tracker=PositionTracker();w,h=int(meta['width']),int(meta['height']);fps=meta['frame_count']/meta['duration_seconds']
 cmd=[cfg['ffmpeg'],'-v','error','-n','-f','rawvideo','-pix_fmt','bgr24','-s',f'{w*2}x{h}','-r',str(fps),'-i','pipe:0','-an','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p',str(out/'comparison.mp4')]
 start=time.monotonic();count=0;gaps=0
 with (out/'encode.log').open('wb') as err,(out/'frames.jsonl').open('w',encoding='utf-8') as log:
  proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stderr=err)
  try:
   for idx,im in frames(a.source,cfg,meta):
    if a.max_frames is not None and idx>=a.max_frames:break
    r=model.predict(im,imgsz=SETTINGS['imgsz'],conf=SETTINGS['detect_conf'],iou=SETTINGS['iou'],max_det=SETTINGS['max_det'],nms=True,agnostic_nms=False,device=a.device,half=False,save=False,verbose=False)[0]
    ds=[dict(box=list(map(float,x[:4])),conf=float(x[4]),class_name=model.names[int(x[5])],class_id=int(x[5])) for x in r.boxes.data.cpu().numpy()]
    ts=meta['timestamps'][idx];tracked=tracker.update(ts,ds);left=im.copy();right=im.copy()
    for d in ds:
     if d['conf']>=SETTINGS['start_conf']:draw(left,d['box'],f"{d['class_name']} {d['conf']:.2f}",COLORS.get(d['class_name'],(255,255,255)))
    for t in tracked:
     text=f"ID{t['track_id']} {t['state']}"+(f" {t['raw_conf']:.2f}" if t['observed'] else ' GAP')
     draw(right,t['box'],text,COLORS[t['state']],not t['observed']);gaps+=int(not t['observed'])
    for image,title in [(left,'RAW conf >= 0.15'),(right,'POSITION TRACK | low=0.05 high=0.15 | gap=unknown')]:cv2.putText(image,title,(15,30),cv2.FONT_HERSHEY_SIMPLEX,.65,(255,255,255),2)
    proc.stdin.write(np.concatenate([left,right],axis=1).tobytes());log.write(json.dumps(dict(frame=idx,time_s=ts,detections=ds,tracks=tracked))+'\n');log.flush();count+=1
    if count%25==0:print('frames',count,'seconds',round(time.monotonic()-start,1),flush=True)
   proc.stdin.close();assert proc.wait()==0,'ffmpeg failed; inspect encode.log'
  finally:
   if not proc.stdin.closed:proc.stdin.close()
   if proc.poll() is None:proc.terminate();proc.wait()
 summary=dict(settings=SETTINGS,model=str(a.model),model_sha256=hashlib.sha256(a.model.read_bytes()).hexdigest(),source=str(a.source),frames=count,tracks_created=tracker.next_id-1,gap_track_frames=gaps,elapsed_s=time.monotonic()-start,device=a.device,output_fps=fps,source_meta=meta,notes='Single scale original image; no area_downup, no color vote. Position-only association and coordinate EMA. Gap state always unknown, raw_conf null. No score boosting. Visual stability only, not accuracy or ID-switch validation. CFR render from average source fps; JSONL retains original timestamps. Audio omitted.')
 (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print('DONE',str(out),flush=True)
if __name__=='__main__':main()
