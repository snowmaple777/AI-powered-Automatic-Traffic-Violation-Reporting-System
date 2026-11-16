"""
option_matcher 單元測試。不需要 pytest，直接執行即可：

    python tests/test_option_matcher.py

（若有安裝 pytest，也可以 `python -m pytest tests`）

注意：以下選項清單是依照《道路交通管理處罰條例》條文用語模擬的，
不是從各縣市網站實際抓下來的；接上真實網站後，請把抓到的選項補進來測試。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.option_matcher import (  # noqa: E402
    best_match, match_district, normalize, normalize_district,
    extract_law_refs, rank_options, explain_no_match, to_options,
)

# ---------- 模擬選項 ----------

CATEGORY_OPTIONS = [
    ("", "請選擇"),
    ("A", "違規停車(第56條)"),
    ("B", "違規臨時停車(第55條)"),
    ("C", "不當駕駛行為(闖紅燈)"),
    ("D", "未戴安全帽(第31條)"),
    ("E", "超速"),
]

SUB_55_OPTIONS = [
    ("0", "--請選擇--"),
    ("1", "在橋樑、隧道、圓環、障礙物對面、人行道、行人穿越道、快車道臨時停車。(55.1.1)"),
    ("2", "在交岔路口、公車站、促進大眾運輸之公共汽車專用道、消防車出入口五公尺內臨時停車。(55.1.2)"),
    ("3", "在設有禁止臨時停車標誌、標線處所臨時停車。(55.1.3)"),
    ("4", "不依順行之方向，或不緊靠道路右側，或單行道不靠右邊臨時停車。(55.1.4)"),
    ("5", "併排臨時停車。(55.1.5)"),
]

PARKING_MIXED_OPTIONS = [
    ("551", "在設有禁止臨時停車標誌、標線處所臨時停車"),
    ("5611", "在禁止臨時停車處所停車"),
    ("5612", "在設有禁止停車標誌、標線之處所停車"),
    ("5613", "在人行道停車(非騎樓)"),
    ("5614", "併排停車"),
    ("5615", "在身心障礙專用停車位停車"),
]


# ---------- 測試 ----------

def test_normalize():
    assert normalize("臺北市　信義區") == normalize("台北市信義區")
    assert normalize("ＡＢＣ－１２３４") == normalize("abc1234")
    # 同義詞
    assert "身心障礙" in normalize("身障車位")
    assert "併排" in normalize("雙排停車")


def test_extract_law_refs():
    assert ("55", "1", "1") in extract_law_refs("在人行道臨時停車。(55.1.1)")
    assert ("55",) in extract_law_refs("違規臨時停車(第55條)")
    assert ("56", "1", "2") in extract_law_refs("第56條第1項第2款")
    assert ("31", "6") in extract_law_refs("道交31-6未戴安全帽")


def test_placeholders_skipped():
    opts = to_options(CATEGORY_OPTIONS)
    assert all(o.text != "請選擇" for o in opts)
    assert len(opts) == 5


def test_category_with_law_number():
    r = best_match("違規臨時停車(第55條)", CATEGORY_OPTIONS)
    assert r and r.value == "B", r and r.explain()
    r = best_match("違規停車", CATEGORY_OPTIONS)
    assert r and r.value == "A", r and r.explain()


def test_category_fallback_to_subcategory():
    # 大類寫「交通違規檢舉專區」比對不到，改用小類「交岔路口闖紅燈」
    r = best_match(["交通違規檢舉專區", "交岔路口闖紅燈"], CATEGORY_OPTIONS)
    assert r and r.value == "C", r and r.explain()


def test_sub_55_with_law_code():
    q = "在人行道、行人穿越道臨時停車。(但機車及騎樓不在此限)(55.1.1)"
    r = best_match(q, SUB_55_OPTIONS)
    assert r and r.value == "1", r and r.explain()


def test_sub_55_taipei_wording_without_code():
    q = "汽車於人行道、行人穿越道臨時停車(但機車及騎樓不在此限)"
    r = best_match(q, SUB_55_OPTIONS)
    assert r and r.value == "1", r and r.explain()


def test_sub_55_bus_stop():
    q = "在交岔路口、公車站十公尺內或者消防車出入口五公尺內臨時停車"
    r = best_match(q, SUB_55_OPTIONS)
    assert r and r.value == "2", r and r.explain()


def test_sub_55_double_parking_slang():
    r = best_match("雙排臨停", SUB_55_OPTIONS)
    assert r and r.value == "5", r and r.explain()


def test_parking_vs_temporary_parking():
    # 只差「臨時」兩個字，必須選到 56 條的「停車」而不是 55 條的「臨時停車」
    r = best_match("在禁止臨時停車處所停車", PARKING_MIXED_OPTIONS)
    assert r and r.value == "5611", r and r.explain()
    r = best_match("在設有禁止臨時停車標誌、標線處所臨時停車", PARKING_MIXED_OPTIONS)
    assert r and r.value == "551", r and r.explain()


def test_slang_red_yellow_line():
    r = best_match("紅線停車", PARKING_MIXED_OPTIONS)
    assert r and r.value == "5611", r and r.explain()
    r = best_match("黃線停車", PARKING_MIXED_OPTIONS)
    assert r and r.value == "5612", r and r.explain()


def test_disabled_parking():
    r = best_match("違停身障車位", PARKING_MIXED_OPTIONS)
    assert r and r.value == "5615", r and r.explain()


def test_no_match_returns_none():
    assert best_match("超速行駛", PARKING_MIXED_OPTIONS) is None
    msg = explain_no_match("超速行駛", PARKING_MIXED_OPTIONS)
    assert "比對不到" in msg


def test_rank_order_and_candidates():
    r = best_match("併排停車", PARKING_MIXED_OPTIONS)
    assert r and r.value == "5614"
    assert r.candidates[0].option == r.option
    scores = [c.score for c in rank_options("併排停車", PARKING_MIXED_OPTIONS)]
    assert scores == sorted(scores, reverse=True)


def test_normalize_district():
    assert normalize_district("臺北市信義區") == "信義"
    assert normalize_district("彰化市") == "彰化"
    assert normalize_district("苓雅分局") == "苓雅"
    assert normalize_district("東區") == "東"


def test_district_east_vs_dongshi():
    opts = ["東勢區", "東區", "西區"]
    r = match_district("東區", opts)
    assert r and r.text == "東區", r and r.explain()
    r = match_district("台中市東區", opts, city="台中市")
    assert r and r.text == "東區", r and r.explain()
    r = match_district("東勢", opts)
    assert r and r.text == "東勢區", r and r.explain()
    # 選項裡沒有「東區」時，不能退而求其次選「東勢區」
    assert match_district("東區", ["東勢區", "苓雅區"]) is None
    # 輸入整段地址時，找出開頭的行政區
    assert match_district("苓雅區中山二路", ["東勢區", "苓雅區"]).text == "苓雅區"


def test_exact_match():
    from utils.option_matcher import exact_match
    opts = [("1", "機車駕駛人，不依規定使用燈光。"), ("2", "汽車駕駛人，不依規定使用燈光。")]
    assert exact_match("汽車駕駛人，不依規定使用燈光。", opts).value == "2"
    # 全形括號、空白視為相同
    assert exact_match("不當駕駛行為(燈光)(第42條)", [("5", "不當駕駛行為（燈光） （第42條）")]).value == "5"
    # 只差結尾句號可以接受（與結尾條號同規則）
    assert exact_match("汽車駕駛人，不依規定使用燈光", opts).value == "2"
    # 少字、中間標點不同都不算
    assert exact_match("駕駛人不依規定使用燈光。", opts) is None
    assert exact_match("汽車駕駛人不依規定使用燈光。", opts) is None


def test_exact_match_trailing_law_code():
    from utils.option_matcher import exact_match
    # 高雄網站實際選項（截圖）
    cats = ["請選擇違規類別", "不當駕駛行為(燈光)(第42條)", "不當駕駛行為(車道 、避讓)(第45條)",
            "不當駕駛行為(闖紅燈)(第53條、第53條之1)", "不當駕駛行為(闖平交道)(第54條)"]
    subs = ["請選擇", "不按遵行之方向行駛。(45.1.1)", "不依規定駛入來車道。(45.1.3)",
            "在多車道不依規定駕車。(45.1.4)", "機車，不在規定車道行駛。(45.1.13)"]
    # 網頁多了條號也要選得到
    assert exact_match("不當駕駛行為(車道、避讓)", cats).text == "不當駕駛行為(車道 、避讓)(第45條)"
    assert exact_match("不依規定駛入來車道", subs).text == "不依規定駛入來車道。(45.1.3)"
    assert exact_match("不依規定駛入來車道。", subs).text == "不依規定駛入來車道。(45.1.3)"
    # 完整文字當然也可以
    assert exact_match("不依規定駛入來車道。(45.1.3)", subs).value == "不依規定駛入來車道。(45.1.3)"
    # 去掉條號後仍須一字不差
    assert exact_match("駛入來車道", subs) is None
    assert exact_match("不當駕駛行為(闖紅燈)", cats).text.startswith("不當駕駛行為(闖紅燈)(第53條")
    # 去掉條號後有兩個以上相同時，不選
    dup = [("1", "違規停車。(56.1.1)"), ("2", "違規停車。(56.1.2)")]
    assert exact_match("違規停車", dup) is None


def test_reordered_category():
    # 「停車違規」只是「違規停車」的詞序顛倒，不能被「違規臨時停車」搶走
    r = best_match(["停車違規", "在禁止臨時停車處所停車"], CATEGORY_OPTIONS)
    assert r and r.value == "A", r and r.explain()


def test_district_various():
    assert match_district("臺北市信義區", ["中正區", "信義區", "大安區"]).text == "信義區"
    assert match_district("彰化市", ["員林市", "彰化市", "鹿港鎮"]).text == "彰化市"
    assert match_district("苓雅區", [("12", "苓雅區"), ("13", "前鎮區")]).value == "12"
    assert match_district("板橋區", ["中正區", "信義區"]) is None


def show_real_cases():
    """把 cases/ 裡每個 info.txt 的違規類別/事實丟進模擬選項，印出結果供人工檢查"""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cases")
    # 模擬兩層選單：大類選中後，小類選單只出現該大類的選項
    sub_by_category = {"A": PARKING_MIXED_OPTIONS, "B": SUB_55_OPTIONS}
    print("\n===== cases/ 實際案例比對（模擬兩層選單，人工檢查用）=====")
    for name in sorted(os.listdir(base)):
        info = os.path.join(base, name, "info.txt")
        if not os.path.exists(info):
            continue
        data = {}
        with open(info, encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    data[k.strip()] = v.strip()
        fact, cat = data.get("違規事實", ""), data.get("違規類別", "")
        # 第一層：大類（違規類別優先，比對不到再用違規事實）
        r1 = best_match([cat, fact], CATEGORY_OPTIONS)
        picked = f"大類[{r1.score:.2f}] {r1.text}" if r1 else "大類（比對不到）"
        # 第二層：小類
        if r1 and r1.value in sub_by_category:
            r2 = best_match([fact, cat], sub_by_category[r1.value])
            picked += f"  ->  小類[{r2.score:.2f}] {r2.text[:30]}" if r2 else "  ->  小類（比對不到）"
        print(f"{name:22s} {fact[:24]:26s} {picked}")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}\n{e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通過")
    show_real_cases()
    sys.exit(1 if failed else 0)
