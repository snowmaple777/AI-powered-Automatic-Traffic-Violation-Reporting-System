import os
import json
from pathlib import Path
from collections import Counter
import shutil

# ============================================================
# 設定
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

BDD_ROOT = PROJECT_ROOT / "archive" / "bdd100k"

IMAGE_ROOT = BDD_ROOT / "images" / "100k"
LABEL_ROOT = BDD_ROOT / "labels"

OUTPUT_ROOT = PROJECT_ROOT / "traffic_light_dataset"

# YOLO 類別
CLASS_MAP = {
    "red": 0,
    "green": 1,
    "yellow": 2,
}

# 是否保留「沒有有效紅綠燈」的圖片作為負樣本
# True = 保留
# False = 只輸出包含 red/green/yellow 的圖片
KEEP_NEGATIVE_IMAGES = True

# 每幾張 negative image 留一張
# 例如 5 代表每 5 張負樣本保留 1 張
# 避免 background 圖片數量太誇張
NEGATIVE_SAMPLE_RATE = 5

# 盡量使用 hard link，避免再複製數 GB 圖片
USE_HARDLINK = True


# ============================================================
# 工具函式
# ============================================================

def ensure_directories():
    for split in ["train", "val"]:
        (OUTPUT_ROOT / "images" / split).mkdir(parents=True, exist_ok=True)
        (OUTPUT_ROOT / "labels" / split).mkdir(parents=True, exist_ok=True)


def copy_or_link(src: Path, dst: Path):
    """
    優先使用 hard link，避免圖片再佔一份硬碟空間。
    如果 hard link 失敗，再改成正常複製。
    """

    if dst.exists():
        return

    if USE_HARDLINK:
        try:
            os.link(src, dst)
            return
        except Exception:
            pass

    shutil.copy2(src, dst)


def get_image_size(image_path: Path):
    """
    BDD100K 圖片通常是 1280x720。
    為了避免每張都用 OpenCV 讀圖，
    目前直接採 BDD100K 原始解析度。

    如果之後發現有例外圖片，再改為實際讀取。
    """
    return 1280, 720


def convert_box_to_yolo(box, image_width, image_height):
    """
    BDD:
        x1, y1, x2, y2

    YOLO:
        x_center, y_center, width, height

    全部正規化到 0~1。
    """

    x1 = float(box["x1"])
    y1 = float(box["y1"])
    x2 = float(box["x2"])
    y2 = float(box["y2"])

    # 修正座標，避免超出圖片
    x1 = max(0.0, min(x1, image_width))
    y1 = max(0.0, min(y1, image_height))
    x2 = max(0.0, min(x2, image_width))
    y2 = max(0.0, min(y2, image_height))

    box_width = x2 - x1
    box_height = y2 - y1

    if box_width <= 0 or box_height <= 0:
        return None

    x_center = (x1 + x2) / 2.0
    y_center = (y1 + y2) / 2.0

    x_center /= image_width
    y_center /= image_height
    box_width /= image_width
    box_height /= image_height

    return x_center, y_center, box_width, box_height


def extract_objects(json_data):
    """
    不同 mirror 的 JSON 最外層可能略有不同，
    這裡盡量自動處理。

    可能是：
        [ {...}, {...} ]

    或：
        {
            "labels": [...]
        }

    或其他包裝格式。
    """

    if isinstance(json_data, list):
        return json_data

    if isinstance(json_data, dict):
        if "labels" in json_data and isinstance(json_data["labels"], list):
            return json_data["labels"]

        if "objects" in json_data and isinstance(json_data["objects"], list):
            return json_data["objects"]

        if "frames" in json_data and isinstance(json_data["frames"], list):
            objects = []

            for frame in json_data["frames"]:
                if isinstance(frame, dict):
                    if "objects" in frame:
                        objects.extend(frame["objects"])

                    if "labels" in frame:
                        objects.extend(frame["labels"])

            return objects

    return []


# ============================================================
# 轉換單一 split
# ============================================================

def convert_split(split):
    image_dir = IMAGE_ROOT / split
    json_dir = LABEL_ROOT / split

    output_image_dir = OUTPUT_ROOT / "images" / split
    output_label_dir = OUTPUT_ROOT / "labels" / split

    print()
    print("=" * 70)
    print(f"開始處理：{split}")
    print("=" * 70)

    if not image_dir.exists():
        raise FileNotFoundError(f"找不到圖片資料夾：{image_dir}")

    if not json_dir.exists():
        raise FileNotFoundError(f"找不到標籤資料夾：{json_dir}")

    json_files = sorted(json_dir.glob("*.json"))

    print(f"找到 JSON：{len(json_files):,} 個")

    class_counter = Counter()

    total_images = 0
    positive_images = 0
    negative_images = 0
    kept_negative_images = 0
    missing_images = 0
    invalid_json = 0
    invalid_boxes = 0
    ignored_none = 0
    ignored_other_class = 0

    negative_index = 0

    for index, json_path in enumerate(json_files, start=1):

        image_path = image_dir / (json_path.stem + ".jpg")

        if not image_path.exists():
            missing_images += 1
            continue

        total_images += 1

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        except Exception as e:
            invalid_json += 1
            print(f"[JSON 錯誤] {json_path.name}: {e}")
            continue

        objects = extract_objects(data)

        image_width, image_height = get_image_size(image_path)

        yolo_lines = []

        for obj in objects:

            if not isinstance(obj, dict):
                continue

            category = obj.get("category", "")

            # 我們只要交通號誌
            if category != "traffic light":
                ignored_other_class += 1
                continue

            attributes = obj.get("attributes", {})

            color = attributes.get("trafficLightColor", "none")

            # 處理大小寫 / 空白
            if isinstance(color, str):
                color = color.strip().lower()
            else:
                color = "none"

            # none / unknown 等全部不要
            if color not in CLASS_MAP:
                ignored_none += 1
                continue

            box = obj.get("box2d")

            if not isinstance(box, dict):
                invalid_boxes += 1
                continue

            required_keys = ["x1", "y1", "x2", "y2"]

            if not all(key in box for key in required_keys):
                invalid_boxes += 1
                continue

            converted = convert_box_to_yolo(
                box,
                image_width,
                image_height
            )

            if converted is None:
                invalid_boxes += 1
                continue

            x, y, w, h = converted

            class_id = CLASS_MAP[color]

            yolo_lines.append(
                f"{class_id} "
                f"{x:.6f} "
                f"{y:.6f} "
                f"{w:.6f} "
                f"{h:.6f}"
            )

            class_counter[color] += 1

        # ====================================================
        # Positive image
        # ====================================================

        if len(yolo_lines) > 0:

            positive_images += 1

            output_image_path = output_image_dir / image_path.name

            output_txt_path = (
                output_label_dir /
                (image_path.stem + ".txt")
            )

            copy_or_link(
                image_path,
                output_image_path
            )

            with open(
                output_txt_path,
                "w",
                encoding="utf-8"
            ) as f:

                f.write("\n".join(yolo_lines))

        # ====================================================
        # Negative image
        # ====================================================

        else:

            negative_images += 1
            negative_index += 1

            if (
                KEEP_NEGATIVE_IMAGES
                and negative_index % NEGATIVE_SAMPLE_RATE == 0
            ):

                kept_negative_images += 1

                output_image_path = (
                    output_image_dir /
                    image_path.name
                )

                output_txt_path = (
                    output_label_dir /
                    (image_path.stem + ".txt")
                )

                copy_or_link(
                    image_path,
                    output_image_path
                )

                # YOLO 負樣本：
                # txt 存在但內容為空
                output_txt_path.touch()

        # 顯示進度
        if index % 5000 == 0:

            print(
                f"[{split}] "
                f"{index:,}/{len(json_files):,} JSON 已處理"
            )

    print()
    print(f"{split} 完成")
    print("-" * 50)

    print(f"原始圖片：             {total_images:,}")
    print(f"包含有效紅綠燈：       {positive_images:,}")
    print(f"沒有有效紅綠燈：       {negative_images:,}")
    print(f"實際保留負樣本：       {kept_negative_images:,}")

    print()
    print("Traffic light annotation：")

    print(
        f"  red:       "
        f"{class_counter['red']:,}"
    )

    print(
        f"  green:     "
        f"{class_counter['green']:,}"
    )

    print(
        f"  yellow:    "
        f"{class_counter['yellow']:,}"
    )

    print()

    print(f"忽略 none/其他顏色：   {ignored_none:,}")
    print(f"無效 box：             {invalid_boxes:,}")
    print(f"找不到圖片：           {missing_images:,}")
    print(f"JSON 解析失敗：        {invalid_json:,}")

    return {
        "class_counter": class_counter,
        "total_images": total_images,
        "positive_images": positive_images,
        "negative_images": negative_images,
        "kept_negative_images": kept_negative_images,
    }


# ============================================================
# 建立 YAML
# ============================================================

def create_yaml():

    yaml_path = OUTPUT_ROOT / "traffic_light.yaml"

    # YOLO 在 Windows 下使用 / 比 \ 穩定
    dataset_path = OUTPUT_ROOT.as_posix()

    yaml_text = f"""path: "{dataset_path}"

train: images/train
val: images/val

names:
  0: red
  1: green
  2: yellow
"""

    with open(
        yaml_path,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(yaml_text)

    print()
    print("建立 YAML：")
    print(yaml_path)


# ============================================================
# main
# ============================================================

def main():

    print("=" * 70)
    print("BDD100K -> YOLO Traffic Light Dataset")
    print("=" * 70)

    print()
    print("BDD100K：")
    print(BDD_ROOT)

    print()
    print("輸出：")
    print(OUTPUT_ROOT)

    ensure_directories()

    train_result = convert_split("train")
    val_result = convert_split("val")

    create_yaml()

    total_counter = (
        train_result["class_counter"]
        + val_result["class_counter"]
    )

    print()
    print("=" * 70)
    print("全部完成")
    print("=" * 70)

    print()
    print("Train + Val Traffic Light 數量：")

    print(
        f"RED:     "
        f"{total_counter['red']:,}"
    )

    print(
        f"GREEN:   "
        f"{total_counter['green']:,}"
    )

    print(
        f"YELLOW:  "
        f"{total_counter['yellow']:,}"
    )

    print()

    print(
        "TOTAL:   "
        f"{sum(total_counter.values()):,}"
    )

    print()
    print("YOLO Dataset：")
    print(OUTPUT_ROOT)

    print()
    print("下一步不要立刻訓練。")
    print("請先檢查 bounding box 是否正確。")


if __name__ == "__main__":
    main()