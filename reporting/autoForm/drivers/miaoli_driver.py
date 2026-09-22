import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class MiaoliDriver(BaseDriver):
    """
    苗栗縣交通違規檢舉自動填表策略 (Miaoli Driver)
    """

    TOWN_MAP = {
        "苗栗": "苗栗市",
        "頭份": "頭份市",
        "竹南": "竹南鎮",
        "後龍": "後龍鎮",
        "通霄": "通霄鎮",
        "苑裡": "苑裡鎮",
        "卓蘭": "卓蘭鎮",
        "造橋": "造橋鄉",
        "頭屋": "頭屋鄉",
        "公館": "公館鄉",
        "大湖": "大湖鄉",
        "泰安": "泰安鄉",
        "銅鑼": "銅鑼鄉",
        "三義": "三義鄉",
        "西湖": "西湖鄉",
        "三灣": "三灣鄉",
        "南庄": "南庄鄉",
        "獅潭": "獅潭鄉"
    }

    def navigate_to_start(self) -> None:
        url = self.config.get("start_url", "https://trv.mpb.gov.tw/Home/Report")
        print(f"[Miaoli] 正在導航至苗栗縣檢舉門戶: {url}")
        self.page.goto(url, wait_until="networkidle")
        self.page.wait_for_timeout(1500)

    def handle_pre_actions(self) -> None:
        # 苗栗縣單頁整合式表單無前置條款頁
        pass

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Miaoli] 正在填寫檢舉人資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        
        # 住址行政區可選比對
        addr = reporter_data.get("reporter_address", "")
        for key, val in self.TOWN_MAP.items():
            if key in addr:
                try:
                    self.page.select_option(self.config["selectors"]["reporter_district"], label=val)
                    print(f"  - 住址行政區配對: {val}")
                except Exception:
                    pass
                break

    def select_district(self, district_name: str) -> None:
        clean_dist = district_name.strip()
        target_town = "苗栗市"
        for key, val in self.TOWN_MAP.items():
            if key in clean_dist or clean_dist in key:
                target_town = val
                break
        print(f"  - 選擇違規發生行政區: '{target_town}'")
        try:
            self.page.select_option(self.config["selectors"]["violation_district"], label=target_town)
        except Exception as e:
            print(f"  [警告] 選擇違規行政區失敗: {e}")

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Miaoli] 正在填寫違規案發詳細資料...")

        # 1. 違規車種類型與前後雙欄車牌
        car_type = violation_data.get("car_type", "汽車")
        type_val = "1"  # 預設汽車
        if "重機" in car_type or "大型重機" in car_type:
            type_val = "3"
        elif "輕機" in car_type or "綠牌" in car_type or "機車" in car_type:
            type_val = "4"
        elif "拖車" in car_type:
            type_val = "2"
            
        print(f"  - 選擇車種代碼: {type_val} ({car_type})")
        self.page.select_option(self.config["selectors"]["car_type"], value=type_val)

        plate = violation_data.get("license_plate", "").upper().strip()
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫車牌號碼: 前碼 '{prefix}', 後碼 '{suffix}'")
        self.page.fill(self.config["selectors"]["plate_first"], prefix)
        self.page.fill(self.config["selectors"]["plate_last"], suffix)

        # 2. 違規時間 (格式 YYYY-MM-DD HH:mm)
        raw_date = violation_data.get("violation_date", "")
        raw_time = violation_data.get("violation_time", "00:00")
        formatted_dt = self._format_datetime(raw_date, raw_time)
        print(f"  - 填寫違規時間: {formatted_dt}")
        self.page.evaluate(f"() => {{ let el = document.querySelector('{self.config['selectors']['violation_date']}'); if(el) {{ el.value = '{formatted_dt}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }} }}")
        self.page.wait_for_timeout(300)

        # 3. 違規地點與補充說明
        district_name = violation_data.get("district", "苗栗市")
        self.select_district(district_name)

        location = violation_data.get("violation_location", "")
        clean_loc = location.replace("苗栗縣", "").replace(district_name, "").strip()
        print(f"  - 填寫違規詳細地點: '{clean_loc}'")
        self.page.fill(self.config["selectors"]["violation_address"], clean_loc)

        remark = violation_data.get("violation_description", "")
        if remark:
            print(f"  - 填寫補充說明: '{remark[:30]}...'")
            self.page.fill(self.config["selectors"]["violation_remark"], remark)

        # 4. 違規項目選單匹配
        subcat = (violation_data.get("violation_subcategory", "") or "") + " " + (violation_data.get("violation_description", "") or "")
        subcat = subcat.strip()
        print(f"  - 搜尋並配對違規項目 (關鍵字: '{subcat}')...")
        
        options = self.page.locator(f"{self.config['selectors']['violation_law']} option").all()
        matched_val = None
        matched_txt = ""

        for opt in options:
            txt = opt.evaluate("el => el.innerText").strip()
            val = opt.evaluate("el => el.value")
            if not val or val == "0":
                continue
                
            if "身障" in subcat or "身心障礙" in subcat:
                if "身心障礙" in txt:
                    matched_val = val
                    matched_txt = txt
                    break
            elif "併排" in subcat:
                if "併排" in txt:
                    matched_val = val
                    matched_txt = txt
                    break
            elif "不依順" in subcat:
                if "不依順" in txt or "逆向" in txt:
                    matched_val = val
                    matched_txt = txt
                    break
            elif "人行道" in subcat and ("臨時" in subcat or "臨停" in subcat):
                if "人行道" in txt and "臨時" in txt:
                    matched_val = val
                    matched_txt = txt
                    break
            elif "人行道" in subcat:
                if "人行道" in txt:
                    matched_val = val
                    matched_txt = txt
                    break
            elif "闖紅燈" in subcat:
                if "闖紅燈" in txt:
                    matched_val = val
                    matched_txt = txt
                    break
            elif "交岔路口" in subcat or "公車站" in subcat or "消防" in subcat:
                if ("交岔路口" in txt or "公車站" in txt) and ("臨時停車" in txt or "停車" in txt):
                    matched_val = val
                    matched_txt = txt
                    break
            elif "人行道" in subcat and "停車" in subcat:
                if "人行道" in txt:
                    matched_val = val
                    matched_txt = txt
                    break
                if "交岔路口" in txt or "公車站" in txt:
                    matched_val = val
                    matched_txt = txt
                    break
            elif "紅線" in subcat or "黃線" in subcat or "臨時停車" in subcat or "停車" in subcat:
                if "禁止" in txt or "停車" in txt or "臨時停車" in txt:
                    matched_val = val
                    matched_txt = txt
                    break

        if not matched_val:
            for opt in options:
                txt = opt.evaluate("el => el.innerText").strip()
                val = opt.evaluate("el => el.value")
                if val and val != "0" and ("停車" in txt or "臨時" in txt):
                    matched_val = val
                    matched_txt = txt
                    break

        if matched_val:
            print(f"    - 成功配對違規項目: '{matched_txt}' (Value: {matched_val})")
            self.page.select_option(self.config["selectors"]["violation_law"], value=matched_val)
        else:
            if len(options) > 1:
                val = options[1].evaluate("el => el.value")
                txt = options[1].evaluate("el => el.innerText").strip()
                self.page.select_option(self.config["selectors"]["violation_law"], value=val)
                print(f"    - 選取預設第二項違規項目: '{txt}'")

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Miaoli] 沒有提供媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[Miaoli] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[Miaoli] 正在上傳附加檔案 (最多 4 個槽): {[os.path.basename(p) for p in valid_paths]}")
        file_inputs = self.config["selectors"]["file_inputs"]
        
        for idx, path in enumerate(valid_paths[:4]):
            if idx < len(file_inputs):
                selector = file_inputs[idx]
                if self.page.is_visible(selector):
                    self.page.set_input_files(selector, path)
                    self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[Miaoli] 正在擷取 BotDetect 圖形驗證碼...")
        captcha_img_selector = self.config["selectors"]["login_captcha_img"]
        self.page.wait_for_selector(captcha_img_selector, state="visible", timeout=8000)
        
        screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/ml_captcha_crop.png"
        self.page.locator(captcha_img_selector).screenshot(path=screenshot_path)
        print(f"[Miaoli] 驗證碼圖片已成功儲存: {screenshot_path}")

        # 聚焦驗證碼輸入框
        self.page.focus(self.config["selectors"]["login_captcha"])
        self.page.wait_for_timeout(500)

        # 呼叫驗證 Hook，觸發人機協作暫停
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[Miaoli] 自動填表完畢，勾選同意條款並儲存預覽截圖...")
        self.page.check(self.config["selectors"]["consent_checkbox"], force=True)
        self.page.wait_for_timeout(500)
        
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/ml_filled_test.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Miaoli] 預覽截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Miaoli] 截圖失敗: {e}")

        self.page.focus(self.config["selectors"]["submit_btn"])
        print("[Miaoli] 系統安全停留在「送出」按鈕前，請使用者輸入驗證碼後手動點擊提交。")
        return False

    # ==================== 輔助方法 ====================

    def _split_license_plate(self, plate: str) -> tuple:
        plate = plate.replace(" ", "").strip()
        if "-" in plate:
            parts = plate.split("-", 1)
            return parts[0], parts[1]
        elif len(plate) >= 6:
            return plate[:3], plate[3:]
        return plate, ""

    def _format_datetime(self, date_str: str, time_str: str) -> str:
        date_str = date_str.replace("/", "-").replace(".", "-").strip()
        if re.match(r"^\d{8}$", date_str):
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            
        time_str = time_str.strip()
        if ":" in time_str:
            h, m = time_str.split(":")
        elif len(time_str) >= 4 and time_str.isdigit():
            h, m = time_str[:2], time_str[2:4]
        else:
            h, m = "00", "00"
            
        return f"{date_str} {int(h):02d}:{int(m):02d}"
