import json, hashlib, subprocess, shutil
from pathlib import Path
from fractions import Fraction
import numpy as np

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def probe(path,cfg):
 cmd=[cfg['ffprobe'],'-v','error','-select_streams','v:0','-show_streams','-show_frames','-show_entries','stream=width,height,codec_name,pix_fmt,r_frame_rate,avg_frame_rate,duration,nb_frames:frame=best_effort_timestamp_time,pkt_duration_time','-of','json',str(path)]
 data=json.loads(subprocess.check_output(cmd));s=data['streams'][0];ts=[float(x['best_effort_timestamp_time']) for x in data['frames']];assert all(b>a for a,b in zip(ts,ts[1:])), 'Non-increasing PTS unsupported'
 origin=ts[0];ts=[t-origin for t in ts];fps=float(Fraction(s['r_frame_rate']));dt=float(data['frames'][-1].get('pkt_duration_time') or 1/fps)
 return dict(**s,frame_count=len(ts),fps=fps,timestamps=ts,duration_seconds=ts[-1]+dt,pts_origin=origin,source=str(Path(path).resolve()),source_sha256=sha(path))

def frames(path,cfg,meta):
 # RGB24 is exactly the pixel format used for prior direct ffmpeg PNG extraction.
 cmd=[cfg['ffmpeg'],'-v','error','-i',str(path),'-map','0:v:0','-fps_mode','passthrough','-f','rawvideo','-pix_fmt','rgb24','pipe:1']
 p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL);n=int(meta['width'])*int(meta['height'])*3;i=0;complete=False
 try:
  while True:
   chunks=[];remaining=n
   while remaining:
    b=p.stdout.read(remaining)
    if not b:break
    chunks.append(b);remaining-=len(b)
   if not chunks:break
   if remaining:raise RuntimeError('Truncated ffmpeg frame')
   rgb=np.frombuffer(b''.join(chunks),np.uint8).reshape(int(meta['height']),int(meta['width']),3)
   yield i,rgb[:,:,::-1].copy();i+=1
  if p.wait()!=0:raise RuntimeError('ffmpeg decoding failed')
  assert i==meta['frame_count'],(i,meta['frame_count']);complete=True
 finally:
  p.stdout.close()
  if p.poll() is None:p.terminate();p.wait()
 def_unused=None


def assignment(reward):
 # Rectangular Hungarian assignment; no additional package dependency.
 trans=reward.shape[0]>reward.shape[1];cost=(-reward.T if trans else -reward).tolist();n=len(cost);m=len(cost[0]);u=[0.]*(n+1);v=[0.]*(m+1);p=[0]*(m+1);way=[0]*(m+1)
 for i in range(1,n+1):
  p[0]=i;j0=0;minv=[float('inf')]*(m+1);used=[False]*(m+1)
  while True:
   used[j0]=True;i0=p[j0];delta=float('inf');j1=0
   for j in range(1,m+1):
    if used[j]:continue
    cur=cost[i0-1][j-1]-u[i0]-v[j]
    if cur<minv[j]:minv[j]=cur;way[j]=j0
    if minv[j]<delta:delta=minv[j];j1=j
   for j in range(m+1):
    if used[j]:u[p[j]]+=delta;v[j]-=delta
    else:minv[j]-=delta
   j0=j1
   if p[j0]==0:break
  while True:
   j1=way[j0];p[j0]=p[j1];j0=j1
   if j0==0:break
 pairs=[(p[j]-1,j-1) for j in range(1,m+1) if p[j]]
 if trans:pairs=[(j,i) for i,j in pairs]
 return [i for i,j in pairs],[j for i,j in pairs]

