"""唯一交接點：以原 schema 欄位提供品質合格標線，模型／違規引擎皆無需改寫。"""
from copy import deepcopy


def attach_compensation(observation, result):
    """回傳新 observation，不修改傳入物件；離線重算會讀取同一份 rule_markings。"""
    output=deepcopy(observation)
    obs=output["observations"]
    obs["road_markings_raw"]=deepcopy(result.raw_markings)
    obs["road_markings_compensated"]=deepcopy(result.compensated_markings)
    # 沿用引擎既有入口；component 多出的 compensation 欄位會保留在事件證據中。
    obs["road_markings"]=deepcopy(result.rule_markings)
    output.setdefault("postprocessing",{})["road_marking_compensation"]=deepcopy(result.diagnostics)
    return output
