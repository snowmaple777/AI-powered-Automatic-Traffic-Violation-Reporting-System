import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class TaichungDriver(BaseDriver):
    """
    台中市交通違規檢舉表單填寫策略 (Vuetify 單頁式表單)
    """
    
    def navigate_to_start(self) -> None:
        print(f"[Taichung] 正在導覽至台中市首頁: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)
        
        # 點擊我已閱讀並同意
        print("[Taichung] 勾選同意檢舉聲明...")
        self.page.locator(self.config["selectors"]["agreement_checkbox"]).click()
        self.page.wait_for_timeout(500)
        
        # 點擊開始檢舉
        print("[Taichung] 點擊「開始檢舉」進入表單頁面...")
        self.page.locator(self.config["selectors"]["start_button"]).click()
        self.page.wait_for_url("**/wCase**", timeout=10000)
        self.page.wait_for_load_state("domcontentloaded")
        print("[Taichung] 成功進入檢舉表單頁面")
        
        # 註冊彈窗自動確認處理器
        self.page.on("dialog", lambda dialog: dialog.accept())

    def handle_pre_actions(self) -> None:
        # 台中市無前期驗證，直接 pass
        pass

    def handle_verification(self) -> None:
        # 台中市無特殊前置驗證，若有需要可使用人機協作插件
        pass

    def select_district(self, district_name: str) -> None:
        if not district_name:
            return
        # 移去「區鄉鎮市」字尾
        clean_dist = district_name.rstrip("區鄉鎮市")
        print(f"  - 選擇行政區: {clean_dist}")
        self.page.click(self.config["selectors"]["location_town"])
        self.page.wait_for_timeout(500)
        self.page.locator(f"div.v-menu__content:visible .v-list-item:has-text('{clean_dist}')").first.click()
        self.page.wait_for_timeout(1000) # 等待路段與門牌欄位解鎖

    def select_village(self, village_name: str) -> None:
        # 台中市無村里欄位，直接 pass
        pass

    def submit(self) -> None:
        print("[Taichung] 自動填表完畢，安全停留在送出表單前，請使用者確認無誤後手動提交。")

    def parse_address_components(self, address: str) -> dict:
        # 移去「台中市/臺中市」前綴
        addr = address.replace("台中市", "").replace("臺中市", "")
        
        # 提取行政區
        town = ""
        for t in ["西屯區", "北屯區", "南屯區", "東區", "南區", "西區", "北區", "中區", "大里區", "太平區", "豐原區", "沙鹿區", "大甲區", "清水區", "梧棲區", "后里區", "神岡區", "潭子區", "大雅區", "新社區", "石岡區", "外埔區", "大安區", "烏日區", "大肚區", "龍井區", "霧峰區", "和平區", "東勢區"]:
            if t in addr:
                town = t
                addr = addr.replace(t, "")
                break
                
        road = ""
        lane = ""
        alley = ""
        number = ""
        sub_number = ""
        remark = address # 預設備註為完整地址
        
        # 檢查是否為交叉路口
        intersection_match = re.search(r"([^與/]+)(?:與|/)([^口]+)口?", addr)
        if intersection_match:
            road = intersection_match.group(1).strip()
        else:
            # 道路
            road_match = re.search(r"([^路街段]+[路街段](?:\d+段)?)", addr)
            if road_match:
                road = road_match.group(1).strip()
                addr = addr.replace(road, "")
            
            # 巷
            lane_match = re.search(r"(\d+)巷", addr)
            if lane_match:
                lane = lane_match.group(1)
                addr = addr.replace(lane_match.group(0), "")
                
            # 弄
            alley_match = re.search(r"(\d+)弄", addr)
            if alley_match:
                alley = alley_match.group(1)
                addr = addr.replace(alley_match.group(0), "")
                
            # 號
            number_match = re.search(r"(\d+)號", addr)
            if number_match:
                number = number_match.group(1)
                addr = addr.replace(number_match.group(0), "")
                
            # 之
            sub_match = re.search(r"[之\-](\d+)", addr)
            if sub_match:
                sub_number = sub_match.group(1)
                
        return {
            "town": town,
            "road": road,
            "lane": lane,
            "alley": alley,
            "number": number,
            "sub_number": sub_number,
            "remark": remark
        }

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Taichung] 正在填寫檢舉人聯絡資料...")
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Taichung] 正在填寫違規案發詳細資料...")
        
        # 1. 違規日期 (Vuetify 依 label 尋找動態 ID)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_roc_date_string(raw_date) # YYYMMDD (如 1150715)
        date_input_id = self.page.locator(self.config["selectors"]["violation_date_label"]).first.get_attribute("for")
        self.page.fill(f"#{date_input_id}", formatted_date)
        
        # 2. 違規時間 (格式 1200, 2330 移除冒號)
        raw_time = violation_data.get("violation_time", "")
        formatted_time = raw_time.replace(":", "")
        time_input_id = self.page.locator(self.config["selectors"]["violation_time_label"]).first.get_attribute("for")
        self.page.fill(f"#{time_input_id}", formatted_time)
        
        # 3. 違規車號-車種 (Vuetify 下拉，選擇「一般」號牌)
        self.page.click(self.config["selectors"]["vehicle_type"])
        self.page.wait_for_timeout(500)
        self.page.locator("div.v-menu__content:visible .v-list-item:has-text('一般')").first.click()
        self.page.wait_for_timeout(500)
        
        # 4. 車牌號碼拆分
        license_plate = violation_data.get("license_plate", "")
        parts = license_plate.split("-")
        if len(parts) == 2:
            self.page.fill(self.config["selectors"]["license_plate_1"], parts[0].strip())
            self.page.fill(self.config["selectors"]["license_plate_2"], parts[1].strip())
        else:
            self.page.fill(self.config["selectors"]["license_plate_1"], license_plate[:3])
            self.page.fill(self.config["selectors"]["license_plate_2"], license_plate[3:])
            
        # 5. 違規地點路段解析與填寫
        addr_info = self.parse_address_components(violation_data.get("violation_location", ""))
        print(f"  - 解析地點詳細元素: {addr_info}")
        
        # A. 行政區 (我們已在 select_district 中選取行政區，此處若未填可補填)
        if not self.page.locator(self.config["selectors"]["location_town"]).evaluate("el => el.value") and addr_info["town"]:
            self.page.click(self.config["selectors"]["location_town"])
            self.page.wait_for_timeout(500)
            self.page.locator(f"div.v-menu__content:visible .v-list-item:has-text('{addr_info['town']}')").first.click()
            self.page.wait_for_timeout(1000)
            
        # B. 路段 (Autocomplete 輸入搜尋後點選)
        if addr_info["road"]:
            road_input = self.page.locator(self.config["selectors"]["location_road"])
            road_input.click()
            road_input.fill("")
            # 自動將「台」替換為「臺」，以相容官方「臺灣大道」路名
            normalized_road = addr_info["road"].replace("台", "臺")
            road_input.press_sequentially(normalized_road, delay=100)
            self.page.wait_for_timeout(1500)
            # 點選選單中第一個路段匹配項
            self.page.locator("div.v-menu__content:visible .v-list-item").first.click()
            self.page.wait_for_timeout(500)
            
        # C. 巷/弄/號/之/備註
        if addr_info["lane"]:
            self.page.fill(self.config["selectors"]["location_lane"], addr_info["lane"])
        if addr_info["alley"]:
            self.page.fill(self.config["selectors"]["location_alley"], addr_info["alley"])
        if addr_info["number"]:
            self.page.fill(self.config["selectors"]["location_number"], addr_info["number"])
        if addr_info["sub_number"]:
            self.page.fill(self.config["selectors"]["location_sub_number"], addr_info["sub_number"])
            
        self.page.fill(self.config["selectors"]["location_remark"], addr_info["remark"])
        
        # 6. 違規事實 (Vuetify v-select 唯讀選單，藉由滾動選單容器來載入並選取關鍵字選項)
        fact_input = self.page.locator(self.config["selectors"]["violation_fact"])
        fact_input.click()
        self.page.wait_for_timeout(1000)
        
        # 滾動選單容器以確保所有項目載入完成
        menu_loc = self.page.locator("div.v-menu__content:visible")
        print("[Taichung] 正在滾動載入所有違規事實選單項目...")
        for scroll_step in range(12):
            try:
                menu_loc.evaluate(f"el => el.scrollTop = {scroll_step * 800 + 800}")
                self.page.wait_for_timeout(300)
            except Exception:
                break
                
        # 獲取所有已渲染的選項
        opts = self.page.locator("div.v-menu__content:visible .v-list-item").all()
        fact_desc = violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")
        
        # 進行多層級優先度匹配，確保選中正確法條 (如：優先選取 56 條或 55 條)
        target_option = None
        
        # 級別 1: 包含 56 且包含 停車 (最精確的紅線/禁止停車處所停車)
        for opt in opts:
            text = opt.evaluate("el => el.innerText").strip()
            if "56" in text and "停車" in text:
                target_option = opt
                break
                
        # 級別 2: 包含 55 (臨時停車)
        if not target_option:
            for opt in opts:
                text = opt.evaluate("el => el.innerText").strip()
                if "55" in text:
                    target_option = opt
                    break
                    
        # 級別 3: 包含 56
        if not target_option:
            for opt in opts:
                text = opt.evaluate("el => el.innerText").strip()
                if "56" in text:
                    target_option = opt
                    break
                    
        # 級別 4: 包含重要關鍵字 (紅線/人行道/紅燈)
        if not target_option:
            for kw in ["紅線", "人行道", "紅燈", "公車", "公交"]:
                if kw in fact_desc:
                    for opt in opts:
                        text = opt.evaluate("el => el.innerText").strip()
                        if kw in text:
                            target_option = opt
                            break
                if target_option:
                    break
                    
        # 級別 5: 包含 停車 且不包含 快速公路/鐵路平交道/身障
        if not target_option:
            for opt in opts:
                text = opt.evaluate("el => el.innerText").strip()
                if "停車" in text and "鐵路" not in text and "快速" not in text:
                    target_option = opt
                    break
                    
        if target_option:
            print(f"  - 尋找到符合的違規事實選項: '{target_option.evaluate('el => el.innerText').strip()}'")
            target_option.click()
        else:
            print("[Taichung 警告] 無法由滾動搜尋到 55/56/停車 等關鍵字選項，將預設點選第一個有效項目。")
            self.page.locator("div.v-menu__content:visible .v-list-item").first.click()
            
        self.page.wait_for_timeout(500)
        
        # 7. 違規事實說明
        self.page.fill(self.config["selectors"]["violation_description"], violation_data.get("violation_description", ""))
        
        # 8. 勾選確認車牌清晰
        self.page.locator(self.config["selectors"]["plate_confirm_checkbox"]).click()
        self.page.wait_for_timeout(500)
        
        # 9. 勾選隱私權政策與個資真實宣告
        self.page.locator(self.config["selectors"]["policy_confirm_checkbox"]).click()
        self.page.wait_for_timeout(500)

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Taichung] 沒有提供附加媒體檔案。")
            return
            
        print(f"[Taichung] 正在上傳附加檔案 (最多 3 個): {media_paths}")
        
        # 台中市使用單一多檔案上傳控制項
        valid_paths = [p for p in media_paths if os.path.exists(p)][:3]
        if valid_paths:
            self.page.set_input_files(self.config["selectors"]["file_input"], valid_paths)
            print("  - 檔案已排隊上傳。等待 4 秒處理縮圖...")
            self.page.wait_for_timeout(4000)

    # ==================== 輔助方法 ====================

    def _format_roc_date_string(self, date_str: str) -> str:
        """將各種日期格式（如 20260715、2026-07-15、2026/07/15）格式化為民國年月日純數字字串 YYYMMDD (如 1150715)"""
        date_str = date_str.replace("/", "-").replace(".", "-").strip()
        # 提取數字
        digits = re.sub(r"\D", "", date_str)
        if len(digits) == 8:
            y = int(digits[:4])
            m = int(digits[4:6])
            d = int(digits[6:])
        else:
            match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_str)
            if match:
                y, m, d = map(int, match.groups())
            else:
                # 備用：無法解析則直接回傳原字串
                return date_str
        
        # 轉換為民國年
        roc_y = y - 1911
        return f"{roc_y}{m:02d}{d:02d}"
