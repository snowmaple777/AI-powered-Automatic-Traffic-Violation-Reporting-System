"""唯一交接點：以原 schema 欄位提供品質合格標線，模型／違規引擎皆無需改寫。"""
from core.fastcopy import clone


def attach_compensation(observation, result):
    """回傳新 observation，不修改傳入物件；離線重算會讀取同一份 rule_markings。"""
    output=clone(observation)
    obs=output["observations"]
    obs["road_markings_raw"]=clone(result.raw_markings)
    obs["road_markings_compensated"]=clone(result.compensated_markings)
    # 沿用引擎既有入口；component 多出的 compensation 欄位會保留在事件證據中。
    obs["road_markings"]=clone(result.rule_markings)
    output.setdefault("postprocessing",{})["road_marking_compensation"]=clone(result.diagnostics)
    return output
