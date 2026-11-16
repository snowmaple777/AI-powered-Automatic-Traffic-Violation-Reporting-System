import os
import sys
import argparse
import json
from playwright.sync_api import sync_playwright

# 解決 Windows 終端機 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 載入設定與 Driver
from drivers.kaohsiung_driver import KaohsiungDriver
from drivers.tainan_driver import TainanDriver
from drivers.pingtung_driver import PingtungDriver
from drivers.yunlin_driver import YunlinDriver
from drivers.chiayi_driver import ChiayiDriver
from drivers.chiayi_city_driver import ChiayiCityDriver
from drivers.taichung_driver import TaichungDriver
from drivers.changhua_driver import ChanghuaDriver
from drivers.nantou_driver import NantouDriver
from drivers.yilan_driver import YilanDriver
from drivers.hualien_driver import HualienDriver
from drivers.taitung_driver import TaitungDriver
from drivers.hsinchu_county_driver import HsinchuCountyDriver
from drivers.hsinchu_city_driver import HsinchuCityDriver
from drivers.penghu_driver import PenghuDriver
from drivers.keelung_driver import KeelungDriver
from drivers.taoyuan_driver import TaoyuanDriver
from drivers.new_taipei_driver import NewTaipeiDriver
from drivers.taipei_driver import TaipeiDriver
from drivers.miaoli_driver import MiaoliDriver
from plugins.human_verification_helper import HumanVerificationHelperPlugin

def get_args():
    parser = argparse.ArgumentParser(description="全台各縣市交通違規檢舉自動填表系統")
    parser.add_argument(
        "--city", 
        type=str, 
        default="kaohsiung", 
        help="欲填表的縣市代碼 (如：kaohsiung)，預設為 kaohsiung"
    )
    parser.add_argument(
        "--case-dir", 
        type=str, 
        default=None, 
        help="案件影像資料夾路徑 (指定後將自動載入 info.txt 與照片/影片)"
    )
    parser.add_argument(
        "--profile", 
        type=str, 
        default="reporter_profile.json", 
        help="檢舉人個人 Profile 設定檔路徑，預設為 reporter_profile.json"
    )
    return parser.parse_args()

def parse_info_txt(file_path: str) -> dict:
    """輔助解析 info.txt 檔案"""
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
    # 1. 若無任何命令列參數，則預設啟動整合主控制台 GUI
    if len(sys.argv) == 1:
        print("[Main] 未偵測到命令列參數。自動啟動系統整合控制台...")
        try:
            import app_gui
            app_gui.run_gui()
            sys.exit(0)
        except Exception as e:
            print(f"[Main 錯誤] 無法啟動整合控制台: {e}")
            sys.exit(1)

    args = get_args()
    
    print("="*60)
    print("  全台縣市交通違規檢舉自動填表系統 - 自動化核心啟動")
    print("="*60)
    
    # 2. 載入檢舉人個人資料 (Profile)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    profile_path = os.path.abspath(args.profile)
    if not os.path.exists(profile_path):
        print(f"[Main 錯誤] 找不到檢舉人設定檔: {profile_path}。請點選 GUI 視窗「修改資料」來建立它。")
        sys.exit(1)
        
    with open(profile_path, "r", encoding="utf-8") as f:
        profile_data = json.load(f)
    print(f"[Main] 成功載入檢舉人: {profile_data.get('reporter_name')}")

    # 3. 載入案件資料與影像
    if not args.case_dir:
        print("[Main 錯誤] 命令行模式下必須提供 --case-dir 指定案件資料夾路徑！")
        sys.exit(1)
        
    case_dir = os.path.abspath(args.case_dir)
    info_path = os.path.join(case_dir, "info.txt")
    if not os.path.exists(info_path):
        print(f"[Main 錯誤] 找不到指定案件之資訊檔 info.txt: {info_path}")
        sys.exit(1)
        
    case_raw = parse_info_txt(info_path)
    print(f"[Main] 成功載入案件資料，車牌: {case_raw.get('車牌號碼')}")

    # 整合欄位轉換為中介欄位
    input_data = {
        # 檢舉人資料
        "reporter_name": profile_data.get("reporter_name"),
        "reporter_id": profile_data.get("reporter_id"),
        "reporter_phone": profile_data.get("reporter_phone"),
        "reporter_email": profile_data.get("reporter_email"),
        "reporter_address": profile_data.get("reporter_address"),
        
        # 案件資料
        "violation_date": case_raw.get("違規日期"),
        "violation_time": case_raw.get("違規時間"),
        "district": case_raw.get("違規行政區") or case_raw.get("交通違規地點行政區") or case_raw.get("違規行政區(必填)"),
        "violation_location": case_raw.get("交通違規地點"),
        "license_plate": case_raw.get("車牌號碼"),
        "car_type": case_raw.get("違規車種類型"),
        "violation_category": case_raw.get("違規類別"),
        "violation_subcategory": case_raw.get("違規事實"),
        "violation_description": case_raw.get("違規事實敘述"),
    }

    # 尋找案件影像檔
    media_files = []
    for file in os.listdir(case_dir):
        if file.lower().endswith((".jpg", ".jpeg", ".png", ".mp4")):
            media_files.append(os.path.join(case_dir, file))
    print(f"[Main] 找到佐證影像: {[os.path.basename(m) for m in media_files]}")

    # 4. 判斷縣市驅動策略
    city_code = args.city.lower()
    driver_class = KaohsiungDriver
    
    if city_code == "kaohsiung":
        driver_class = KaohsiungDriver
    elif city_code == "tainan":
        driver_class = TainanDriver
    elif city_code == "pingtung":
        driver_class = PingtungDriver
    elif city_code == "yunlin":
        driver_class = YunlinDriver
    elif city_code == "chiayi":
        driver_class = ChiayiDriver
    elif city_code == "chiayi_city":
        driver_class = ChiayiCityDriver
    elif city_code == "taichung":
        driver_class = TaichungDriver
    elif city_code == "changhua":
        driver_class = ChanghuaDriver
    elif city_code == "nantou":
        driver_class = NantouDriver
    elif city_code == "yilan":
        driver_class = YilanDriver
    elif city_code == "hualien":
        driver_class = HualienDriver
    elif city_code == "taitung":
        driver_class = TaitungDriver
    elif city_code == "hsinchu_county":
        driver_class = HsinchuCountyDriver
    elif city_code == "hsinchu_city":
        driver_class = HsinchuCityDriver
    elif city_code == "penghu":
        driver_class = PenghuDriver
    elif city_code == "keelung":
        driver_class = KeelungDriver
    elif city_code == "taoyuan":
        driver_class = TaoyuanDriver
    elif city_code == "new_taipei":
        driver_class = NewTaipeiDriver
    elif city_code == "taipei":
        driver_class = TaipeiDriver
    elif city_code == "miaoli":
        driver_class = MiaoliDriver
    else:
        print(f"[Main 警告] 目前尚未實作 [{args.city}] 的專屬驅動策略。將降級並以 [高雄市] 進行表單填寫展示。")
        city_code = "kaohsiung"
        driver_class = KaohsiungDriver
        
    config_path = os.path.join(base_dir, "configs", f"{city_code}.json")

    # 5. 啟動 Playwright 表單填寫
    print(f"[Main] 啟動瀏覽器並載入策略: {city_code.upper()}...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            
            # 實例化策略 Driver
            driver = driver_class(
                city_code=city_code,
                config_path=config_path,
                context=context,
                page=page
            )
            
            # 註冊人機協作驗證插件
            verify_plugin = HumanVerificationHelperPlugin(mode="auto")
            driver.register_plugin(verify_plugin)
            
            # 執行完整填表流程
            try:
                success = driver.run_workflow(input_data, media_files)
                if success:
                    print("\n[🎉 系統提示] 交通檢舉表單填寫已成功跑完！瀏覽器視窗將保持開啟，當您手動關閉瀏覽器後程式才會結束。")
                else:
                    print("\n[⚠️ 系統警告] 填表流程已結束，但未偵測到成功的確認標記，請手動確認網頁狀態。")
            except Exception as inner_e:
                print(f"\n[❌ 執行中斷] 填表流程遭遇錯誤: {inner_e}")
                print("瀏覽器視窗將保持開啟以供檢查。")
                
            # 保持瀏覽器開啟，直到使用者手動關閉
            try:
                if not page.is_closed():
                    page.wait_for_event("close", timeout=0)
            except Exception:
                pass
                
            try:
                browser.close()
            except Exception:
                pass
            
    except Exception as e:
        print(f"\n[❌ 啟動中斷] 瀏覽器啟動或主程序遭遇錯誤: {e}")
        sys.exit(1)
        
    print("="*60)
    print("  系統已安全結束執行。")
    print("="*60)

if __name__ == "__main__":
    main()
