import os
import sys
import json

# 解決 Windows 終端機可能發生的 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright

# 確保載入專案底下的 modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from drivers.kaohsiung_driver import KaohsiungDriver
from plugins.human_verification_helper import HumanVerificationHelperPlugin

def parse_info_txt(file_path: str) -> dict:
    """解析 info.txt 檔案"""
    data = {}
    if not os.path.exists(file_path):
        return data
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, val = line.split(":", 1)
            data[key.strip()] = val.strip()
    return data

def main():
    print("="*60)
    print("  高雄市交通檢舉自動填表測試 (無害測試模式)")
    print("="*60)

    # 1. 載入檢舉人資料
    base_dir = os.path.dirname(os.path.abspath(__file__))
    profile_path = os.path.join(base_dir, "reporter_profile.json")
    if not os.path.exists(profile_path):
        print(f"[錯誤] 找不到檢舉人設定檔: {profile_path}")
        return
        
    with open(profile_path, "r", encoding="utf-8") as f:
        reporter_data = json.load(f)
    print(f"[測試] 成功載入檢舉人: {reporter_data.get('reporter_name')}")

    # 2. 載入案件資料
    case_dir = os.path.join(base_dir, "cases", "case_20260710_001")
    info_path = os.path.join(case_dir, "info.txt")
    if not os.path.exists(info_path):
        print(f"[錯誤] 找不到案件 info.txt: {info_path}")
        return
        
    case_raw = parse_info_txt(info_path)
    print(f"[測試] 成功載入案件資料，車牌: {case_raw.get('車牌號碼')}")

    # 整合欄位轉換為中介欄位
    input_data = {
        # 檢舉人資料
        "reporter_name": reporter_data.get("reporter_name"),
        "reporter_id": reporter_data.get("reporter_id"),
        "reporter_phone": reporter_data.get("reporter_phone"),
        "reporter_email": reporter_data.get("reporter_email"),
        "reporter_address": reporter_data.get("reporter_address"),
        
        # 案件資料
        "violation_date": case_raw.get("違規日期"),
        "violation_time": case_raw.get("違規時間"),
        "district": case_raw.get("違規行政區"),
        "violation_location": case_raw.get("交通違規地點"),
        "license_plate": case_raw.get("車牌號碼"),
        "car_type": case_raw.get("違規車種類型"),
        "violation_category": case_raw.get("違規類別"),
        "violation_subcategory": case_raw.get("違規事實"),
        "violation_description": case_raw.get("違規事實敘述"),
    }

    # 尋找佐證照片
    media_files = []
    for file in os.listdir(case_dir):
        if file.lower().endswith((".jpg", ".jpeg", ".png")):
            media_files.append(os.path.join(case_dir, file))
    print(f"[測試] 找到佐證照片: {media_files}")

    # 3. 啟動 Playwright 並執行 Driver
    print("[測試] 啟動瀏覽器中 (有頭模式，以便檢視填寫細節)...")
    with sync_playwright() as p:
        # 有頭模式執行
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        
        # 實例化高雄市 Driver
        driver = KaohsiungDriver(
            city_code="kaohsiung",
            config_path=os.path.join(base_dir, "configs", "kaohsiung.json"),
            context=context,
            page=page
        )
        
        # 安全機制：覆寫提交方法，避免在測試中實際將資料送出給警察局
        def mock_submit():
            print("\n" + "*"*80)
            print(" 【無害測試攔截成功】")
            print(" 系統已自動填妥所有資料、選擇完行政區與車種、並上傳了佐證照片。")
            print(" 因處於測試模式，系統不會點擊最後的送出按鈕以防止送出假檢舉信。")
            print("*"*80 + "\n")
            return True
            
        driver.submit = mock_submit
        
        # 註冊人機協作驗證插件 (使用手動確認模式)
        verify_plugin = HumanVerificationHelperPlugin(mode="signal")
        driver.register_plugin(verify_plugin)
        
        # 執行完整工作流
        print("[測試] 開始執行自動填表流程...")
        success = driver.run_workflow(input_data, media_files)
        
        if success:
            print("[🎉 測試成功] 高雄市填表工作流完整跑完！")
        else:
            print("[⚠️ 測試失敗] 高雄市填表工作流未正常結束。")
            
        browser.close()

if __name__ == "__main__":
    main()
