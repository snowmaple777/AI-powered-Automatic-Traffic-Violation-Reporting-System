"""
共用下拉選單比對模組 (Option Matcher)

用途：根據使用者在 info.txt 填寫的文字（例如「違規事實」、「違規行政區」），
從網頁下拉選單的所有選項中，挑出「最相近」的一個，並給出信心分數。

本模組是純 Python，不依賴 Playwright，方便單獨測試。
Driver 端只要先把選項讀成 [(value, text), ...] 再丟進來即可。

主要 API
--------
    best_match(query, options)          -> MatchResult | None
    rank_options(query, options)        -> List[MatchResult]   (由高到低)
    match_district(name, options)       -> MatchResult | None   (行政區專用，避免「東區」配到「東勢區」)
    normalize(text)                     -> str

比對分數 (0 ~ 1) 的組成
-----------------------
1. 文字正規化：NFKC 全半形統一、臺→台、去空白標點、同義詞替換 (configs/option_synonyms.json)
2. 基礎相似度：完全相同 / 包含關係 / 中文雙字組 (bigram) 覆蓋率 / difflib 序列相似度，取最高者
   並分別對「完整文字」與「去掉括號說明的核心文字」各算一次取高者
3. 法條加減分：兩邊都有條號（如 55.1.1、第56條）時，條號相符加分、不符扣分
4. 關鍵詞計數差扣分：如「臨時」、「併排」等關鍵詞的出現次數兩邊不同就扣分，
   用來區分「在禁止臨時停車處所停車」與「在禁止臨時停車處所臨時停車」這類只差幾個字的選項
"""

from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# ==================== 型別與資料結構 ====================


@dataclass(frozen=True)
class Option:
    """下拉選單中的一個選項"""
    value: str
    text: str
    index: int = -1


@dataclass
class MatchResult:
    """比對結果"""
    option: Option
    score: float
    query: str = ""
    # 完整排名（只有 best_match / match_district 回傳的結果會帶，用於除錯與提示使用者）
    candidates: List["MatchResult"] = field(default_factory=list, repr=False)
    ambiguous: bool = False

    @property
    def value(self) -> str:
        return self.option.value

    @property
    def text(self) -> str:
        return self.option.text

    def explain(self, top: int = 5) -> str:
        """輸出前幾名候選的說明文字，方便印在終端機或 GUI 上"""
        lines = [f"查詢: '{self.query}'"]
        ranked = self.candidates or [self]
        for i, c in enumerate(ranked[:top], 1):
            mark = " <== 選中" if c.option == self.option else ""
            lines.append(f"  {i}. [{c.score:.2f}] {c.option.text} (value={c.option.value}){mark}")
        if self.ambiguous:
            lines.append("  [注意] 第一名與第二名分數接近，結果可能不準確。")
        return "\n".join(lines)


OptionLike = Union[Option, Tuple[str, str], Dict[str, Any], str]
QueryLike = Union[str, Sequence[str], Sequence[Tuple[str, float]]]

# ==================== 預設參數 ====================

DEFAULT_THRESHOLD = 0.55      # 低於此分數視為「比對不到」
AMBIGUOUS_MARGIN = 0.03       # 第一、二名差距小於此值視為「不確定」
DEFAULT_QUERY_WEIGHTS = (1.0, 0.95, 0.9, 0.85)  # 多個查詢時，後面的查詢稍微降權

# 視為「請選擇」之類佔位用的選項，不參與比對
_PLACEHOLDER_RE = re.compile(r"^(請選擇|請選取|請先選擇|選擇|--+|－+|==+|全部|不拘|none|select)", re.I)

# 全台縣市名稱（行政區比對時去除前綴用）
_CITY_NAMES = [
    "台北市", "新北市", "基隆市", "桃園市", "新竹市", "新竹縣", "苗栗縣", "台中市",
    "彰化縣", "南投縣", "雲林縣", "嘉義市", "嘉義縣", "台南市", "高雄市", "屏東縣",
    "宜蘭縣", "花蓮縣", "台東縣", "澎湖縣", "金門縣", "連江縣",
]

# ==================== 同義詞設定 ====================

_DEFAULT_SYNONYMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "option_synonyms.json"
)


class _SynonymTable:
    """負責同義詞替換與關鍵詞清單，從 JSON 載入，找不到檔案時使用空設定"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or _DEFAULT_SYNONYMS_PATH
        self.alias_to_canonical: Dict[str, str] = {}
        self.discriminators: List[str] = []
        self._pattern: Optional[re.Pattern] = None
        self.reload()

    def reload(self) -> None:
        data: Dict[str, Any] = {}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

        self.alias_to_canonical = {}
        for canonical, aliases in (data.get("synonyms") or {}).items():
            canon_n = _basic_normalize(canonical)
            for alias in aliases:
                alias_n = _basic_normalize(alias)
                if alias_n and alias_n != canon_n:
                    self.alias_to_canonical[alias_n] = canon_n

        self.discriminators = [_basic_normalize(t) for t in (data.get("discriminators") or []) if t]

        if self.alias_to_canonical:
            # 長的別名優先，單次掃描替換，避免重複替換造成循環
            aliases_sorted = sorted(self.alias_to_canonical, key=len, reverse=True)
            self._pattern = re.compile("|".join(re.escape(a) for a in aliases_sorted))
        else:
            self._pattern = None

    def canonicalize(self, text: str) -> str:
        if not self._pattern or not text:
            return text
        return self._pattern.sub(lambda m: self.alias_to_canonical[m.group(0)], text)


_synonyms: Optional[_SynonymTable] = None


def _get_synonyms() -> _SynonymTable:
    global _synonyms
    if _synonyms is None:
        _synonyms = _SynonymTable()
    return _synonyms


def load_synonyms(path: Optional[str] = None) -> None:
    """重新載入同義詞設定（修改 JSON 後，或想換用其他設定檔時呼叫）"""
    global _synonyms
    _synonyms = _SynonymTable(path)


# ==================== 正規化 ====================

# 法條條號：55.1.1 / 56-1-1 / 31-6 / 第55條 / 第56條第1項第2款 / 第7條之2
_LAW_DOTTED_RE = re.compile(r"(?<!\d)(\d{1,3})[.\-－](\d{1,2})(?:[.\-－](\d{1,2}))?(?!\d)")
_LAW_ARTICLE_RE = re.compile(r"第\s*(\d{1,3})\s*條(?:\s*之\s*(\d{1,2}))?(?:\s*第\s*(\d{1,2})\s*項)?(?:\s*第\s*(\d{1,2})\s*款)?")
# 括號內的補充說明：(但機車及騎樓不在此限)、（55.1.1）
_PAREN_RE = re.compile(r"[(（\[【〔][^)）\]】〕]*[)）\]】〕]")
# 只保留中日韓文字、英文字母、數字
_NON_WORD_RE = re.compile(r"[^\w一-鿿]|_", re.UNICODE)


def _basic_normalize(text: Any) -> str:
    """NFKC、臺→台、轉小寫、去除空白與標點（不做同義詞替換）"""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = s.replace("臺", "台").replace("颱", "台")
    s = s.lower()
    return _NON_WORD_RE.sub("", s)


def normalize(text: Any, use_synonyms: bool = True) -> str:
    """完整正規化：基本正規化 + 同義詞替換"""
    s = _basic_normalize(text)
    if use_synonyms:
        s = _get_synonyms().canonicalize(s)
    return s


def _strip_law_refs(text: str) -> str:
    s = _LAW_ARTICLE_RE.sub("", text)
    return _LAW_DOTTED_RE.sub("", s)


def _core_text(raw: str) -> str:
    """去掉括號說明與條號後的「核心文字」"""
    s = unicodedata.normalize("NFKC", raw or "")
    s = _PAREN_RE.sub("", s)
    s = _strip_law_refs(s)
    return normalize(s)


def _full_text(raw: str) -> str:
    """保留括號說明，但去掉條號的完整文字"""
    s = unicodedata.normalize("NFKC", raw or "")
    s = _strip_law_refs(s)
    return normalize(s)


def extract_law_refs(raw: str) -> List[Tuple[str, ...]]:
    """
    擷取法條條號，回傳如 [('55','1','1')]、[('56',)]。
    「道交31-6」會得到 ('31','6')。
    """
    s = unicodedata.normalize("NFKC", raw or "")
    refs: List[Tuple[str, ...]] = []
    for m in _LAW_ARTICLE_RE.finditer(s):
        art, sub, item, clause = m.groups()
        art_id = f"{art}-{sub}" if sub else art
        refs.append(tuple(p for p in (art_id, item, clause) if p))
    for m in _LAW_DOTTED_RE.finditer(s):
        refs.append(tuple(p for p in m.groups() if p))
    return refs


# ==================== 相似度計算 ====================


def _bigrams(s: str) -> set:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _text_similarity(q: str, o: str) -> float:
    """兩個已正規化字串的基礎相似度 (0 ~ 1)"""
    if not q or not o:
        return 0.0
    if q == o:
        return 1.0

    scores = []

    # 包含關係
    if q in o:
        # 查詢較短，被選項包含：選項越接近查詢長度分數越高
        scores.append(0.80 + 0.15 * len(q) / len(o))
    elif o in q and len(o) >= 2:
        # 選項較短，被查詢包含（例如選項「闖紅燈」、查詢「交岔路口闖紅燈」）
        scores.append(0.70 + 0.20 * len(o) / len(q))

    # 中文雙字組：偏重「查詢內容有多少出現在選項中」(recall)
    qb, ob = _bigrams(q), _bigrams(o)
    inter = len(qb & ob)
    if inter:
        recall = inter / len(qb)
        precision = inter / len(ob)
        beta2 = 4.0  # F2：recall 權重較高
        f2 = (1 + beta2) * precision * recall / (beta2 * precision + recall)
        scores.append(f2 * 0.95)

    # 短查詢的字元組成比對：處理詞序顛倒，例如「停車違規」對「違規停車」
    if len(q) <= 10:
        qc, oc = Counter(q), Counter(o)
        common = sum((qc & oc).values())
        if common:
            c_recall, c_precision = common / len(q), common / len(o)
            c_f2 = 5 * c_precision * c_recall / (4 * c_precision + c_recall)
            scores.append(c_f2 * 0.8)

    sm = difflib.SequenceMatcher(None, q, o, autojunk=False)

    # 序列相似度
    scores.append(sm.ratio() * 0.9)

    # 最長共同片段：處理「查詢多了地點等描述」的情況
    # 例如「交岔路口闖紅燈」對「不當駕駛行為(闖紅燈)」，共同片段「闖紅燈」
    lcs = sm.find_longest_match(0, len(q), 0, len(o)).size
    if lcs >= 3:
        # 同時考慮較短與較長的一方，避免很短的通用選項（如「違規臨時停車」）分數過高
        scores.append(0.45 + 0.25 * lcs / min(len(q), len(o)) + 0.15 * lcs / max(len(q), len(o)))

    return max(scores)


def _law_adjustment(q_refs: List[Tuple[str, ...]], o_refs: List[Tuple[str, ...]]) -> float:
    """兩邊都有條號時：條號相符加分，不符扣分"""
    if not q_refs or not o_refs:
        return 0.0
    best = -1
    for qr in q_refs:
        for orf in o_refs:
            depth = 0
            for a, b in zip(qr, orf):
                if a != b:
                    break
                depth += 1
            best = max(best, depth)
    if best <= 0:
        return -0.20          # 連「第幾條」都不同
    return min(0.05 * best, 0.15)


def _discriminator_penalty(q_core: str, o_core: str) -> float:
    """關鍵詞出現次數不同就扣分（每個詞 0.12，最多 0.36）"""
    penalty = 0.0
    for term in _get_synonyms().discriminators:
        if q_core.count(term) != o_core.count(term):
            penalty += 0.12
    return min(penalty, 0.36)


def score_text(query: str, option_text: str) -> float:
    """計算一段查詢文字與一個選項文字的最終分數 (0 ~ 1)"""
    q_full, o_full = _full_text(query), _full_text(option_text)
    q_core, o_core = _core_text(query), _core_text(option_text)
    if not (q_full or q_core) or not (o_full or o_core):
        return 0.0

    # 完整/核心文字交叉比較（選項可能沒有括號說明而查詢有，或反過來），
    # 每種組合各自扣關鍵詞分數後取最高者。
    # 例如選項「不當駕駛行為(闖紅燈)」的關鍵詞在括號內，用完整文字比較才不會被誤扣。
    best = 0.0
    for qv, ov in ((q_full, o_full), (q_core, o_core), (q_core, o_full), (q_full, o_core)):
        sim = _text_similarity(qv, ov)
        # 基礎分數太低時不套用關鍵詞扣分，避免無意義的選項互相比較
        if sim >= 0.4:
            sim -= _discriminator_penalty(qv, ov)
        best = max(best, sim)

    law = _law_adjustment(extract_law_refs(query), extract_law_refs(option_text))

    return max(0.0, min(1.0, best + law))


# ==================== 輸入整理 ====================


def to_options(raw_options: Iterable[OptionLike], skip_placeholders: bool = True) -> List[Option]:
    """
    把各種格式的選項轉成 Option 清單。接受：
      - Option
      - (value, text) tuple / list
      - {"value": ..., "text": ...} dict（Playwright evaluate 常見格式；也接受 label）
      - 純字串（value 與 text 相同，適用 Vuetify 這類只有文字的清單）
    """
    result: List[Option] = []
    for i, item in enumerate(raw_options):
        if isinstance(item, Option):
            opt = item if item.index >= 0 else Option(item.value, item.text, i)
        elif isinstance(item, dict):
            text = item.get("text", item.get("label", ""))
            opt = Option(str(item.get("value", text) or ""), str(text or ""), i)
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            opt = Option(str(item[0] or ""), str(item[1] or ""), i)
        else:
            opt = Option(str(item), str(item), i)

        if skip_placeholders and is_placeholder(opt):
            continue
        result.append(opt)
    return result


def is_placeholder(opt: Option) -> bool:
    text = (opt.text or "").strip()
    if not text:
        return True
    if _PLACEHOLDER_RE.match(text):
        return True
    return False


def _to_weighted_queries(query: QueryLike) -> List[Tuple[str, float]]:
    """
    query 可以是：
      - 單一字串
      - 字串清單：依序套用預設權重 1.0, 0.95, 0.9...（第一個最重要）
      - (字串, 權重) 清單
    空字串會被略過。
    """
    if isinstance(query, str):
        items: List[Tuple[str, float]] = [(query, 1.0)]
    else:
        items = []
        for i, q in enumerate(query):
            if isinstance(q, (tuple, list)):
                items.append((str(q[0] or ""), float(q[1])))
            else:
                w = DEFAULT_QUERY_WEIGHTS[min(i, len(DEFAULT_QUERY_WEIGHTS) - 1)]
                items.append((str(q or ""), w))
    return [(q, w) for q, w in items if q and q.strip()]


# ==================== 主要 API ====================


def rank_options(query: QueryLike, options: Iterable[OptionLike]) -> List[MatchResult]:
    """回傳所有選項依分數由高到低的排名"""
    queries = _to_weighted_queries(query)
    opts = to_options(options)
    if not queries or not opts:
        return []

    label = " | ".join(q for q, _ in queries)
    results = []
    for opt in opts:
        best = 0.0
        for q, w in queries:
            best = max(best, score_text(q, opt.text) * w)
            # 選項的 value 有時是代碼或與文字相同，也比對一下（只接受完全相同）
            if opt.value and normalize(opt.value) == normalize(q):
                best = max(best, 1.0 * w)
        results.append(MatchResult(option=opt, score=round(best, 4), query=label))

    # 分數相同時保持網頁原本的順序
    results.sort(key=lambda r: (-r.score, r.option.index))
    return results


def best_match(
    query: QueryLike,
    options: Iterable[OptionLike],
    threshold: float = DEFAULT_THRESHOLD,
) -> Optional[MatchResult]:
    """
    找出最相近的選項。分數低於 threshold 時回傳 None（請不要自動亂選，改為提示使用者）。

    範例:
        r = best_match(["在人行道臨時停車", "違規臨時停車"], [("1", "..."), ("2", "...")])
        if r:
            page.select_option(selector, value=r.value)
        else:
            print("比對不到")
    """
    ranked = rank_options(query, options)
    if not ranked:
        return None
    top = ranked[0]
    top.candidates = ranked
    if len(ranked) > 1 and normalize(ranked[1].option.text) != normalize(top.option.text):
        top.ambiguous = (top.score - ranked[1].score) < AMBIGUOUS_MARGIN
    if top.score < threshold:
        return None
    return top


def explain_no_match(query: QueryLike, options: Iterable[OptionLike], top: int = 5) -> str:
    """比對失敗時，產生「使用者輸入 X，最接近的候選為 A、B、C」的說明文字"""
    ranked = rank_options(query, options)
    label = " | ".join(q for q, _ in _to_weighted_queries(query))
    lines = [f"比對不到符合的選項 (門檻 {DEFAULT_THRESHOLD})，查詢: '{label}'"]
    for i, r in enumerate(ranked[:top], 1):
        lines.append(f"  {i}. [{r.score:.2f}] {r.option.text}")
    return "\n".join(lines)


# ==================== 精確比對（不做模糊） ====================


def _exact_key(text: Any) -> str:
    """只忽略全半形差異與空白，其餘（含標點、括號）都必須相同"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", "", s)


# 結尾的條號：「(45.1.3)」、「(第45條)」、「(第53條、第53條之1)」，以及結尾句號
_TRAILING_LAW_RE = re.compile(r"(?:[。.]?\((?:第?\d+(?:\.\d+)*條?(?:之\d+)?[、,]?)+\))+$")


def _strip_trailing_law(key: str) -> str:
    s = _TRAILING_LAW_RE.sub("", key)
    return s.rstrip("。.")


def exact_match(text: str, options: Iterable[OptionLike]) -> Optional[MatchResult]:
    """
    找出文字「完全相同」的選項（只忽略空白與全半形差異）。找不到回傳 None。
    用於已經確定網頁選項文字的情況，例如 configs 中的 violation_map。

    第二層：完全相同的找不到時，兩邊都去掉結尾的條號與句號
    （如「不依規定駛入來車道」對「不依規定駛入來車道。(45.1.3)」）再比一次，
    剩下的文字仍須完全相同，且只有剛好一個選項符合才採用，否則回傳 None。
    """
    target = _exact_key(text)
    if not target:
        return None
    opts = to_options(options)
    hits = [o for o in opts if _exact_key(o.text) == target]
    if not hits:
        core = _strip_trailing_law(target)
        loose = [o for o in opts if core and _strip_trailing_law(_exact_key(o.text)) == core]
        if len(loose) != 1:
            return None
        hits = loose
    top = MatchResult(hits[0], 1.0, text)
    top.candidates = [MatchResult(o, 1.0, text) for o in hits]
    top.ambiguous = len(hits) > 1
    return top


# ==================== 行政區專用 ====================

_DISTRICT_NOISE_RE = re.compile(r"(警察局|分局|派出所|分駐所)")


def normalize_district(name: Any, city: Optional[str] = None) -> str:
    """
    行政區正規化：去掉縣市前綴、分局/派出所字樣、結尾的 區/鄉/鎮/市。
      '臺北市信義區' -> '信義'
      '彰化市'       -> '彰化'
      '東區'         -> '東'
    """
    s = _basic_normalize(name)
    for c in ([_basic_normalize(city)] if city else []) + _CITY_NAMES:
        if c and s.startswith(c) and len(s) > len(c):
            s = s[len(c):]
            break
    s = _DISTRICT_NOISE_RE.sub("", s)
    if len(s) >= 2 and s[-1] in "區鄉鎮市":
        s = s[:-1]
    return s


def match_district(
    name: str,
    options: Iterable[OptionLike],
    city: Optional[str] = None,
    threshold: float = 0.75,
) -> Optional[MatchResult]:
    """
    行政區比對。先做「去掉後綴後完全相同」的比對（避免「東區」誤配「東勢區」），
    完全找不到時才退回一般模糊比對，且門檻較高。
    """
    opts = to_options(options)
    target = normalize_district(name, city)
    if not target or not opts:
        return None

    exact = [o for o in opts if normalize_district(o.text, city) == target
             or (o.value and normalize_district(o.value, city) == target)]
    if exact:
        ranked = [MatchResult(o, 1.0, name) for o in exact]
        ranked += [MatchResult(o, 0.0, name) for o in opts if o not in exact]
        top = ranked[0]
        top.candidates = ranked
        top.ambiguous = len(exact) > 1
        return top

    # 退回比對（刻意保守，寧可回報比對不到，也不要選錯行政區）：
    # 1. 輸入是一整段地址（如「苓雅區中山二路」）時，找出地址中包含的行政區名稱
    # 2. 否則只接受序列相似度很高的情況（錯字），不使用「包含」關係，避免「東」配到「東勢」
    def fallback_score(o: Option) -> float:
        base = normalize_district(o.text, city)
        if len(base) >= 2 and len(target) > len(base) and target.startswith(base):
            return 0.9
        if len(base) < 2 or len(target) < 2:
            return 0.0
        return difflib.SequenceMatcher(None, target, base).ratio()

    ranked = sorted(
        (MatchResult(o, round(fallback_score(o), 4), name) for o in opts),
        key=lambda r: (-r.score, r.option.index),
    )
    top = ranked[0]
    top.candidates = ranked
    if top.score < threshold:
        return None
    return top
