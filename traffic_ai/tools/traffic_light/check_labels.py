import random
from pathlib import Path

import cv2
import numpy as np


# ============================================================
# 設定
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

DATASET_ROOT = PROJECT_ROOT / "traffic_light_dataset"

IMAGE_DIR = DATASET_ROOT / "images" / "val"
LABEL_DIR = DATASET_ROOT / "labels" / "val"

OUTPUT_DIR = PROJECT_ROOT / "label_check"

NUM_SAMPLES = 30

CLASS_NAMES = {
    0: "RED",
    1: "GREEN",
    2: "YELLOW",
}

CLASS_COLORS = {
    0: (0, 0, 255),
    1: (0, 255, 0),
    2: (0, 255, 255),
}


# ============================================================
# 中文路徑安全版 OpenCV 讀圖
# ============================================================

def imread_unicode(path: Path):

    try:
        data = np.fromfile(
            str(path),
            dtype=np.uint8
        )

        image = cv2.imdecode(
            data,
            cv2.IMREAD_COLOR
        )

        return image

    except Exception as e:
        print(
            f"[讀圖失敗] {path.name}: {e}"
        )
        return None


# ============================================================
# 中文路徑安全版 OpenCV 寫圖
# ============================================================

def imwrite_unicode(path: Path, image):

    try:

        ext = path.suffix.lower()

        if ext not in [
            ".jpg",
            ".jpeg",
            ".png"
        ]:
            ext = ".jpg"

        success, encoded = cv2.imencode(
            ext,
            image
        )

        if not success:
            return False

        encoded.tofile(
            str(path)
        )

        return True

    except Exception as e:

        print(
            f"[寫圖失敗] {path.name}: {e}"
        )

        return False


# ============================================================
# 讀取 YOLO label
# ============================================================

def read_yolo_label(label_path):

    boxes = []

    if not label_path.exists():
        return boxes

    with open(
        label_path,
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            line = line.strip()

            if not line:
                continue

            parts = line.split()

            if len(parts) != 5:

                print(
                    f"[警告] 格式錯誤："
                    f"{label_path.name}"
                )

                continue

            try:

                class_id = int(parts[0])

                x_center = float(parts[1])
                y_center = float(parts[2])
                width = float(parts[3])
                height = float(parts[4])

            except ValueError:

                print(
                    f"[警告] 數值錯誤："
                    f"{label_path.name}"
                )

                continue

            boxes.append(
                (
                    class_id,
                    x_center,
                    y_center,
                    width,
                    height,
                )
            )

    return boxes


# ============================================================
# YOLO -> Pixel
# ============================================================

def yolo_to_pixel(
    x_center,
    y_center,
    width,
    height,
    image_width,
    image_height,
):

    x_center *= image_width
    y_center *= image_height

    width *= image_width
    height *= image_height

    x1 = int(
        x_center - width / 2
    )

    y1 = int(
        y_center - height / 2
    )

    x2 = int(
        x_center + width / 2
    )

    y2 = int(
        y_center + height / 2
    )

    x1 = max(
        0,
        min(
            x1,
            image_width - 1
        )
    )

    y1 = max(
        0,
        min(
            y1,
            image_height - 1
        )
    )

    x2 = max(
        0,
        min(
            x2,
            image_width - 1
        )
    )

    y2 = max(
        0,
        min(
            y2,
            image_height - 1
        )
    )

    return x1, y1, x2, y2


# ============================================================
# 畫框
# ============================================================

def draw_box(
    image,
    class_id,
    x1,
    y1,
    x2,
    y2,
):

    color = CLASS_COLORS.get(
        class_id,
        (255, 255, 255)
    )

    class_name = CLASS_NAMES.get(
        class_id,
        f"CLASS_{class_id}"
    )

    cv2.rectangle(
        image,
        (x1, y1),
        (x2, y2),
        color,
        2
    )

    font = cv2.FONT_HERSHEY_SIMPLEX

    font_scale = 0.55

    thickness = 2

    (
        text_width,
        text_height
    ), baseline = cv2.getTextSize(
        class_name,
        font,
        font_scale,
        thickness
    )

    text_y = max(
        y1,
        text_height + 6
    )

    cv2.rectangle(
        image,
        (
            x1,
            text_y - text_height - 5
        ),
        (
            x1 + text_width + 6,
            text_y + baseline
        ),
        color,
        -1
    )

    cv2.putText(
        image,
        class_name,
        (
            x1 + 3,
            text_y - 3
        ),
        font,
        font_scale,
        (0, 0, 0),
        thickness,
        cv2.LINE_AA
    )


# ============================================================
# main
# ============================================================

def main():

    print(
        "=" * 70
    )

    print(
        "YOLO Traffic Light Label Checker"
    )

    print(
        "=" * 70
    )

    print()

    print(
        f"Image folder:\n{IMAGE_DIR}"
    )

    print()

    print(
        f"Label folder:\n{LABEL_DIR}"
    )

    print()

    if not IMAGE_DIR.exists():

        raise FileNotFoundError(
            f"找不到圖片資料夾："
            f"{IMAGE_DIR}"
        )

    if not LABEL_DIR.exists():

        raise FileNotFoundError(
            f"找不到標籤資料夾："
            f"{LABEL_DIR}"
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # ========================================================
    # 找 positive labels
    # ========================================================

    positive_labels = []

    for label_path in sorted(
        LABEL_DIR.glob("*.txt")
    ):

        try:

            if label_path.stat().st_size > 0:

                positive_labels.append(
                    label_path
                )

        except OSError:

            continue

    print(
        f"Val 中找到有標註圖片："
        f"{len(positive_labels):,}"
    )

    if not positive_labels:

        print(
            "沒有找到正樣本。"
        )

        return

    sample_count = min(
        NUM_SAMPLES,
        len(positive_labels)
    )

    random.seed(42)

    samples = random.sample(
        positive_labels,
        sample_count
    )

    print(
        f"抽取 {sample_count} 張"
    )

    print()

    total_boxes = 0
    success_images = 0
    failed_images = 0

    # ========================================================
    # 畫框
    # ========================================================

    for index, label_path in enumerate(
        samples,
        start=1
    ):

        image_path = (
            IMAGE_DIR
            / f"{label_path.stem}.jpg"
        )

        if not image_path.exists():

            print(
                f"[找不到圖片] "
                f"{image_path.name}"
            )

            failed_images += 1

            continue

        image = imread_unicode(
            image_path
        )

        if image is None:

            print(
                f"[無法讀圖] "
                f"{image_path.name}"
            )

            failed_images += 1

            continue

        image_height, image_width = (
            image.shape[:2]
        )

        boxes = read_yolo_label(
            label_path
        )

        for (
            class_id,
            x_center,
            y_center,
            width,
            height,
        ) in boxes:

            (
                x1,
                y1,
                x2,
                y2
            ) = yolo_to_pixel(
                x_center,
                y_center,
                width,
                height,
                image_width,
                image_height,
            )

            draw_box(
                image,
                class_id,
                x1,
                y1,
                x2,
                y2
            )

            total_boxes += 1

        output_name = (
            f"{index:02d}_"
            f"{image_path.name}"
        )

        output_path = (
            OUTPUT_DIR
            / output_name
        )

        success = imwrite_unicode(
            output_path,
            image
        )

        if success:

            success_images += 1

            print(
                f"[{index:02d}/{sample_count}] "
                f"{image_path.name} "
                f"-> {len(boxes)} boxes"
            )

        else:

            failed_images += 1

    print()

    print(
        "=" * 70
    )

    print(
        "檢查圖片產生完成"
    )

    print(
        "=" * 70
    )

    print()

    print(
        f"成功圖片：{success_images}"
    )

    print(
        f"失敗圖片：{failed_images}"
    )

    print(
        f"總共畫出：{total_boxes} 個框"
    )

    print()

    print(
        "輸出位置："
    )

    print(
        OUTPUT_DIR
    )

    print()

    print(
        "請人工確認："
    )

    print(
        "1. 框是否真的框住交通號誌"
    )

    print(
        "2. RED 是否為紅燈"
    )

    print(
        "3. GREEN 是否為綠燈"
    )

    print(
        "4. YELLOW 是否為黃燈"
    )

    print(
        "5. 框是否整體偏移"
    )


if __name__ == "__main__":

    main()