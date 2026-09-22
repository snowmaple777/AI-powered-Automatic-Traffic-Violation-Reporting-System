from abc import ABC
from typing import Any

class BasePlugin(ABC):
    """
    所有插件的基底類別 (Plugin Interface)。
    定義可被 Driver 生命週期觸發的事件/Hook 介面。
    """
    def on_plugin_registered(self, driver: Any) -> None:
        """當插件被註冊到 Driver 時觸發，可用於初始化設定"""
        pass

    def on_before_navigate(self, driver: Any) -> None:
        """頁面跳轉前觸發"""
        pass

    def on_verification(self, driver: Any) -> Any:
        """
        當網頁需要進行驗證碼或信箱認證時觸發。
        回傳 True 代表驗證通過，可繼續填寫表單。
        """
        pass

    def on_after_submit(self, driver: Any, success: bool) -> None:
        """表單提交完成後觸發"""
        pass
