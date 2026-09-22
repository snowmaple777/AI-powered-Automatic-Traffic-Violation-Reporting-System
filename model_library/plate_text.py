"""Plate normalization heuristics migrated from perception-v2; not legal validation."""
import re

class TaiwanPlateValidator:
    """
    台灣車牌語法校驗與字元消歧義修復器 (Taiwan License Plate Syntax Validator & Disambiguation)
    依據交通部公路局法規與公路監理號牌編碼規範：
    1. 支援主流 8 代新式汽機車 (3 英文 - 4 數字，如 ABC-1234, BXH-6208)
    2. 支援 8 代 / 7 代機車 (3 英文 - 3 數字，如 MAY-123, AAA-001)
    3. 支援 7 代舊式汽車 (前 2 代字 - 後 4 數字，如 AB-1234, 2R-1234, 22-1234)
    4. 支援 7 代舊式汽車反向 (前 4 數字 - 後 2 代字，如 1234-AB, 0001-DA, 0001-A2)
    5. 支援 7 代舊式營業車/計程車 (2 代字 - 3 數字 或 3 數字 - 2 代字，如 CA-001, 001-CA)
    6. 法規禁用字元約束：台灣號牌英文字軌全面禁用 'I' 與 'O'，徹底解決 1/I 與 0/O 混淆
    7. 字元位置先驗 (Positional Priors)：數字區段出現字母時進行確定性映射修復 (O/D->0, B->8, S->5, Z->2, I/L->1)
    8. 缺少連字號智慧還原：自動識別長度並插入 '-'
    """
    DIGIT_FIX = {
        'O': '0', 'D': '0', 'Q': '0',
        'I': '1', 'L': '1', 'J': '1',
        'Z': '2',
        'S': '5',
        'B': '8',
        'G': '6',
    }

    LETTER_FIX = {
        '8': 'B',
        '2': 'Z',
        '5': 'S',
        '0': 'D',
    }

    @classmethod
    def fix_digits(cls, s):
        return ''.join(cls.DIGIT_FIX.get(c, c) for c in s)

    @classmethod
    def fix_letters(cls, s):
        return ''.join(cls.LETTER_FIX.get(c, c) for c in s)

    @classmethod
    def validate_and_normalize(cls, raw_text):
        if not raw_text:
            return None
        # 轉大寫，並將常見分隔符號統一轉換為 '-'
        s = raw_text.upper().strip()
        s = re.sub(r'[\s·・._:—–]+', '-', s)
        s = re.sub(r'[^A-Z0-9-]', '', s).strip('-')
        if len(s) < 4:
            return None

        def check_candidate(cand):
            if not cand or '-' not in cand:
                return None
            parts = cand.split('-')
            if len(parts) != 2:
                return None
            p1, p2 = parts[0], parts[1]

            # 1. 第八代新式汽車 / 重機 / 白牌機車 (最主流 7 碼: 3 英文 - 4 數字，例: ABC-1234, BXH-6208)
            if len(p1) == 3 and len(p2) == 4:
                p1_c = cls.fix_letters(p1)
                p2_c = cls.fix_digits(p2)
                c = f"{p1_c}-{p2_c}"
                # 台灣字母無 I, O，此處嚴格檢查
                if re.match(r'^[A-HJ-NP-Z]{3}-[0-9]{4}$', c):
                    return c

            # 2. 第八代/第七代機車 (6 碼: 3 英文 - 3 數字，例: MAY-123, AAA-001)
            if len(p1) == 3 and len(p2) == 3:
                p1_c = cls.fix_letters(p1)
                p2_c = cls.fix_digits(p2)
                c = f"{p1_c}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z]{3}-[0-9]{3}$', c):
                    return c

            # 3. 第七代舊式汽車 (6 碼: 前 2 碼代字 - 後 4 碼數字，例: AB-1234, 2R-1234)
            if len(p1) == 2 and len(p2) == 4:
                p2_c = cls.fix_digits(p2)
                c = f"{p1}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z0-9]{2}-[0-9]{4}$', c):
                    if re.search(r'[A-HJ-NP-Z]', p1) or p1 in ('22', '88', '66', '99'):
                        return c

            # 4. 第七代舊式汽車反向 (6 碼: 前 4 碼數字 - 後 2 碼代字，例: 1234-AB, 0001-DA, 0001-A2)
            if len(p1) == 4 and len(p2) == 2:
                p1_c = cls.fix_digits(p1)
                c = f"{p1_c}-{p2}"
                if re.match(r'^[0-9]{4}-[A-HJ-NP-Z0-9]{2}$', c):
                    if re.search(r'[A-HJ-NP-Z]', p2) or p2 in ('22', '88', '66', '99'):
                        return c

            # 5. 第七代舊式營業車 / 計程車 (5 碼: 2 碼代字 - 3 碼數字，例: CA-001, 2A-001)
            if len(p1) == 2 and len(p2) == 3:
                p2_c = cls.fix_digits(p2)
                c = f"{p1}-{p2_c}"
                if re.match(r'^[A-HJ-NP-Z0-9]{2}-[0-9]{3}$', c) and re.search(r'[A-HJ-NP-Z]', p1):
                    return c

            # 6. 第七代舊式營業車反向 (5 碼: 3 碼數字 - 2 碼代字，例: 001-CA, 001-2A)
            if len(p1) == 3 and len(p2) == 2:
                p1_c = cls.fix_digits(p1)
                c = f"{p1_c}-{p2}"
                if re.match(r'^[0-9]{3}-[A-HJ-NP-Z0-9]{2}$', c) and re.search(r'[A-HJ-NP-Z]', p2):
                    return c

            return None

        # 情境一：字串本身已有連字號 (或由空格、句點等符號轉換而來)
        if '-' in s:
            valid = check_candidate(s)
            if valid:
                return valid

        # 情境二：字串缺少連字號，按台灣號牌長度與字元先驗自動切割修復
        pure = s.replace('-', '')
        if len(pure) == 7:
            cand = f"{pure[:3]}-{pure[3:]}"
            valid = check_candidate(cand)
            if valid:
                return valid
        elif len(pure) == 6:
            # 優先嘗試 3-3 (機車)
            valid = check_candidate(f"{pure[:3]}-{pure[3:]}")
            if valid:
                return valid
            # 次嘗試 2-4 (7代汽車)
            valid = check_candidate(f"{pure[:2]}-{pure[2:]}")
            if valid:
                return valid
            # 次嘗試 4-2 (7代汽車反向)
            valid = check_candidate(f"{pure[:4]}-{pure[4:]}")
            if valid:
                return valid
        elif len(pure) == 5:
            valid = check_candidate(f"{pure[:2]}-{pure[2:]}")
            if valid:
                return valid
            valid = check_candidate(f"{pure[:3]}-{pure[3:]}")
            if valid:
                return valid

        return None
