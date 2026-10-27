"""Display-only palette, adaptive opacity, connected-region labels and H.264."""
import math,os,shutil,subprocess
from pathlib import Path
import numpy as np
import yaml
from PIL import Image,ImageDraw,ImageFont
from road_marking_core import path


def load_palette(filename):
    entries=yaml.safe_load(path(filename).read_text(encoding='utf-8-sig'))
    if set(entries)!=set(range(1,25)):raise ValueError('Overlay palette must define IDs 1..24 only; 0 stays transparent')
    palette=np.zeros((25,3),dtype=np.uint8)
    for c,rgb in entries.items():
        if type(c)!=int or not isinstance(rgb,list) or len(rgb)!=3 or any(type(v)!=int or not 0<=v<=255 for v in rgb):
            raise ValueError('Palette values must be RGB integer triples')
        palette[c]=rgb
    return palette


def get_font(size):
    for filename in ['C:/Windows/Fonts/arial.ttf','DejaVuSans.ttf']:
        try:return ImageFont.truetype(filename,size)
        except OSError:pass
    return ImageFont.load_default()


def color_card(palette,names,target):
    canvas=Image.new('RGB',(1440,780),'white');draw=ImageDraw.Draw(canvas)
    draw.text((15,10),'DISPLAY PALETTE ONLY | RGB | class 0 transparent',fill='black',font=get_font(21))
    for c in range(1,25):
        x=((c-1)%4)*360+12;y=((c-1)//4)*120+55;rgb=tuple(int(v) for v in palette[c])
        draw.rectangle((x,y,x+70,y+70),fill=rgb,outline=(80,80,80),width=1)
        label=f'{c}';color='black' if sum(rgb)>400 else 'white'
        draw.text((x+24,y+23),label,fill=color,font=get_font(23))
        draw.text((x+82,y+3),f'{c}: {names[c]}',fill='black',font=get_font(14))
        draw.text((x+82,y+28),'#'+''.join(f'{v:02X}' for v in rgb),fill='black',font=get_font(20))
        draw.text((x+82,y+55),str(rgb),fill='black',font=get_font(15))
    Path(target).parent.mkdir(parents=True,exist_ok=True);canvas.save(target)


def render(frame,pred,palette,names,settings,frame_index,seconds):
    import cv2
    s=settings['overlay'];h,w=pred.shape
    if frame.shape[:2]!=(h,w):raise ValueError('Overlay shape mismatch')
    if not 0<=s['thin_alpha']<=1 or not 0<=s['large_alpha']<=1 or s['thin_width_px']<=0:raise ValueError('Invalid display opacity/width')
    painted=frame.copy();hist=np.bincount(pred.ravel(),minlength=25)
    visible=[c for c in range(1,25) if hist[c]>s['legend_fraction']*pred.size]
    centers={}
    for c in range(1,25):
        if not hist[c]:continue
        binary=(pred==c).astype(np.uint8)
        n,components,stats,centroids=cv2.connectedComponentsWithStats(binary,8)
        largest=1+int(np.argmax(stats[1:,cv2.CC_STAT_AREA]))
        color=np.asarray(palette[c][::-1],dtype=np.float32)
        for label in range(1,n):
            x,y,ww,hh,area=map(int,stats[label]);roi=components[y:y+hh,x:x+ww]==label
            # A zero border makes the distance to the component boundary exact,
            # including full-width regions touching an image edge.
            distance=cv2.distanceTransform(np.pad(roi.astype(np.uint8),1),cv2.DIST_L2,5)[1:-1,1:-1]
            effective_width=2*float(distance.max())
            alpha=s['thin_alpha'] if effective_width<s['thin_width_px'] else s['large_alpha']
            patch=painted[y:y+hh,x:x+ww];patch[roi]=np.rint(patch[roi]*(1-alpha)+color*alpha).astype(np.uint8)
            if effective_width>=s['thin_width_px']:
                thickness=int(s['contour_width_px'])
                if thickness<0:raise ValueError('Negative contour width')
                if thickness:
                    eroded=cv2.erode(np.pad(roi.astype(np.uint8),thickness),np.ones((2*thickness+1,2*thickness+1),dtype=np.uint8))[thickness:-thickness,thickness:-thickness]
                    # Draw inside the class only: class 0 never receives fill.
                    boundary=roi & ~eroded.astype(bool);patch[boundary]=color.astype(np.uint8)
            if label==largest and c in visible:
                cx,cy=map(lambda v:int(round(v)),centroids[label]);cx=min(w-1,max(0,cx));cy=min(h-1,max(0,cy))
                if components[cy,cx]!=label:
                    yy,xx=np.unravel_index(int(distance.argmax()),distance.shape);cx=x+int(xx);cy=y+int(yy)
                centers[c]=(cx,cy)
    for c,(x,y) in centers.items():
        location=(min(max(2,x-6),max(2,w-22)),min(max(15,y+5),h-3))
        cv2.putText(painted,str(c),location,cv2.FONT_HERSHEY_SIMPLEX,.45,(0,0,0),3,cv2.LINE_AA)
        cv2.putText(painted,str(c),location,cv2.FONT_HERSHEY_SIMPLEX,.45,(255,255,255),1,cv2.LINE_AA)
    # Reserved legend panel never obscures road pixels and has fixed dimensions.
    panel_w=int(s['legend_panel_width']);height=max(h,650)
    if panel_w<280:raise ValueError('Legend panel must be at least 280 px')
    output=np.zeros((height,w+panel_w,3),dtype=np.uint8);output[:h,:w]=painted
    cv2.putText(output,f'frame {frame_index} | {seconds:.3f}s',(w+10,25),cv2.FONT_HERSHEY_SIMPLEX,.48,(255,255,255),1,cv2.LINE_AA)
    cv2.putText(output,f'ID / class present (>{100*s["legend_fraction"]:g}%)',(w+10,49),cv2.FONT_HERSHEY_SIMPLEX,.45,(255,255,255),1,cv2.LINE_AA)
    for i,c in enumerate(visible):
        y=68+i*24;color=tuple(int(v) for v in palette[c][::-1])
        cv2.rectangle(output,(w+10,y),(w+25,y+14),color,-1)
        cv2.putText(output,f'{c:02d} {names[c]}',(w+34,y+13),cv2.FONT_HERSHEY_SIMPLEX,.43,(255,255,255),1,cv2.LINE_AA)
    return output


class H264Writer:
    def __init__(self,target,width,height,fps,settings):
        s=settings['overlay'];exe=s['ffmpeg']
        exe=str(path(exe)) if '/' in exe or '\\' in exe else shutil.which(exe)
        if not exe or not Path(exe).is_file():raise FileNotFoundError('Set overlay.ffmpeg to an existing FFmpeg executable')
        self.target=Path(target);self.width=width;self.height=height;self.frames=0
        self.log=self.target.with_suffix('.ffmpeg.log').open('w',encoding='utf-8')
        args=[exe,'-hide_banner','-nostdin','-f','rawvideo','-pix_fmt','bgr24','-s',f'{width}x{height}',
            '-r',repr(float(fps)),'-i','pipe:0','-an','-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2',
            '-c:v','libx264','-preset',str(s['preset']),'-crf',str(s['crf']),'-pix_fmt','yuv420p','-movflags','+faststart',str(self.target)]
        self.process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    def write(self,frame):
        if frame.shape!=(self.height,self.width,3):raise ValueError('Changing overlay dimensions')
        self.process.stdin.write(np.ascontiguousarray(frame).tobytes());self.frames+=1
    def close(self):
        self.process.stdin.close();code=self.process.wait();self.log.close()
        if code:raise RuntimeError(f'FFmpeg exited {code}; see {self.target.with_suffix(".ffmpeg.log")}')
        import cv2
        cap=cv2.VideoCapture(str(self.target));count=0
        if not cap.isOpened():raise ValueError('Encoded video cannot open')
        while True:
            ok,frame=cap.read()
            if not ok:break
            if frame.shape[:2]!=((self.height+1)//2*2,(self.width+1)//2*2):raise ValueError('Encoded dimensions changed')
            count+=1
        cap.release()
        if count!=self.frames:raise ValueError('Encoded video is incomplete')
        return count
    def abort(self):
        if self.process.poll() is None:
            self.process.kill();self.process.wait()
        if not self.process.stdin.closed:
            try:self.process.stdin.close()
            except (BrokenPipeError,OSError):pass
        if not self.log.closed:self.log.close()
