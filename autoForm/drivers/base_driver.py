import os
import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from playwright.sync_api import Page, BrowserContext
from utils.network_interceptor import NetworkInterceptor

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
            
            # 5. 填寫後半部資料 (若驗證碼是在送出前要求，預設為 before_submit)
            if self.config.get("verification_timing", "before_submit") == "before_submit":
                self.handle_verification()
            
            # 6. 提交表單
            success = self.submit()
            return success
            
        except Exception as e:
            print(f"[{self.city_code.upper()} Driver 錯誤] 填表流程中斷: {e}")
            raise e

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
