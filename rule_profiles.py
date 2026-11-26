"""規則設定組合（4by）：一次切換闖紅燈、區域、雙線三份規則設定檔。

- default：configs/*.json（現行預設）。
- high_recall：configs/high_recall/*.json＝預設＋三個召回選項。三項都只產生疑似事件：
  - 停止線有限延伸 1 倍車寬（4bk）；
  - 停等區貼邊車輛佔用 0.8（4bl）；
  - 雙線過寬元件逐列截除（4bg）。

  用在長片切段、串流篩選等之後會再人工看片段的場合。漏報較少，誤報也較多，數字見 docs/refactor_log.md 4by。

明確指定的 --area-rule-config／--red-light-config／--double-line-config 優先於組合。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_ATTRS = {"area_rule_config": "area_rules.json", "red_light_config": "red_light_rules.json",
                "double_line_config": "double_line_rules.json"}
PROFILES = {"default": ROOT / "configs", "high_recall": ROOT / "configs" / "high_recall"}


def add_profile_argument(parser):
    parser.add_argument("--rule-profile", choices=sorted(PROFILES), default="default",
                        help="規則設定組合：default＝現行預設；high_recall＝加上三個只產生疑似的召回選項"
                             "（長片切段、串流篩選用，誤報較多）。明確指定的 --*-config 優先")


def apply_profile(args):
    """把仍是預設路徑的三個規則設定檔換成所選組合；使用者明確指定的檔案不動。"""
    profile = getattr(args, "rule_profile", "default") or "default"
    folder = PROFILES[profile]
    for attr, name in CONFIG_ATTRS.items():
        current = getattr(args, attr, None)
        if current is None or Path(current) == PROFILES["default"] / name:
            setattr(args, attr, folder / name)
    return args
