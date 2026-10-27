"""Full 374-image gate through the EXACT video frame loader/model path."""
import argparse,csv,json,time
from pathlib import Path
import numpy as np
from PIL import Image
from road_marking_core import (ROOT,VAL_SHA,REFERENCES,TOLERANCE,RoadMarkingModel,load_settings,
    path,digest,signature,save_json,imread_bgr)
from road_marking_overlay import load_palette,color_card


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings',default='configs/road_marking_infer.yaml')
    p.add_argument('--color-card-only',action='store_true');a=p.parse_args()
    settings=load_settings(a.settings)
    if a.color_card_only:
        from mmengine.config import Config
        cfg=Config.fromfile(str(path(settings['model_config'])),lazy_import=False,import_custom_modules=False)
        names=list(cfg.val_dataloader.dataset.metainfo.classes)
        color_card(load_palette(settings['overlay']['palette']),names,ROOT/'road_marking_inference/overlay_palette_card.png')
        print('Generated display-only 24-color card; no verification certificate/inference/training.')
        return
    model=RoadMarkingModel(settings)
    output=path(settings['verification']['report']);output.parent.mkdir(parents=True,exist_ok=True)
    color_card(load_palette(settings['overlay']['palette']),model.names,ROOT/'road_marking_inference/overlay_palette_card.png')
    data=path(settings['verification']['data_root']);val=path(settings['verification']['val_list'])
    if digest(val)!=VAL_SHA:raise ValueError('Fixed val list SHA256 changed')
    stems=val.read_text(encoding='utf-8').splitlines()
    if len(stems)!=374 or len(set(stems))!=374:raise ValueError('Need the fixed 374 unique val filenames')
    rows=[];ds=model.dataset_cfg
    for stem in stems:
        if Path(stem).is_absolute() or '..' in Path(stem).parts:raise ValueError('Unsafe val stem')
        image=data/ds.data_prefix.img_path/(stem+ds.get('img_suffix','.jpg'))
        mask=data/ds.data_prefix.seg_map_path/(stem+ds.get('seg_map_suffix','.png'))
        rows.append(dict(stem=stem,image=str(image),mask=str(mask),image_sha256=digest(image),mask_sha256=digest(mask)))
    dataset_hash=signature([{k:r[k] for k in ['stem','image_sha256','mask_sha256']} for r in rows])
    save_json(output.parent/'validation_inputs.json',dict(val_list_sha256=VAL_SHA,dataset_sha256=dataset_hash,samples=rows))
    pending=dict(status='incomplete',fingerprint=model.fingerprint,val_list_sha256=VAL_SHA,variants={})
    pending['payload_sha256']=signature(pending);save_json(output,pending)
    all_metrics=[];variants={};started=time.perf_counter()
    for crop,stride in [(512,341),(1024,683)]:
        tag=f'{crop}/{stride}';model.set_slide((crop,crop),(stride,stride));total=np.zeros((25,25),dtype=np.int64)
        cache=output.parent/'cache'/f'{crop}_{stride}';cache.mkdir(parents=True,exist_ok=True)
        binding=signature(dict(model=model.fingerprint,dataset=dataset_hash,variant=tag))
        for i,row in enumerate(rows):
            file=cache/f'{i:05d}.json'
            if file.exists():
                record=json.loads(file.read_text(encoding='utf-8'))
                if record['binding']!=binding or record['stem']!=row['stem'] or signature(record['cm'])!=record['cm_sha256']:
                    raise ValueError('Per-image verification cache differs; choose a new verification report directory')
                cm=np.asarray(record['cm'],dtype=np.int64)
                if cm.shape!=(25,25) or np.any(cm<0):raise ValueError('Invalid cached confusion matrix')
            else:
                frame=imread_bgr(row['image'])
                with Image.open(row['mask']) as im:gt=np.asarray(im)
                if gt.ndim!=2 or gt.shape!=frame.shape[:2] or not set(np.unique(gt)).issubset(set(range(25))|{255}):raise ValueError('Invalid val mask: '+row['stem'])
                prediction=model.predict(frame).argmax(0).cpu().numpy();valid=gt!=255
                cm=np.bincount((gt[valid].astype(np.int64)*25+prediction[valid]).ravel(),minlength=625).reshape(25,25)
                value=cm.tolist();save_json(file,dict(binding=binding,stem=row['stem'],cm=value,cm_sha256=signature(value)))
            total+=cm
            if (i+1)%20==0 or i+1==374:print(f'val {tag}: {i+1}/374; {time.perf_counter()-started:.1f}s',flush=True)
        tp=np.diag(total).astype(float);g=total.sum(1);pcounts=total.sum(0)
        with np.errstate(divide='ignore',invalid='ignore'):iou=tp/(g+pcounts-tp)*100;acc=tp/g*100;precision=tp/pcounts*100
        miou=float(np.nanmean(iou))
        variants[tag]=dict(images=374,mIoU=miou,expected=REFERENCES[tag],tolerance=TOLERANCE,
                           passed=abs(miou-REFERENCES[tag])<=TOLERANCE+1e-9)
        for c,name in enumerate(model.names):
            all_metrics.append(dict(variant=tag,class_id=c,class_name=name,IoU=float(iou[c]),Acc=float(acc[c]),Precision=float(precision[c]),
                TP=int(tp[c]),FP=int(pcounts[c]-tp[c]),FN=int(g[c]-tp[c]),mIoU=miou))
        with (output.parent/'per_class.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(all_metrics[0]));writer.writeheader();writer.writerows(all_metrics)
        status='incomplete'
        if not variants[tag]['passed']:status='failed'
        report=dict(status=status,fingerprint=model.fingerprint,identity=model.identity,variants=variants,
                    val_list_sha256=VAL_SHA,validation_dataset_sha256=dataset_hash,strict=True,precision='FP32')
        report['payload_sha256']=signature(report);save_json(output,report)
        if status=='failed':raise RuntimeError(f'Val {tag} failed: {miou:.8f}, expected {REFERENCES[tag]} +/-0.1. Inference remains blocked.')
        print(f'PASS val {tag}: {miou:.8f}',flush=True)
    # Do not certify a model/config/data snapshot that changed during a long run.
    fresh=RoadMarkingModel(load_settings(a.settings),load=False)
    if fresh.fingerprint!=model.fingerprint or digest(val)!=VAL_SHA:raise ValueError('Model/config/runtime changed during verification')
    if any(digest(r['image'])!=r['image_sha256'] or digest(r['mask'])!=r['mask_sha256'] for r in rows):raise ValueError('Validation bytes changed during verification')
    report.pop('payload_sha256');report['status']='passed';report['payload_sha256']=signature(report);save_json(output,report)
    print('PASS complete 374-image dual-slide verification through shared strict frame inference path. No training.',flush=True)


if __name__=='__main__':main()
