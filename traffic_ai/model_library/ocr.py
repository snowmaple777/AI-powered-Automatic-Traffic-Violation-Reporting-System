"""Independent OCR backend with bounded per-track temporal voting."""
from collections import Counter, deque
from .base import ModelResult, PerceptionModel
from .plate_text import TaiwanPlateValidator


class OCRModel(PerceptionModel):
    def __init__(self, weights, device="cpu", interval=4, min_width=40,
                 confidence=.5, taiwan_filter=True, cache_ttl=90):
        from rapidocr_onnxruntime import RapidOCR
        import onnxruntime as ort
        use_cuda = str(device).startswith("cuda") and "CUDAExecutionProvider" in ort.get_available_providers()
        # Recognition-only calls; the package supplies its character dictionary.
        self.model = RapidOCR(rec_model_path=str(weights), rec_use_cuda=use_cuda,
                              det_use_cuda=False, cls_use_cuda=False)
        if interval < 1 or cache_ttl < 1:
            raise ValueError("OCR interval and cache_ttl must be positive")
        self.interval, self.min_width, self.confidence = interval, min_width, confidence
        self.taiwan_filter, self.cache_ttl = taiwan_filter, cache_ttl
        self.reset()

    def reset(self):
        self.history = {}

    def close(self):
        self.model = None
        self.reset()

    def infer(self, frame, context, upstream):
        output = []
        h, w = frame.shape[:2]
        self.history = {k:v for k,v in self.history.items() if context.index-v["seen"] <= self.cache_ttl}
        for plate in upstream["plates"].records:
            key = plate.get("object_id")
            state = self.history.get(key) if key is not None else None
            if state is None:
                state = {"history":deque(maxlen=10), "last":-10000, "seen":context.index,
                         "raw_text":None,"text":None,"text_confidence":0.,"confirmed":False}
            state["seen"] = context.index
            x1,y1,x2,y2 = plate["bbox_xyxy"]
            interval = 30 if state["confirmed"] else self.interval
            if not plate.get("held") and x2-x1 >= self.min_width and context.index-state["last"] >= interval:
                state["last"] = context.index  # failed attempts are throttled too
                dx,dy = (x2-x1)*.05,(y2-y1)*.08
                roi = frame[max(0,int(y1-dy)):min(h,int(y2+dy)), max(0,int(x1-dx)):min(w,int(x2+dx))]
                if roi.shape[0] >= 10 and roi.shape[1] >= 20:
                    results, _ = self.model(roi, use_det=False, use_cls=False)
                    if results:
                        raw, score = str(results[0][0]), float(results[0][1])
                        if score >= self.confidence:
                            text = TaiwanPlateValidator.validate_and_normalize(raw) if self.taiwan_filter else raw
                            state["raw_text"] = raw
                            if text:
                                state["history"].append((text,score))
                                best,count = Counter(t for t,s in state["history"]).most_common(1)[0]
                                scores = [s for t,s in state["history"] if t == best]
                                avg = sum(scores)/len(scores)
                                state.update(text=best, text_confidence=avg,
                                             confirmed=(count>=3 and avg>=.7) or (count>=2 and max(scores)>=.9))
            if key is not None:
                self.history[key] = state
            output.append({**plate, **{field:state[field] for field in ("raw_text","text","text_confidence","confirmed")},
                           "ocr_source_frame":state["last"] if state["last"] >= 0 else None})
        return ModelResult(output)
