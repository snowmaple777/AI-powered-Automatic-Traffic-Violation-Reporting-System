import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class YunlinDriver(BaseDriver):
    """
    雲林縣交通違規檢舉填表策略 (Strategy Pattern)。
    對應網址: https://trv.ylhpb.gov.tw/Home/Report
    """

    def navigate_to_start(self) -> None:
        print(f"[Yunlin] 正在導覽至起始填表頁面: {self.config['start_url']}")
        self.page.goto(self.config["start_url"])
        self.page.wait_for_load_state("domcontentloaded")
        
        # 註冊對話方塊自動確認，避免彈窗阻塞
        self.page.on("dialog", lambda dialog: dialog.accept())

    def handle_pre_actions(self) -> None:
        # 雲林縣起始頁即為填寫表單頁，無須前置跳轉動作
        print("[Yunlin] 已直接到達表單頁面。")
        self.page.wait_for_selector(self.config["selectors"]["reporter_name"], timeout=10000)

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Yunlin] 正在填寫檢舉人基本資料...")
        
        # 1. 姓名
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        
        # 2. 身份證字號
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        
        # 3. 聯絡電話
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        
        # 4. E-mail
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        
        # 5. 通訊地址 (包含下拉選單 TownId 與詳細地址輸入框)
        address = reporter_data.get("reporter_address", "")
        # 比對並提取雲林縣的行政區
        town_match = re.search(
            r"(斗南鎮|大埤鄉|虎尾鎮|土庫鎮|褒忠鄉|東勢鄉|台西鄉|崙背鄉|麥寮鄉|斗六市|林內鄉|古坑鄉|莿桐鄉|西螺鎮|二崙鄉|北港鎮|水林鄉|口湖鄉|四湖鄉|元長鄉)", 
            address
        )
        if town_match:
            town_name = town_match.group(1)
            self._select_option_by_keyword(self.config["selectors"]["reporter_address_town"], town_name)
            # 填入行政區之後的詳細地址
            detail_addr = address.split(town_name)[-1].strip()
            self.page.fill(self.config["selectors"]["reporter_address_detail"], detail_addr)
        else:
            self.page.fill(self.config["selectors"]["reporter_address_detail"], address)

    def select_district(self, district_name: str) -> None:
        """選擇違規行政區"""
        if not district_name:
            return
            
        print(f"[Yunlin] 正在選擇違規行政區: {district_name}")
        
        # 清理行政區名稱
        clean_district = district_name.replace("雲林縣", "").replace("雲林", "").replace("分局", "").replace("派出所", "").strip()
        clean_district = clean_district.rstrip("區鄉鎮市")
        
        # 選擇違規地點行政區 #TownIdOfOccurrence
        self._select_option_by_keyword(self.config["selectors"]["violation_district"], clean_district)

    def select_village(self, village_name: str) -> None:
        # 雲林縣無此欄位，直接 pass
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Yunlin] 正在填寫違規內容與時間...")
        
        # 1. 違規車型 (CarTypeId dropdown)
        car_type = violation_data.get("car_type", "汽車")
        car_type_selector = self.config["selectors"]["car_type"]
        if "重機" in car_type or "大型重機" in car_type:
            self.page.select_option(car_type_selector, value="3") # 重機
        elif "輕機" in car_type or "機車" in car_type:
            self.page.select_option(car_type_selector, value="4") # 輕機
        else:
            self.page.select_option(car_type_selector, value="1") # 汽車
            
        # 2. 車牌號碼拆分 (FistCarNumber / LastCarNumber)
        license_plate = violation_data.get("license_plate", "")
        parts = license_plate.split("-")
        if len(parts) == 2:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], parts[0].strip())
            self.page.fill(self.config["selectors"]["violation_plate_part2"], parts[1].strip())
        else:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], license_plate[:3])
            self.page.fill(self.config["selectors"]["violation_plate_part2"], license_plate[3:])
            
        # 3. 違規日期 (HTML5 date input, 格式 YYYY-MM-DD)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date_hyphens(raw_date)
        self.page.fill(self.config["selectors"]["violation_date"], formatted_date)
        
        # 4. 違規時間 (HTML5 time input, 格式 HH:MM)
        time_str = violation_data.get("violation_time", "")
        self.page.fill(self.config["selectors"]["violation_time"], time_str)
        
        # 5. 違規項目 (ViolationId dropdown)
        category = violation_data.get("violation_category", "")
        subcategory = violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")
        
        print(f"  - 選擇違規項目: '{category}' (小類關鍵字: '{subcategory[:10]}')")
        # 優先以小類關鍵字比對
        kw = subcategory[:10] if subcategory else category
        success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], kw)
        if not success:
            # 備用大類比對
            success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], category)
        if not success:
            print("[Yunlin 警告] 無法匹配違規項目，將嘗試選擇第一個可選項目。")
            try:
                self.page.select_option(self.config["selectors"]["violation_category"], index=1)
            except Exception:
                pass
                
        # 6. 詳細違規地點
        self.page.fill(self.config["selectors"]["violation_location_address"], violation_data.get("violation_location", ""))
        
        # 7. 違規事實描述 (ViolationRemark)
        self.page.fill(self.config["selectors"]["violation_description"], violation_data.get("violation_description", ""))
        
        # 8. 勾選同意注意事項
        self.page.check(self.config["selectors"]["agreement_checkbox"])

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Yunlin] 沒有提供附加媒體檔案。")
            return
            
        print(f"[Yunlin] 正在上傳附加檔案 (最多 3 個): {media_paths}")
        
        # 雲林提供 3 個獨立的 File1 ~ File3 file 欄位
        upload_selectors = self.config["selectors"]["file_uploads"]
        for idx, path in enumerate(media_paths[:3]):
            if idx < len(upload_selectors) and os.path.exists(path):
                print(f"  - 上傳檔案到槽位 {idx+1}: {path}")
                self.page.set_input_files(upload_selectors[idx], path)

    def handle_verification(self) -> None:
        # 雲林需要發送信箱驗證碼
        print("[Yunlin] 點擊「發送驗證碼」按鈕...")
        try:
            self.page.click(self.config["selectors"]["email_send_verify"])
            self.page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[Yunlin 警告] 點擊發送驗證碼按鈕失敗: {e}")

        # 呼叫人機協作插件暫停流程，讓使用者填寫信箱驗證碼與圖片驗證碼
        print("[Yunlin] 觸發手動驗證 Hook...")
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        if "Home/Report" not in self.page.url:
            print("[Yunlin] 偵測到網頁已提前完成跳轉，無須重複點擊送出。")
            return self.wait_for_success()
            
        print("[Yunlin] 執行最後送出點擊...")
        self.page.click(self.config["selectors"]["submit_btn"])
        return self.wait_for_success()

    def wait_for_success(self) -> bool:
        """等待並判斷是否發送成功"""
        print("[Yunlin] 正在驗證是否檢舉成功...")
        try:
            # 成功送出後會跳轉離開表單頁
            self.page.wait_for_function("() => !window.location.href.includes('Home/Report')", timeout=10000)
            current_url = self.page.url
            print(f"[Yunlin] 已跳離填寫頁面，目前 URL: {current_url}")
            return True
        except Exception as e:
            print(f"[Yunlin 警告] 驗證成功狀態判定超時: {e}")
            
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
            print(f"[Yunlin 警告] 無法由關鍵字選取下拉選單 [{selector}]: {e}")
        return False
