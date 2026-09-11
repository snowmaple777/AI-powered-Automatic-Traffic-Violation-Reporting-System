import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class PingtungDriver(BaseDriver):
    """
    屏東縣交通違規檢舉填表策略 (Strategy Pattern)。
    對應網址: https://trafficmailbox.ptpolice.gov.tw/
    """

    def navigate_to_start(self) -> None:
        print(f"[Pingtung] 正在導覽至起始聲明頁面: {self.config['start_url']}")
        self.page.goto(self.config["start_url"])
        self.page.wait_for_load_state("domcontentloaded")
        
        # 註冊彈窗對話框自動確認處理器，確保流程不被彈窗阻擋
        self.page.on("dialog", lambda dialog: dialog.accept())

    def handle_pre_actions(self) -> None:
        print("[Pingtung] 正在點擊免責聲明同意核取方塊...")
        self.page.check(self.config["selectors"]["agreement_checkbox"])
        
        print("[Pingtung] 點擊「下一步」按鈕進入表單...")
        self.page.click(self.config["selectors"]["agreement_submit"])
        
        # 等待進入主表單頁面
        self.page.wait_for_url("**/traffic_write.jsp**", timeout=15000)
        print(f"[Pingtung] 成功進入資料填寫頁面: {self.page.url}")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Pingtung] 正在填寫檢舉人基本資料...")
        
        # 1. 姓名
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        
        # 2. 國籍 (預設勾選本國籍)
        self.page.check(self.config["selectors"]["reporter_nationality_domestic"])
        
        # 3. 身分證字號
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        
        # 4. 聯絡電話
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        
        # 5. 通訊地址
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        
        # 6. E-mail
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))

    def select_district(self, district_name: str) -> None:
        """選擇違規發生行政區"""
        if not district_name:
            return
            
        print(f"[Pingtung] 正在選擇違規行政區: {district_name}")
        
        # 清理行政區名稱 (例如 "屏東縣潮州鎮" -> "潮州")
        clean_district = district_name.replace("屏東縣", "").replace("屏東市", "").replace("屏東", "").replace("分局", "").replace("派出所", "").strip()
        clean_district = clean_district.rstrip("區鄉鎮市")
        
        # 選擇下拉選單 #cityarea
        self._select_option_by_keyword(self.config["selectors"]["violation_district"], clean_district)

    def select_village(self, village_name: str) -> None:
        # 屏東縣無此欄位，直接 pass
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Pingtung] 正在填寫違規內容與時間...")
        
        # 1. 違規事項 (qclass select)
        category = violation_data.get("violation_category", "")
        subcategory = violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")
        
        print(f"  - 選擇違規事項: '{category}' (小類關鍵字: '{subcategory[:10]}')")
        # 優先用小類關鍵字比對 (例如 "闖紅燈" 匹配 "(53)交岔路口闖紅燈...")
        kw = subcategory[:10] if subcategory else category
        success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], kw)
        if not success:
            # 備用大類比對
            success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], category)
        if not success:
            print("[Pingtung 警告] 無法匹配違規事項，將嘗試選擇第一個可選項目。")
            try:
                self.page.select_option(self.config["selectors"]["violation_category"], index=1)
            except Exception:
                pass
                
        # 2. 車牌號碼拆分 (屏東拆分為前/後兩欄)
        license_plate = violation_data.get("license_plate", "")
        parts = license_plate.split("-")
        if len(parts) == 2:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], parts[0].strip())
            self.page.fill(self.config["selectors"]["violation_plate_part2"], parts[1].strip())
        else:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], license_plate[:3])
            self.page.fill(self.config["selectors"]["violation_plate_part2"], license_plate[3:])
            
        # 3. 發生地點
        self.page.fill(self.config["selectors"]["violation_location_address"], violation_data.get("violation_location", ""))
        
        # 4. 違規時間 (格式 YYYY-MM-DD HH:MM)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date_hyphens(raw_date)
        time_str = violation_data.get("violation_time", "") # HH:MM
        
        datetime_val = f"{formatted_date} {time_str}".strip()
        print(f"  - 填寫違規日期時間: {datetime_val}")
        
        # 因為 #violationdatetime 具有 readonly 屬性，需先利用 JS 移除它，才能順利以 Playwright fill 填寫
        try:
            self.page.locator(self.config["selectors"]["violation_datetime"]).evaluate("el => el.removeAttribute('readonly')")
            self.page.fill(self.config["selectors"]["violation_datetime"], datetime_val)
        except Exception as dte:
            print(f"  - 移除 readonly 失敗，改用 JS 直接設定 value: {dte}")
            self.page.locator(self.config["selectors"]["violation_datetime"]).evaluate(f"el => el.value = '{datetime_val}'")
        
        # 5. 違規事實敘述
        self.page.fill(self.config["selectors"]["violation_description"], violation_data.get("violation_description", ""))
        
        # 6. 勾選同意個資法蒐集條款
        self.page.check(self.config["selectors"]["agreement_data_collect"])

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Pingtung] 沒有提供附加媒體檔案。")
            return
            
        print(f"[Pingtung] 正在上傳附加檔案 (最多 4 個): {media_paths}")
        
        # 屏東提供 4 個獨立的 filename1 ~ filename4 file 欄位
        upload_selectors = self.config["selectors"]["file_uploads"]
        for idx, path in enumerate(media_paths[:4]):
            if idx < len(upload_selectors) and os.path.exists(path):
                print(f"  - 上傳檔案到槽位 {idx+1}: {path}")
                self.page.set_input_files(upload_selectors[idx], path)

    def handle_verification(self) -> None:
        # 呼叫人機協作插件暫停流程，讓使用者填寫驗證碼
        print("[Pingtung] 觸發手動驗證 Hook...")
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        if "traffic_write" not in self.page.url:
            print("[Pingtung] 偵測到網頁已提前完成跳轉，無須重複點擊送出。")
            return self.wait_for_success()
            
        print("[Pingtung] 執行最後送出點擊...")
        self.page.click(self.config["selectors"]["submit_btn"])
        return self.wait_for_success()

    def wait_for_success(self) -> bool:
        """等待並判斷是否發送成功"""
        print("[Pingtung] 正在驗證是否檢舉成功...")
        try:
            # 成功送出後會跳轉離開填表頁
            self.page.wait_for_function("() => !window.location.href.includes('traffic_write.jsp')", timeout=10000)
            current_url = self.page.url
            print(f"[Pingtung] 已跳離填寫頁面，目前 URL: {current_url}")
            return True
        except Exception as e:
            print(f"[Pingtung 警告] 驗證成功狀態判定超時: {e}")
            
        return False

    # ==================== 輔助與格式化方法 ====================
    
    def _format_date_hyphens(self, date_str: str) -> str:
        """將各種日期格式（如 20260706、2026/07/06）格式化為 YYYY-MM-DD"""
        date_str = date_str.replace("/", "-").replace(".", "-").strip()
        # 情況 A: 8碼純數字 (例如 20260706)
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        # 情況 B: 已經是 YYYY-MM-DD 或 YYYY-M-D
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_str)
        if match:
            y, m, d = match.groups()
            return f"{y}-{int(m):02d}-{int(d):02d}"
        return date_str

    def _select_option_by_keyword(self, selector: str, keyword: str) -> bool:
        """根據關鍵字模糊比對並選取下拉選單選項"""
        try:
            options = self.page.locator(f"{selector} option").all()
            for opt in options:
                val = opt.evaluate("el => el.value")
                text = opt.evaluate("el => el.text")
                if keyword in text or keyword == val:
                    self.page.select_option(selector, value=val)
                    print(f"  - 下拉選單 [{selector}] 已成功選擇: {text}")
                    return True
        except Exception as e:
            print(f"[Pingtung 警告] 無法由關鍵字選取下拉選單 [{selector}]: {e}")
        return False
