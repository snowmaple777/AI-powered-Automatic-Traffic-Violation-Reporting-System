import os
import re
import json
import time
import unicodedata
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from playwright.sync_api import Page, BrowserContext
from utils.network_interceptor import NetworkInterceptor
from utils.option_matcher import (
    Option, MatchResult, best_match, match_district, exact_match, explain_no_match, DEFAULT_THRESHOLD,
)

class BaseDriver(ABC):
    """
    所有縣市交通檢舉 Driver 的抽象基類 (Strategy Pattern)。
    定義標準的填表生命週期與公共方法，並支援人機協作插件系統。
    """
    
    def __init__(self, city_code: str, config_path: str, context: BrowserContext, page: Page):
        self.city_code = city_code
        self.config_path = config_path
        self.context = context
        self.page = page
        
        # 載入縣市專屬配置
        self.config = self.load_config()
        
        # 初始化 Request-Driven 攔截器
        self.interceptor = NetworkInterceptor(self.page)
        
        # 插件列表
        self.plugins = []

        # 下拉選單比對不到的欄位（供流程結束時提示使用者手動處理）
        self.unmatched_fields: List[Dict[str, Any]] = []

    def load_config(self) -> Dict[str, Any]:
        """載入縣市設定檔 (config.json)"""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"找不到縣市設定檔: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def register_plugin(self, plugin: Any) -> None:
        """註冊插件，將插件掛載至 Driver 生命週期"""
        self.plugins.append(plugin)
        if hasattr(plugin, "on_plugin_registered"):
            plugin.on_plugin_registered(self)

    def trigger_hook(self, hook_name: str, *args, **kwargs) -> Any:
        """觸發插件 Hook，依序執行所有插件的對應方法"""
        result = None
        for plugin in self.plugins:
            if hasattr(plugin, hook_name):
                # 執行 hook 方法並傳遞 Driver 自身與參數
                res = getattr(plugin, hook_name)(self, *args, **kwargs)
                if res is not None:
                    result = res  # 若有返回值則記錄最後一個非空返回值
        return result

    # ==================== 範本方法 (Template Method Pattern) ====================
    
    def run_workflow(self, input_data: Dict[str, Any], media_files: List[str]) -> bool:
        """
        標準填表主流程。規範了填表的標準步驟順序。
        當遇到需要驗證的步驟時，會安全地暫停，等待使用者手動完成後，再繼續自動填寫下半部表單。
        """
        try:
            self.input_data = input_data
            
            # 1. 導覽至該縣市申報首頁
            self.navigate_to_start()
            
            # 2. 處理同意條款與前置宣告
            self.handle_pre_actions()
            
            # 3. 填寫前半部資料 (若該縣市之驗證碼是在一開始就要求，則先觸發驗證)
            if self.config.get("verification_timing") == "before_fill":
                self.handle_verification()
            
            # 4. 填寫基本資料與案件資訊
            self.fill_reporter_info(input_data)
            self.select_district(input_data.get("district", ""))
            self.select_village(input_data.get("village", ""))
            self.fill_violation_details(input_data)
            self.upload_media(media_files)

            # 提醒使用者：有下拉選單沒選到，需要手動選。
            # 放在驗證之前，讓使用者在填驗證碼時就看得到紅框（上傳檔案的 PostBack 會清掉先前的紅框，這裡會重新標上）
            self.report_unmatched_fields()

            # 5. 填寫後半部資料 (若驗證碼是在送出前要求，預設為 before_submit)
            if self.config.get("verification_timing", "before_submit") == "before_submit":
                self.handle_verification()

            # 6. 提交表單
            success = self.submit()
            return success
            
        except Exception as e:
            print(f"[{self.city_code.upper()} Driver 錯誤] 填表流程中斷: {e}")
            raise e

    # ==================== 共用下拉選單工具 (原生 <select>) ====================

    def read_select_options(self, selector: str) -> List[Option]:
        """讀取原生 <select> 的所有選項"""
        raw = self.page.eval_on_selector(
            selector,
            "s => [...s.options].map((o, i) => ({value: o.value, text: (o.text || '').trim(), index: i}))",
        )
        return [Option(r["value"], r["text"], r["index"]) for r in raw]

    def _options_signature(self, selector: str) -> Optional[str]:
        """選項內容的指紋，用來判斷選單是否已更新；頁面重新載入中或找不到元素時回傳 None"""
        try:
            return self.page.eval_on_selector(
                selector, "s => [...s.options].map(o => o.value + '\\u0001' + o.text).join('\\u0002')"
            )
        except Exception:
            return None

    def wait_for_options_change(self, selector: str, old_signature: Optional[str], timeout_ms: int = 10000) -> bool:
        """
        等待選單的選項內容改變（例如上層選單選完後，子選單載入新選項）。
        同時適用 AJAX 更新與 ASP.NET 整頁 PostBack：頁面重新載入期間的錯誤會被忽略並持續等待。
        回傳是否在時限內偵測到改變。
        """
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            sig = self._options_signature(selector)
            # 有選項（扣掉「請選擇」至少一項）且內容與先前不同
            if sig is not None and sig != old_signature and sig.count("\u0002") >= 1:
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                return True
            self.page.wait_for_timeout(200)
        return False

    def _is_postback_select(self, selector: str) -> bool:
        """判斷 <select> 是否為 ASP.NET AutoPostBack（選了會整頁重新載入）"""
        try:
            onchange = self.page.get_attribute(selector, "onchange") or ""
            return "__doPostBack" in onchange
        except Exception:
            return False

    def _apply_select(self, selector: str, value: str, timeout_ms: int = 15000) -> None:
        """選取選項；若是 AutoPostBack 選單且值有改變，等頁面重新載入完成"""
        current = self.page.eval_on_selector(selector, "s => s.value")
        if current == value:
            return
        if self._is_postback_select(selector):
            try:
                with self.page.expect_navigation(timeout=timeout_ms, wait_until="domcontentloaded"):
                    self.page.select_option(selector, value=value)
                return
            except Exception:
                # UpdatePanel 之類的局部更新不會換頁，改等網路閒置
                try:
                    self.page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                return
        self.page.select_option(selector, value=value)

    def select_native(
        self,
        selector: str,
        query: Union[str, Sequence[Any]],
        field_label: str = "",
        threshold: float = DEFAULT_THRESHOLD,
        district: bool = False,
        city: Optional[str] = None,
    ) -> Optional[MatchResult]:
        """
        依使用者輸入，從原生 <select> 中選出最相近的選項。
        - query 可給多個查詢（例如 [違規事實, 違規類別]），越前面越重要
        - district=True 時使用行政區專用比對
        - 比對不到時「不會亂選」，而是在網頁上標紅該欄位並記錄到 unmatched_fields
        """
        label = field_label or selector
        queries = [query] if isinstance(query, str) else [q for q in query if q]
        if not queries or not any((q[0] if isinstance(q, (tuple, list)) else q) for q in queries):
            print(f"  - [{label}] 沒有可用的輸入資料，略過。")
            return None

        try:
            options = self.read_select_options(selector)
        except Exception as e:
            print(f"  - [{label}] 讀取選項失敗: {e}")
            self._record_unmatched(selector, label, queries, f"讀取選項失敗: {e}")
            return None

        if district:
            result = match_district(queries[0] if isinstance(queries[0], str) else queries[0][0], options, city=city)
        else:
            result = best_match(queries, options, threshold=threshold)

        if result is None:
            detail = explain_no_match(queries, options)
            print(f"  - [{label}] {detail}")
            self._record_unmatched(selector, label, queries, detail)
            return None

        self._apply_select(selector, result.value)
        flag = "（分數接近，請確認）" if result.ambiguous else ""
        print(f"  - [{label}] 已選擇: {result.text} (分數 {result.score:.2f}){flag}")
        if result.ambiguous:
            print("    " + result.explain(top=3).replace("\n", "\n    "))
        return result

    def select_exact(self, selector: str, option_text: str, field_label: str = "") -> Optional[MatchResult]:
        """
        選取文字「完全相同」的選項（只忽略空白與全半形差異），不做模糊比對。
        找不到時不會亂選，而是標紅欄位並記錄到 unmatched_fields。
        """
        label = field_label or selector
        if not option_text:
            print(f"  - [{label}] 沒有指定要選的選項，略過。")
            return None
        try:
            options = self.read_select_options(selector)
        except Exception as e:
            print(f"  - [{label}] 讀取選項失敗: {e}")
            self._record_unmatched(selector, label, option_text, f"讀取選項失敗: {e}")
            return None

        result = exact_match(option_text, options)
        if result is None:
            available = "\n".join(f"      - {o.text}" for o in options if o.text)
            detail = f"網頁上找不到選項「{option_text}」。目前網頁上的選項：\n{available}"
            print(f"  - [{label}] {detail}")
            self._record_unmatched(selector, label, option_text, detail)
            return None

        self._apply_select(selector, result.value)
        print(f"  - [{label}] 已選擇: {result.text}")
        return result

    def select_cascade(
        self,
        parent_selector: str,
        parent_query: Union[str, Sequence[Any]],
        child_selector: str,
        child_query: Union[str, Sequence[Any]],
        parent_label: str = "",
        child_label: str = "",
        timeout_ms: int = 15000,
        exact: bool = False,
    ) -> Optional[MatchResult]:
        """
        兩層連動選單：選上層 → 等子選單選項真的更新 → 再選子選單。
        exact=True 時兩層都用 select_exact（parent_query / child_query 必須是網頁上的完整選項文字）。
        回傳子選單的比對結果（上層或子選單任一比對不到時回傳 None）。
        """
        if exact:
            pick = lambda sel, q, lbl: self.select_exact(sel, q, field_label=lbl)
        else:
            pick = lambda sel, q, lbl: self.select_native(sel, q, field_label=lbl)

        old_child_sig = self._options_signature(child_selector)
        parent_before = None
        try:
            parent_before = self.page.eval_on_selector(parent_selector, "s => s.value")
        except Exception:
            pass

        parent_result = pick(parent_selector, parent_query, parent_label)
        if parent_result is None:
            # 上層沒選到，子選單也無法正確選取，一併標記
            self._record_unmatched(child_selector, child_label or child_selector, child_query, "上層選單未選取")
            return None

        if parent_result.value != parent_before:
            if not self.wait_for_options_change(child_selector, old_child_sig, timeout_ms):
                print(f"  - [{child_label or child_selector}] 等待 {timeout_ms // 1000} 秒子選單仍未更新，以目前選項嘗試比對。")

        return pick(child_selector, child_query, child_label)

    # ==================== 違規項目：共用目錄查表 + 模糊比對 ====================
    # 流程（與高雄 violation_map 同一套名稱）：
    #   1. info.txt 的「違規事實」優先、其次「違規類別」，對照 configs/violation_catalog.json 的名稱與 aliases
    #      （只忽略空白與全半形差異，文字須相同），確定是哪一種違規
    #   2. 用該違規的 queries（標準用語＋條號）到各縣市網站的選項中模糊比對
    #   3. 目錄裡沒有這種違規時，退回用 info.txt 原文模糊比對
    #   4. 比對不到就不選，標紅框交給使用者手動選（不會預設亂選）

    VIOLATION_THRESHOLD = 0.6
    _violation_catalog: Optional[Dict[str, Dict[str, Any]]] = None

    @staticmethod
    def _violation_key(text: Any) -> str:
        s = unicodedata.normalize("NFKC", str(text or ""))
        return re.sub(r"\s+", "", s)

    @classmethod
    def load_violation_catalog(cls) -> Dict[str, Dict[str, Any]]:
        if BaseDriver._violation_catalog is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "configs", "violation_catalog.json")
            data: Dict[str, Any] = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            BaseDriver._violation_catalog = {
                k: v for k, v in (data.get("violations") or {}).items() if not k.startswith("_")
            }
        return BaseDriver._violation_catalog

    def resolve_violation(self, violation_data: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """依違規事實／違規類別查目錄，回傳 (違規名稱, 目錄項目)；查不到回傳 None"""
        catalog = self.load_violation_catalog()
        lookup: Dict[str, str] = {}
        for name, entry in catalog.items():
            for n in [name] + list(entry.get("aliases", [])):
                lookup[self._violation_key(n)] = name
        for text in (violation_data.get("violation_subcategory"), violation_data.get("violation_category")):
            key = self._violation_key(text)
            if key and key in lookup:
                name = lookup[key]
                return name, catalog[name]
        return None

    def violation_queries(self, violation_data: Dict[str, Any]) -> List[str]:
        """模糊比對用的查詢文字：目錄有這種違規就用目錄的 queries，否則用 info.txt 原文"""
        resolved = self.resolve_violation(violation_data)
        if resolved:
            name, entry = resolved
            return list(entry.get("queries") or [name])
        return [t.strip() for t in (violation_data.get("violation_subcategory"),
                                    violation_data.get("violation_category")) if t and t.strip()]

    def violation_kind(self, violation_data: Dict[str, Any]) -> Optional[str]:
        """目錄中的 kind：static（停車類）/ dynamic（行駛類）；目錄沒有這種違規時回傳 None"""
        resolved = self.resolve_violation(violation_data)
        return resolved[1].get("kind") if resolved else None

    def match_violation(self, options: Sequence[Any], violation_data: Dict[str, Any],
                        field_label: str = "違規事實") -> Optional[MatchResult]:
        """
        從任意選項清單（<select> 選項、radio/checkbox 標籤、Vuetify 清單文字…）中找出符合的違規項目。
        只比對、不操作網頁；options 可為 Option、(value, text) 或純文字。
        """
        resolved = self.resolve_violation(violation_data)
        queries = self.violation_queries(violation_data)
        source = f"違規項目「{resolved[0]}」" if resolved else "info.txt 原文（不在違規目錄中）"
        if not queries:
            print(f"  - [{field_label}] info.txt 沒有違規事實/違規類別，略過。")
            return None
        result = best_match(queries, options, threshold=self.VIOLATION_THRESHOLD)
        if result is None:
            print(f"  - [{field_label}] 依{source}比對不到：")
            print("    " + explain_no_match(queries, options).replace("\n", "\n    "))
            return None
        flag = "（分數接近，請確認）" if result.ambiguous else ""
        print(f"  - [{field_label}] 依{source} → {result.text} (分數 {result.score:.2f}){flag}")
        if result.ambiguous:
            print("    " + result.explain(top=3).replace("\n", "\n    "))
        return result

    def violation_unmatched(self, selector: str, violation_data: Dict[str, Any],
                            field_label: str = "違規事實", detail: str = "") -> None:
        """違規項目比對不到：標紅框並記錄，請使用者手動選擇"""
        queries = self.violation_queries(violation_data)
        self._record_unmatched(selector, field_label, queries,
                               detail or "依違規目錄比對不到符合的選項，請手動選擇")

    def select_violation(self, selector: str, violation_data: Dict[str, Any],
                         field_label: str = "違規事實") -> Optional[MatchResult]:
        """原生 <select> 的違規項目選單：比對到就選取，比對不到就標紅框"""
        try:
            options = self.read_select_options(selector)
        except Exception as e:
            print(f"  - [{field_label}] 讀取選項失敗: {e}")
            self.violation_unmatched(selector, violation_data, field_label, f"讀取選項失敗: {e}")
            return None
        result = self.match_violation(options, violation_data, field_label)
        if result is None:
            self.violation_unmatched(selector, violation_data, field_label)
            return None
        self._apply_select(selector, result.value)
        return result

    def _record_unmatched(self, selector: str, label: str, queries: Any, detail: str) -> None:
        self.unmatched_fields.append({"selector": selector, "label": label, "query": queries, "detail": detail})
        self._highlight(selector)
        self.trigger_hook("on_field_unmatched", label, detail)

    def _highlight(self, selector: str) -> None:
        """在網頁上把欄位標紅，方便使用者找到要手動選的地方"""
        try:
            self.page.eval_on_selector(
                selector, "s => { s.style.outline = '3px solid red'; s.style.outlineOffset = '2px'; }"
            )
        except Exception:
            pass

    def report_unmatched_fields(self) -> None:
        if not self.unmatched_fields:
            return
        # 之後的 PostBack（例如上傳檔案）會讓紅框消失，這裡重新標一次
        for f in self.unmatched_fields:
            self._highlight(f["selector"])
        print("\n" + "!" * 70)
        print(f" 有 {len(self.unmatched_fields)} 個下拉選單無法自動比對，已在網頁上以紅框標示，請手動選擇：")
        for f in self.unmatched_fields:
            print(f"   - {f['label']}  (輸入: {f['query']})")
        print("!" * 70 + "\n")

    # ==================== 必須由各縣市 Driver 實作的抽象方法 ====================

    @abstractmethod
    def navigate_to_start(self) -> None:
        """導航至填表起始頁面"""
        pass

    @abstractmethod
    def handle_pre_actions(self) -> None:
        """處理同意書勾選、下一步按鈕等前置步驟"""
        pass

    @abstractmethod
    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        """填寫檢舉人姓名、身分證、電話、Email、地址"""
        pass

    @abstractmethod
    def select_district(self, district_name: str) -> None:
        """選擇行政區 (如：板橋區、苓雅區)"""
        pass

    @abstractmethod
    def select_village(self, village_name: str) -> None:
        """選擇村里路段 (若該縣市無此欄位，可為 pass)"""
        pass

    @abstractmethod
    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        """填寫違規時間、違規車牌、項目、地點詳述等"""
        pass

    @abstractmethod
    def upload_media(self, media_paths: List[str]) -> None:
        """上傳影像佐證資料"""
        pass

    @abstractmethod
    def handle_verification(self) -> None:
        """
        處理網頁驗證 (防機器人機制)。
        子類別應在此處調用 self.trigger_hook("on_verification")
        以便讓「人機協作輔助插件」暫停流程並等待使用者手動操作。
        """
        pass

    @abstractmethod
    def submit(self) -> bool:
        """提交表單並利用 NetworkInterceptor 確認是否成功"""
        pass
