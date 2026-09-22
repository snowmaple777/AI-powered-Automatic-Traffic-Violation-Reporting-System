import os
import sys

# 解決 Windows 終端機可能發生的 UnicodeEncodeError (例如 cp932/cp950 等非 UTF-8 語系環境)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import json
import uuid
import shutil
import argparse
from playwright.sync_api import sync_playwright

def get_args():
    parser = argparse.ArgumentParser(description="網頁表單欄位探測工具 - 擷取 <input>、<select> 與 <textarea> 屬性")
    parser.add_argument(
        "--url", 
        type=str, 
        default=None, 
        help="目標網址 (選填，若未指定將於啟動後提示輸入)"
    )
    parser.add_argument(
        "--output", 
        type=str, 
        default="debug.json", 
        help="輸出 JSON 檔案路徑，預設為 debug.json"
    )
    parser.add_argument(
        "--headless", 
        action="store_true", 
        help="以無頭模式 (Headless) 執行瀏覽器 (預設為有頭模式，方便使用者手動操作/登入)"
    )
    return parser.parse_args()

def capture_page(browser_context, page, output_file, js_extract_script):
    print("[系統] 開始探測表單欄位...")
    
    # 更新 page 對象（以防開了新分頁，我們抓取最後一個被啟動或當前的 page）
    current_page = page
    # 如果有其他分頁，使用最後一個分頁
    if len(browser_context.pages) > 1:
        current_page = browser_context.pages[-1]
        print(f"[系統] 偵測到多個分頁，將擷取最新分頁: {current_page.url}")
    
    all_elements = []
    
    # 遍歷頁面中的所有 frames (支援 iframe 探測)
    frames = current_page.frames
    print(f"[系統] 偵測到 {len(frames)} 個 Frame (包含主頁面與 iframe)。開始掃描...")
    
    for idx, frame in enumerate(frames):
        try:
            frame_elements = frame.evaluate(js_extract_script)
            if frame_elements:
                # 標註 frame 資訊
                for el in frame_elements:
                    el['frame_index'] = idx
                    el['frame_url'] = frame.url
                    el['frame_name'] = frame.name or ""
                all_elements.extend(frame_elements)
        except Exception:
            # 忽略跨網域 iframe 造成的權限問題或載入失敗 of frame
            pass
    
    # 整理統計
    inputs = [el for el in all_elements if el['tag'] == 'input']
    selects = [el for el in all_elements if el['tag'] == 'select']
    textareas = [el for el in all_elements if el['tag'] == 'textarea']
    
    print(f"\n[結果] 成功探測到：")
    print(f"       - <input>    共 {len(inputs)} 個")
    print(f"       - <select>   共 {len(selects)} 個")
    print(f"       - <textarea> 共 {len(textareas)} 個")
    print(f"       總共：{len(all_elements)} 個表單控制項。")
    
    # 確保輸出路徑的目錄存在
    output_dir = os.path.dirname(os.path.abspath(output_file))
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        
    # 將結果寫入檔案
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_elements, f, indent=2, ensure_ascii=False)
        print(f"[成功] 欄位屬性已寫入至: {os.path.abspath(output_file)}")
    except Exception as we:
        print(f"[錯誤] 無法寫入 JSON 檔案: {we}")
    
    print("-" * 60)

def main():
    args = get_args()
    
    print("="*60)
    print("  網頁表單欄位探測工具 (Form Element Inspector)")
    print("="*60)
    
    url = args.url
    if not url:
        url = input("請輸入欲探測的網頁網址 (URL): ").strip()
        if not url:
            print("[錯誤] 未輸入網址，程式結束。")
            sys.exit(1)
            
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
        
    print(f"\n[系統] 即將啟動瀏覽器並前往：{url}")
    print("[提示] 建議使用有頭模式 (預設)。若頁面需要點擊「同意條款」、「下一步」或進行登入，")
    print("       請先在瀏覽器視窗中操作，直到看見目標表單，再回終端機按 [Enter] 進行擷取。")
    print("-" * 60)
    
    # 建立臨時的 Chrome profile 以免被其他 Chrome 進程佔用
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)
    temp_profile = os.path.join(project_root, f"temp_inspector_profile_{uuid.uuid4().hex[:8]}")
    
    # JavaScript 擷取腳本
    js_extract_script = r"""
    () => {
        const elements = document.querySelectorAll('input, select, textarea');
        const data = [];
        elements.forEach((el, index) => {
            const tagName = el.tagName.toLowerCase();
            
            // 產生 CSS 選擇器
            let selector = '';
            if (el.id) {
                selector = `#${el.id}`;
            } else if (el.name) {
                const query = `${tagName}[name="${el.name}"]`;
                try {
                    if (document.querySelectorAll(query).length === 1) {
                        selector = query;
                    }
                } catch (e) {}
            }
            
            if (!selector) {
                // 路徑遞迴生成
                const path = [];
                let current = el;
                while (current && current.nodeType === Node.ELEMENT_NODE) {
                    let tag = current.tagName.toLowerCase();
                    if (current.id) {
                        tag += `#${current.id}`;
                        path.unshift(tag);
                        break;
                    } else {
                        let sib = current, nth = 1;
                        while (sib = sib.previousElementSibling) {
                            if (sib.tagName === current.tagName) nth++;
                        }
                        if (nth > 1) {
                            tag += `:nth-of-type(${nth})`;
                        }
                    }
                    path.unshift(tag);
                    current = current.parentNode;
                }
                selector = path.join(' > ');
            }

            const info = {
                index: index + 1,
                tag: tagName,
                selector: selector,
                id: el.id || null,
                name: el.name || null,
                type: el.type || null,
                placeholder: el.placeholder || null,
                value: el.value || '',
                class: el.className || null,
                required: el.required || false,
                disabled: el.disabled || false,
                title: el.title || null
            };
            
            // 擷取 Label / 上下文文字
            let labelText = '';
            if (el.id) {
                const label = document.querySelector(`label[for="${el.id}"]`);
                if (label) {
                    labelText = label.innerText.trim();
                }
            }
            if (!labelText) {
                const parentLabel = el.closest('label');
                if (parentLabel) {
                    labelText = parentLabel.innerText.replace(el.innerText || '', '').trim();
                }
            }
            if (!labelText) {
                const parent = el.parentElement;
                if (parent) {
                    labelText = parent.innerText.trim().replace(/\s+/g, ' ');
                    if (labelText.length > 150) {
                        labelText = labelText.substring(0, 150) + '...';
                    }
                }
            }
            info.label = labelText || null;

            // 特定標籤的額外資訊
            if (tagName === 'select') {
                info.options = Array.from(el.options).map(opt => ({
                    value: opt.value,
                    text: opt.text.trim()
                }));
            } else if (tagName === 'input') {
                if (el.type === 'checkbox' || el.type === 'radio') {
                    info.checked = el.checked;
                }
            }
            
            data.push(info);
        });
        return data;
    }
    """

    playwright = sync_playwright().start()
    browser_context = None
    
    try:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars"
        ]
        
        print("[系統] 正在啟動 Chrome 瀏覽器...")
        browser_context = playwright.chromium.launch_persistent_context(
            user_data_dir=temp_profile,
            headless=args.headless,
            args=launch_args,
            slow_mo=100,
            ignore_default_args=["--enable-automation"]
        )
        
        # 偽裝 webdriver
        browser_context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        
        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        
        print(f"[系統] 正在導向至：{url}")
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[警告] 頁面載入可能超時或未完全載入: {e}")
            
        print("\n" + "="*80)
        print(" 【探測就緒】")
        print(f" 瀏覽器目前 URL: {page.url}")
        print(" 請在瀏覽器視窗中操作到您需要擷取的表單頁面。")
        print("="*80 + "\n")
        
        if args.headless:
            print("[系統] 無頭模式偵測：將自動擷取一次表單欄位後結束。")
            capture_page(browser_context, page, args.output, js_extract_script)
        else:
            while True:
                try:
                    cmd = input(">>> 按 [Enter] 開始擷取當前頁面表單，或輸入 'q' 關閉並結束：").strip()
                except EOFError:
                    print("\n[系統] 偵測到輸入流結束 (EOF)，開始進行最後擷取並結束程式。")
                    capture_page(browser_context, page, args.output, js_extract_script)
                    break
                if cmd.lower() == 'q':
                    break
                capture_page(browser_context, page, args.output, js_extract_script)
            
    except Exception as e:
        print(f"\n[錯誤] 執行過程中遭遇異常: {e}")
        
    finally:
        print("[系統] 正在關閉瀏覽器與清理臨時檔案...")
        if browser_context:
            try:
                browser_context.close()
            except Exception:
                pass
        playwright.stop()
        
        # 清除臨時的 Chrome profile 資料夾
        if os.path.exists(temp_profile):
            for _ in range(5):
                try:
                    shutil.rmtree(temp_profile)
                    break
                except Exception:
                    import time
                    time.sleep(0.5)
                    
        print("="*60)
        print("  探測工作已結束。")
        print("="*60)

if __name__ == "__main__":
    main()
