"""Shared strict loader, unchanged preprocessing, temporal logic and gate.

Display colors/rendering are deliberately outside this module and its gate.
No training APIs or diagnostic-tool imports are used.
"""
import os
os.environ['MPLBACKEND']='Agg'
import copy,hashlib,json,math,sys
from collections import deque
from pathlib import Path
import numpy as np
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
SOURCE_SHA='75ccd13c8efe31a0c1a76f22ddcd3867ebdd606287af3c39ae43b6c589d96b36'
VAL_SHA='618aeb35daaf724a919e3ff9e446cc1482d94fb9ffa21675d7e28e14a82ca2de'
REFERENCES={'512/341':58.66,'1024/683':59.94}
TOLERANCE=.1


def path(value):
    p=Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (ROOT/p).resolve()


def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()


def signature(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode('utf-8')).hexdigest()


def save_json(p,value):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    temporary=p.with_suffix(p.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(p)


def load_settings(filename):
    settings=yaml.safe_load(path(filename).read_text(encoding='utf-8-sig'))
    if settings['checkpoint']['sha256']!=SOURCE_SHA:raise ValueError('This release authorizes only audited best56000; stage2 checkpoints are not yet supported')
    s=settings['inference'];crop=s['crop'];stride=s['stride']
    if len(crop)!=2 or len(stride)!=2 or any(type(x)!=int or x<=0 for x in crop+stride) or any(x>y for x,y in zip(stride,crop)):
        raise ValueError('Expected positive H/W crop and stride <= crop')
    if s['precision']!='float32' or s['probability_storage']!='cuda_float16':raise ValueError('FP32 network and CUDA fp16 probabilities are mandatory')
    if not 0<=s['alpha']<1 or not math.isfinite(s['alpha']):raise ValueError('Invalid EMA alpha')
    if type(s['vote_window'])!=int or s['vote_window']<1 or not 0<s['presence_fraction']<=1:raise ValueError('Invalid event window/area threshold')
    return settings


def verification_settings(settings):
    # Display-only changes never affect model verification/run signatures.
    return dict(checkpoint_sha256=settings['checkpoint']['sha256'],inference=settings['inference'],device=settings['device'])


def imread_bgr(filename):
    import cv2
    data=np.fromfile(filename,dtype=np.uint8);image=cv2.imdecode(data,cv2.IMREAD_COLOR)
    if image is None:raise ValueError('Cannot decode image: '+str(filename))
    return image


class RoadMarkingModel:
    def __init__(self,settings,load=True):
        import torch,mmcv,mmengine,mmseg,cv2,PIL
        from mmengine.config import Config
        from mmengine.utils import import_modules_from_strings
        from mmseg.utils import register_all_modules
        from mmseg.utils.tokenizer import SimpleTokenizer,default_bpe
        import mmseg.visualization
        self.settings=settings;self.device=torch.device(settings['device'])
        if self.device.type!='cuda' or not torch.cuda.is_available():raise RuntimeError('Requested CUDA is required; no CPU/precision fallback')
        checkpoint=path(settings['checkpoint']['path'])
        if digest(checkpoint)!=SOURCE_SHA:raise ValueError('Checkpoint SHA256 differs from authorized best56000')
        cfg=Config.fromfile(str(path(settings['model_config'])),lazy_import=False,import_custom_modules=False)
        if cfg.get('custom_imports'):import_modules_from_strings(**cfg.custom_imports)
        SimpleTokenizer();register_all_modules()
        dataset=cfg.val_dataloader.dataset
        if 'dataset' in dataset or 'datasets' in dataset:raise ValueError('Wrapped val dataset is not supported')
        self.names=list(dataset.metainfo.classes);self.meta_palette=copy.deepcopy(dataset.metainfo.palette)
        if len(self.names)!=25 or dataset.get('reduce_zero_label',False) or dataset.get('ignore_index',255)!=255:
            raise ValueError('Expected unchanged IDs 0..24 and ignore=255')
        self.pipeline=copy.deepcopy(dataset.pipeline);self.dataset_cfg=copy.deepcopy(dataset)
        if signature(cfg.test_pipeline)!=signature(self.pipeline):raise ValueError('Original test/val pipelines differ; do not guess which to use')
        implementation=['tools/road_marking_core.py','tools/verify_infer_pipeline.py',
            'mmseg/apis/utils.py','mmseg/models/segmentors/encoder_decoder.py','mmseg/models/segmentors/base.py',
            'mmseg/models/backbones/mit.py','mmseg/models/decode_heads/segformer_head.py',
            'mmseg/models/data_preprocessor.py','mmseg/datasets/transforms/loading.py','mmseg/datasets/transforms/transforms.py']
        self.identity=dict(profile=verification_settings(settings),checkpoint_sha256=SOURCE_SHA,
            config_expanded_sha256=signature(cfg.pretty_text),pipeline=self.pipeline,classes=self.names,
            implementations={p:digest(ROOT/p) for p in implementation},
            versions=dict(torch=torch.__version__,cuda=torch.version.cuda,mmcv=mmcv.__version__,
                          mmengine=mmengine.__version__,mmseg=mmseg.__version__,gpu=torch.cuda.get_device_name(self.device),
                          opencv=cv2.__version__,numpy=np.__version__,pillow=PIL.__version__,
                          cudnn_benchmark=torch.backends.cudnn.benchmark,cudnn_deterministic=torch.backends.cudnn.deterministic,
                          matmul_tf32=torch.backends.cuda.matmul.allow_tf32,cudnn_tf32=torch.backends.cudnn.allow_tf32),
            tokenizer_sha256=digest(Path(default_bpe())))
        self.fingerprint=signature(self.identity);self.cfg=cfg;self.model=None
        if load:self.load(checkpoint)

    def load(self,checkpoint=None):
        import torch
        from mmengine.config import Config
        from mmengine.model import revert_sync_batchnorm
        from mmseg.registry import MODELS
        from mmcv.transforms import Compose
        checkpoint=checkpoint or path(self.settings['checkpoint']['path'])
        if digest(checkpoint)!=SOURCE_SHA:raise ValueError('Checkpoint changed before strict load')
        state=torch.load(str(checkpoint),map_location='cpu')
        meta=state.get('meta',{});text=meta.get('cfg') or meta.get('config')
        if not text:raise ValueError('Need original checkpoint config metadata')
        historical=Config.fromstring(text,'.py')
        if signature(historical.val_dataloader.dataset.pipeline)!=signature(self.pipeline):raise ValueError('Pipeline differs from original checkpoint')
        classes=meta.get('dataset_meta',{}).get('classes')
        if classes is not None and list(classes)!=self.names:raise ValueError('Checkpoint class IDs differ')
        cfg=copy.deepcopy(self.cfg);cfg.model.backbone.init_cfg=None;cfg.model.pretrained=None
        model=MODELS.build(cfg.model)
        model.load_state_dict(state['state_dict'],strict=True);del state
        model=revert_sync_batchnorm(model).float().to(self.device).eval()
        model.cfg=cfg;model.dataset_meta=dict(self.dataset_cfg.metainfo)
        self.model=model
        pipeline=copy.deepcopy(self.pipeline)
        if pipeline[0]['type']!='LoadImageFromFile':raise ValueError('Expected original file loader')
        pipeline[0]['type']='LoadImageFromNDArray'
        pipeline=[step for step in pipeline if step['type']!='LoadAnnotations']
        self.compose=Compose(pipeline)
        self.set_slide(self.settings['inference']['crop'],self.settings['inference']['stride'])

    def set_slide(self,crop,stride):
        self.model.test_cfg.update(mode='slide',crop_size=tuple(crop),stride=tuple(stride),blend='uniform',whole_weight=0.)

    def predict(self,frame):
        import torch
        from mmengine.dataset import pseudo_collate
        if frame.ndim!=3 or frame.shape[2]!=3:raise ValueError('Expected BGR image')
        with torch.inference_mode(),torch.autocast(device_type='cuda',enabled=False):
            result=self.model.test_step(pseudo_collate([self.compose(dict(img=frame))]))[0]
            logits=result.seg_logits.data
        if logits.dtype!=torch.float32 or logits.shape!=(25,*frame.shape[:2]) or not torch.isfinite(logits).all():
            raise ValueError('Expected finite, original-size, 25-class FP32 final logits')
        return logits


def require_verification(settings,model):
    file=path(settings['verification']['report'])
    if not file.is_file():raise RuntimeError('Run verify_infer_pipeline.py first; no unverified inference is allowed')
    report=json.loads(file.read_text(encoding='utf-8'));saved_hash=report.pop('payload_sha256',None)
    if signature(report)!=saved_hash:raise ValueError('Verification report checksum changed')
    if report.get('status')!='passed' or report.get('fingerprint')!=model.fingerprint or report.get('val_list_sha256')!=VAL_SHA:
        raise ValueError('Verification failed/stale/different model, settings, code or runtime; re-run verifier')
    if set(report['variants'])!=set(REFERENCES):raise ValueError('Both full-val variants are mandatory')
    for tag,expected in REFERENCES.items():
        value=report['variants'][tag]
        if value['images']!=374 or abs(value['mIoU']-expected)>TOLERANCE+1e-9:raise ValueError('Full-val gate did not pass')
    return report,saved_hash


class MajorityVote:
    def __init__(self,window,classes=25):
        self.window=window;self.queue=deque();self.positive=np.zeros(classes,dtype=np.int64)
    def step(self,present):
        present=np.asarray(present,dtype=bool);self.queue.append(present.copy());self.positive+=present
        if len(self.queue)>self.window:self.positive-=self.queue.popleft()
        return None if len(self.queue)<self.window else self.positive>self.window/2


class ProbabilityEMA:
    def __init__(self,alpha):self.alpha=alpha;self.probabilities=None
    def step(self,logits):
        import torch
        if logits.dtype!=torch.float32 or logits.device.type!='cuda':raise ValueError('EMA requires FP32 network logits on CUDA')
        probabilities=logits.float().softmax(dim=0).to(torch.float16)
        if self.probabilities is None:self.probabilities=probabilities.clone()
        else:self.probabilities.mul_(self.alpha).add_(probabilities,alpha=1-self.alpha)
        if self.probabilities.dtype!=torch.float16 or self.probabilities.device.type!='cuda':raise ValueError('Probability storage changed')
        return self.probabilities.argmax(0).to(torch.uint8).cpu().numpy()


class EventTracker:
    def __init__(self,names,fps):self.names=names;self.fps=fps;self.active={};self.rows=[]
    def step(self,frame,states,source):
        if states is None:return
        for c,present in enumerate(states):
            key=(source,c)
            if present and key not in self.active:self.active[key]=frame
            elif not present and key in self.active:self.close(key,frame-1,False)
    def close(self,key,end,eof):
        start=self.active.pop(key);source,c=key
        self.rows.append(dict(presence_source=source,class_id=c,class_name=self.names[c],start_frame=start,end_frame=end,
            start_time_seconds=start/self.fps,end_time_seconds=end/self.fps,
            end_time_exclusive_seconds=(end+1)/self.fps,duration_seconds=(end-start+1)/self.fps,closed_at_eof=int(eof)))
    def finish(self,last):
        for key in list(self.active):self.close(key,last,True)
        return sorted(self.rows,key=lambda r:(r['presence_source'],r['class_id'],r['start_frame']))
