import os
import re
import datetime
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class TaipeiDriver(BaseDriver):
    """
    台北市交通違規檢舉自動填表策略 (Taipei Driver)
    """

    def navigate_to_start(self) -> None:
        url = "https://prsweb.tcpd.gov.tw/#/New"
        print(f"[Taipei] 正在導航至台北市檢舉登錄頁: {url}")
        self.page.goto(url, wait_until="networkidle")
        self.page.wait_for_timeout(2000)

    def handle_pre_actions(self) -> None:
        # 台北市 /#/New 頁面沒有獨立的前置同意宣告關卡，直接為填表頁
        pass

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Taipei] 正在填寫檢舉人資料...")
        
        # 填寫身分證、姓名、聯絡電話、地址、電子信箱
        self.page.fill(self.config["selectors"]["sPub_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["sPub_nm"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["sPubtel"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["sPubadd"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["email"], reporter_data.get("reporter_email", ""))
        self.page.wait_for_timeout(1000)

        # 偵測是否已經解鎖 (若先前已驗證過信箱，網頁會自動呈現第二階段欄位)
        if self._is_unlocked():
            print("[Taipei] 偵測到信箱先前已驗證，網頁已自動解鎖第二階段表單，免除發信。")
            return

        # 若未解鎖，點擊「發送認證信」
        send_btn = self.page.locator(self.config["selectors"]["btn_send_mail"])
        if send_btn.is_visible() and not send_btn.is_disabled():
            print("[Taipei] 點擊「發送認證信」按鈕...")
            send_btn.click(force=True)
            self.page.wait_for_timeout(2000)
            
            # 呼叫驗證 Hook，使流程暫停並等待使用者至信箱點擊認證連結
            print("[Taipei] 表單二階段鎖定中，呼叫驗證 Hook 等待信箱連結認證解鎖...")
            self.trigger_hook("on_verification")
        else:
            # 雙重確認，如果按鈕不可點且尚未呈現第二階段，可能需要等待或手動處理
            if not self._is_unlocked():
                print("[Taipei] 發送認證信按鈕不可用，且表單尚未解鎖。嘗試進入等待。")
                self.trigger_hook("on_verification")

    def select_district(self, district_name: str) -> None:
        print(f"[Taipei] 選擇行政區: '{district_name}'")
        self.page.click(self.config["selectors"]["district_input"], force=True)
        self.page.wait_for_timeout(500)
        
        # 尋找匹配的行政區並點選
        dist_item = self.page.locator(f".v-menu__content:visible .v-list-item:has-text('{district_name}')").first
        if dist_item.count() > 0:
            dist_item.click(force=True)
        self.page.wait_for_timeout(1000)  # 等待路段下拉選單動態載入

    def select_village(self, village_name: str) -> None:
        # 台北市無村里選擇欄位，對應為路段(Road)輸入，在 fill_violation_details 統一處理
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Taipei] 正在填寫違規案發詳細資料...")
        
        # 1. 違規日期時間處理
        raw_date = violation_data.get("violation_date", "")  # YYYY-MM-DD
        raw_time = violation_data.get("violation_time", "00:00")  # HH:MM
        
        dt_date = datetime.datetime.strptime(raw_date, "%Y-%m-%d")
        dt_time = datetime.datetime.strptime(raw_time, "%H:%M")
        
        # 點選日期下拉選單
        date_query = f"{dt_date.month} 月 {dt_date.day} 日"
        print(f"  - 選擇違規日期: {date_query}")
        self.page.click(self.config["selectors"]["date_input"], force=True)
        self.page.wait_for_timeout(500)
        date_item = self.page.locator(".v-menu__content:visible .v-list-item").filter(has_text=date_query).first
        if date_item.count() > 0:
            date_item.click(force=True)
        self.page.wait_for_timeout(500)

        # 選擇小時
        hour_val = f"{dt_time.hour}"
        print(f"  - 選擇違規小時: {hour_val} 時")
        self.page.click(self.config["selectors"]["hour_input"], force=True)
        self.page.wait_for_timeout(500)
        self._select_exact_list_item(hour_val)
        self.page.wait_for_timeout(500)

        # 選擇分鐘
        min_val = f"{dt_time.minute:02d}"
        print(f"  - 選擇違規分鐘: {min_val} 分")
        self.page.click(self.config["selectors"]["minute_input"], force=True)
        self.page.wait_for_timeout(500)
        self._select_exact_list_item(min_val)
        self.page.wait_for_timeout(500)

        # 2. 車牌號碼與車種
        plate = violation_data.get("license_plate", "").upper()
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫車牌號碼: 前碼 '{prefix}', 後碼 '{suffix}'")
        
        # 車牌種類預設一般
        self.page.click(self.config["selectors"]["plate_type_input"], force=True)
        self.page.wait_for_timeout(300)
        type_item = self.page.locator(".v-menu__content:visible .v-list-item:has-text('一般')").first
        if type_item.count() > 0:
            type_item.click(force=True)
        self.page.wait_for_timeout(300)
        
        self.page.fill(self.config["selectors"]["plate_prefix_input"], prefix)
        self.page.fill(self.config["selectors"]["plate_suffix_input"], suffix)

        # 3. 違規地點 (行政區在 select_district 填寫，此處選路段)
        location = violation_data.get("violation_location", "")
        district = violation_data.get("district", "")
        
        # 清理地點，取得路街名稱
        road_name = location.replace("臺北市", "").replace("台北市", "").replace(district, "").strip()
        
        # 正則提取路街及門牌
        road_match = re.match(r"^([^路街段]+[路街]|[^\s]+路|[^\s]+街)(?:\s*(\d+)段)?", road_name)
        base_road = road_name
        if road_match:
            base_road = road_match.group(1)
            segment = road_match.group(2)
            if segment:
                base_road += f"{segment}段"
                
        print(f"  - 搜尋與輸入路段: '{base_road}'")
        self.page.click(self.config["selectors"]["road_input"], force=True)
        self.page.fill(self.config["selectors"]["road_input"], base_road)
        self.page.wait_for_timeout(1000)
        
        # 點選下拉選單中完全匹配的項目
        self._select_exact_list_item(base_road)
        self.page.wait_for_timeout(500)

        # 門牌細項拆分填入
        lin_val, lane_val, alley_val, suballey_val, num_val, subnum_val, remark_val = self._parse_address_details(road_name, base_road)
        print(f"  - 填寫地點細項 -> 巷: '{lane_val}', 弄: '{alley_val}', 衖: '{suballey_val}', 號: '{num_val}', 之: '{subnum_val}', 地點備註: '{remark_val}'")
        
        if lane_val:
            self.page.fill(self.config["selectors"]["lane_input"], lane_val)
        if alley_val:
            self.page.fill(self.config["selectors"]["alley_input"], alley_val)
        if suballey_val:
            self.page.fill(self.config["selectors"]["alley_sub_input"], suballey_val)
        if num_val:
            self.page.fill(self.config["selectors"]["num_input"], num_val)
        if subnum_val:
            self.page.fill(self.config["selectors"]["num_sub_input"], subnum_val)
        
        self.page.fill(self.config["selectors"]["address_remark_input"], remark_val)

        # 4. 違規事實項目匹配與說明
        subcat = (violation_data.get("violation_subcategory", "") or "") + " " + (violation_data.get("violation_description", "") or "")
        subcat = subcat.strip()
        
        print(f"  - 搜尋並配對違規項目 (關鍵字: '{subcat}')...")
        self.page.click(self.config["selectors"]["law_input"], force=True)
        self.page.wait_for_timeout(800)
        
        options = self.page.locator(".v-menu__content:visible .v-list-item").all()
        matched_opt = None
        
        # 關鍵字規則比對
        for opt in options:
            txt = opt.evaluate("el => el.innerText").strip()
            if "身障" in subcat or "身心障礙" in subcat:
                if "身心障礙" in txt:
                    matched_opt = opt
                    break
            elif "併排" in subcat:
                if "併排" in txt:
                    matched_opt = opt
                    break
            elif "不依順" in subcat:
                if "不依順" in txt or "逆向" in txt:
                    matched_opt = opt
                    break
            elif "人行道" in subcat and "臨時停車" in subcat:
                if "人行道" in txt and "臨時停車" in txt:
                    matched_opt = opt
                    break
            elif "人行道" in subcat and "停車" in subcat:
                if "人行道" in txt and "停車" in txt and "臨時" not in txt:
                    matched_opt = opt
                    break
            elif "紅線" in subcat or "黃線" in subcat or "臨時停車" in subcat:
                if "設有禁止" in txt or "臨時停車" in txt:
                    matched_opt = opt
                    break
            elif "闖紅燈" in subcat:
                if "闖紅燈" in txt:
                    matched_opt = opt
                    break
                    
        # 模糊降級選取
        if not matched_opt:
            for opt in options:
                txt = opt.evaluate("el => el.innerText").strip()
                if "停車" in txt or "臨時停車" in txt:
                    matched_opt = opt
                    break
                    
        if matched_opt:
            matched_txt = matched_opt.evaluate("el => el.innerText").strip()
            print(f"    - 配對法規成功: '{matched_txt}'")
            matched_opt.click(force=True)
        else:
            if len(options) > 1:
                options[1].click(force=True)
                print("    - 無法配對法規，選取預設第二項項目。")
        self.page.wait_for_timeout(500)

        # 違規描述
        desc = violation_data.get("violation_description", "")
        self.page.fill(self.config["selectors"]["description_textarea"], desc)

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Taipei] 沒有提供媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[Taipei] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[Taipei] 正在上傳附加檔案: {[os.path.basename(p) for p in valid_paths]}")
        file_input = self.page.locator(self.config["selectors"]["file_input"]).first
        
        # 台北市支援多檔案批次選擇
        file_input.set_input_files(valid_paths)
        self.page.wait_for_timeout(2000)

    def handle_verification(self) -> None:
        # 台北市的驗證在一二階段之間 (由 fill_reporter_info 調用 on_verification 觸發)，
        # 送出前不再有額外的圖形驗證碼，因此此步驟僅為預防性 pass。
        pass

    def submit(self) -> bool:
        print("[Taipei] 自動填表完畢，勾選同意條款並儲存預覽截圖...")
        self.page.check(self.config["selectors"]["consent_checkbox"], force=True)
        self.page.wait_for_timeout(500)
        
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/tpe_filled_test.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Taipei] 全頁面填寫截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Taipei] 截圖失敗: {e}")
            
        # 安全聚焦至送出按鈕
        self.page.focus(self.config["selectors"]["btn_submit"])
        print("[Taipei] 系統安全停留在「送出」按鈕前，請使用者確認後手動點選送出提交表單。")
        return False

    # ==================== 輔助方法 ====================

    def _is_unlocked(self) -> bool:
        """檢查第二階段表單欄位是否已在 DOM 中渲染出現"""
        return self.page.locator("textarea#sRuldec").is_visible()

    def _select_exact_list_item(self, target_text: str) -> None:
        """在當前可見的下拉選單中點選精確相符的項目"""
        options = self.page.locator(".v-menu__content:visible .v-list-item").all()
        for opt in options:
            val_text = opt.evaluate("el => el.innerText").strip()
            if val_text == target_text:
                opt.click(force=True)
                return
        print(f"  [警告] 未能在下拉選單中找到完全匹配的項目: '{target_text}'")

    def _split_license_plate(self, plate: str) -> tuple:
        plate = plate.replace(" ", "").strip()
        if "-" in plate:
            parts = plate.split("-", 1)
            return parts[0], parts[1]
        elif len(plate) >= 6:
            return plate[:3], plate[3:]
        return plate, ""

    def _parse_address_details(self, road_name: str, base_road: str) -> tuple:
        """從道路剩餘地址字串中，抓取 巷、弄、衖、號、之 門牌資訊"""
        details = road_name.replace(base_road, "").strip()
        
        lane_m = re.search(r"(\d+)巷", details)
        alley_m = re.search(r"(\d+)弄", details)
        suballey_m = re.search(r"(\d+)衖", details)
        num_m = re.search(r"(\d+)號", details)
        subnum_m = re.search(r"之(\d+)", details)
        
        lane = lane_m.group(1) if lane_m else ""
        alley = alley_m.group(1) if alley_m else ""
        suballey = suballey_m.group(1) if suballey_m else ""
        num = num_m.group(1) if num_m else ""
        subnum = subnum_m.group(1) if subnum_m else ""
        
        # 移除已匹配的元素作為剩餘地點備註
        remark = details
        if lane_m: remark = remark.replace(lane_m.group(0), "")
        if alley_m: remark = remark.replace(alley_m.group(0), "")
        if suballey_m: remark = remark.replace(suballey_m.group(0), "")
        if num_m: remark = remark.replace(num_m.group(0), "")
        if subnum_m: remark = remark.replace(subnum_m.group(0), "")
        
        remark = remark.strip()
        if not remark:
            remark = "前" if num else ""
            
        return "", lane, alley, suballey, num, subnum, remark
