"""純資料（JSON 結構）的快速深拷貝。

觀測紀錄、規則狀態與事件證據都由 dict、list、數字、字串組成；copy.deepcopy 為支援任意
物件與共享參照會為每個元素做備忘查表，在逐幀處理中佔了約三分之一的非模型時間。
clone() 只遞迴複製 dict／list／tuple，numpy 陣列以 .copy() 複製，其他物件退回 deepcopy；
不保留共享參照（同一物件出現兩次會得到兩份獨立拷貝）。
"""
import copy

import numpy as np

_IMMUTABLE = (str, int, float, bool, type(None), np.floating, np.integer, np.bool_)


def clone(value):
    kind = type(value)
    if kind is dict:
        return {key: clone(item) for key, item in value.items()}
    if kind is list:
        return [clone(item) for item in value]
    if isinstance(value, _IMMUTABLE):
        return value
    if kind is tuple:
        return tuple(clone(item) for item in value)
    if isinstance(value, np.ndarray):
        return value.copy()
    return copy.deepcopy(value)
