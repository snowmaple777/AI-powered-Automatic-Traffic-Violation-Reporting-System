from typing import Callable, Any, Dict, Optional
import re
from playwright.sync_api import Page, Response

class NetworkInterceptor:
    """
    Playwright 網路請求攔截與監聽器 (Request-Driven)。
    提供攔截 API 請求、等待特定 API 回應以及解析 JSON 資料的工具，避免盲目的 time.sleep()。
    """
    def __init__(self, page: Page):
        self.page = page

    def wait_for_api_response(
        self, 
        url_pattern: str, 
        success_condition: Optional[Callable[[Response], bool]] = None, 
        timeout_ms: int = 30000
    ) -> Response:
        """
        等待特定的 API 回應。
        
        Args:
            url_pattern: 網址所包含的關鍵字或正規表示式匹配。
            success_condition: 選擇性的檢查函式，傳入 Response 對象，回傳 bool 代表是否符合成功條件。
            timeout_ms: 等待超時毫秒數 (預設 30 秒)。
        """
        def check_response(response: Response) -> bool:
            # 檢查 URL 是否匹配
            if not re.search(url_pattern, response.url):
                return False
            
            # 如果有自訂檢查條件，執行自訂條件
            if success_condition:
                try:
                    return success_condition(response)
                except Exception:
                    return False
            
            # 預設僅判定 HTTP 狀態碼為 2xx 代表成功
            return 200 <= response.status < 300

        # wait_for_event / expect_response 會在事件發生時返回 Response
        with self.page.expect_response(check_response, timeout=timeout_ms) as response_info:
            return response_info.value

    def register_json_listener(self, url_pattern: str, callback: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        """
        註冊一個非同步監聽器。當符合 url_pattern 的請求完成時，自動解析其 JSON 內容並執行 callback。
        回傳一個解除監聽的函式。
        
        Args:
            url_pattern: 網址包含的關鍵字或正規表示式。
            callback: 接收解析後字典資料的 callback 函式。
            
        Returns:
            解除監聽的函式。呼叫該函式可取消此監聽。
        """
        def on_response(response: Response):
            if re.search(url_pattern, response.url):
                # 確保是成功的回應且 Content-Type 是 JSON 相關
                if 200 <= response.status < 300:
                    try:
                        content_type = response.headers.get("content-type", "")
                        if "application/json" in content_type or response.text():
                            data = response.json()
                            callback(data)
                    except Exception:
                        pass  # 忽略解析失敗 of JSON

        self.page.on("response", on_response)
        
        # 回傳解除監聽的 clean up 函式
        return lambda: self.page.remove_listener("response", on_response)
