import random

import numpy as np
from PIL import Image

from mmseg.datasets import BaseSegDataset
from mmseg.registry import DATASETS


@DATASETS.register_module()
class RLMDRCSDataset(BaseSegDataset):
    """RLMD dataset with Rare Class Sampling (RCS).

    Training behavior:
    1. Scan the real annotation masks used by MMSeg.
    2. Ignore background/ignore IDs for rare-class selection.
    3. Compute foreground class frequencies.
    4. Sample a class with temperature-based rare-class probabilities.
    5. Sample an image that contains that class.
    6. Run the normal MMSeg augmentation pipeline.
    7. Retry if too few target-class pixels survive the crop.

    This class is only intended for the TRAIN split.
    Validation/test should still use BaseSegDataset.
    """

    def __init__(
        self,
        rcs_temperature=0.5,
        rcs_min_pixels=1,
        rcs_min_crop_pixels=32,
        rcs_ignore_ids=(0, 255),
        rcs_max_retries=10,
        **kwargs,
    ):
        # IMPORTANT:
        # Pop/store every custom RCS argument here so none of them are passed
        # into BaseSegDataset.__init__.
        self.rcs_temperature = float(rcs_temperature)
        self.rcs_min_pixels = int(rcs_min_pixels)
        self.rcs_min_crop_pixels = int(rcs_min_crop_pixels)
        self.rcs_ignore_ids = {int(x) for x in rcs_ignore_ids}
        self.rcs_max_retries = int(rcs_max_retries)

        super().__init__(**kwargs)

        if not self.test_mode:
            self._init_rcs()

    @staticmethod
    def _read_mask_counts(seg_map_path):
        """Return {class_id: pixel_count} for one segmentation mask."""
        with Image.open(seg_map_path) as img:
            mask = np.asarray(img)

        ids, counts = np.unique(mask, return_counts=True)

        return {
            int(class_id): int(count)
            for class_id, count in zip(ids, counts)
        }

    def _init_rcs(self):
        """Build RCS statistics directly from MMSeg's real mask paths."""
        class_pixel_totals = {}
        samples_with_class = {}

        print()
        print("=" * 72)
        print("[RCS] Building statistics directly from annotation masks")
        print("=" * 72)
        print(f"[RCS] dataset images       : {len(self)}")
        print(f"[RCS] temperature          : {self.rcs_temperature}")
        print(f"[RCS] source min pixels    : {self.rcs_min_pixels}")
        print(f"[RCS] crop min pixels      : {self.rcs_min_crop_pixels}")
        print(f"[RCS] max retries          : {self.rcs_max_retries}")
        print(f"[RCS] ignore ids           : {sorted(self.rcs_ignore_ids)}")

        for idx in range(len(self)):
            info = self.get_data_info(idx)

            if "seg_map_path" not in info:
                raise RuntimeError(
                    f"Dataset item {idx} has no 'seg_map_path'. "
                    "Check data_prefix and seg_map_suffix."
                )

            seg_path = info["seg_map_path"]

            if idx == 0:
                print(f"[RCS] first mask            : {seg_path}")

            counts = self._read_mask_counts(seg_path)

            for class_id, pixel_count in counts.items():
                if class_id in self.rcs_ignore_ids:
                    continue

                class_pixel_totals[class_id] = (
                    class_pixel_totals.get(class_id, 0) + pixel_count
                )

                # This image can be sampled for the class only if the class
                # exists with at least rcs_min_pixels in the original mask.
                if pixel_count >= self.rcs_min_pixels:
                    samples_with_class.setdefault(class_id, []).append(idx)

            if (idx + 1) % 100 == 0 or (idx + 1) == len(self):
                print(
                    f"[RCS] scanned               : "
                    f"{idx + 1}/{len(self)}"
                )

        eligible_classes = sorted(
            class_id
            for class_id in class_pixel_totals
            if len(samples_with_class.get(class_id, [])) > 0
        )

        if not eligible_classes:
            print("[RCS] class_pixel_totals:", class_pixel_totals)
            raise RuntimeError(
                "RCS found no eligible foreground classes. "
                "Check annotation IDs and paths."
            )

        # Frequency among eligible foreground classes only.
        totals = np.asarray(
            [class_pixel_totals[c] for c in eligible_classes],
            dtype=np.float64,
        )
        freqs = totals / totals.sum()

        # DAFormer-style temperature idea:
        # lower-frequency classes receive higher probability.
        #
        # softmax(-freq / T)
        logits = -freqs / max(self.rcs_temperature, 1e-8)
        logits -= logits.max()

        probs = np.exp(logits)
        probs /= probs.sum()

        self.rcs_classes = np.asarray(eligible_classes, dtype=np.int64)
        self.rcs_probs = np.asarray(probs, dtype=np.float64)
        self.samples_with_class = samples_with_class
        self.class_pixel_totals = class_pixel_totals

        print()
        print("=" * 72)
        print("[RCS] ENABLED")
        print("=" * 72)
        print("[RCS] eligible classes     :", self.rcs_classes.tolist())
        print()
        print("[RCS] sampling probabilities:")

        rows = sorted(
            zip(self.rcs_classes.tolist(), self.rcs_probs.tolist()),
            key=lambda x: x[1],
            reverse=True,
        )

        for class_id, prob in rows:
            print(
                f"  class {class_id:2d} | "
                f"p={prob:.6f} | "
                f"images={len(self.samples_with_class[class_id]):4d} | "
                f"pixels={self.class_pixel_totals[class_id]}"
            )

        print("=" * 72)
        print()

    @staticmethod
    def _count_class_pixels_in_packed_sample(packed, class_id):
        """Count target-class pixels after resize/crop/flip."""
        try:
            gt = packed["data_samples"].gt_sem_seg.data
            return int((gt == class_id).sum().item())
        except Exception:
            return 0

    def __getitem__(self, idx):
        if self.test_mode:
            return super().__getitem__(idx)

        # RCS intentionally ignores the sampler-provided index:
        # choose class first, then an image containing that class.
        target_class = int(
            np.random.choice(
                self.rcs_classes,
                p=self.rcs_probs,
            )
        )

        candidates = self.samples_with_class[target_class]
        last_valid_sample = None

        for _ in range(self.rcs_max_retries):
            chosen_idx = random.choice(candidates)
            packed = self.prepare_data(chosen_idx)

            if packed is None:
                continue

            last_valid_sample = packed

            kept_pixels = self._count_class_pixels_in_packed_sample(
                packed,
                target_class,
            )

            if kept_pixels >= self.rcs_min_crop_pixels:
                return packed

        # If repeated random crops miss the selected class, return the last
        # valid sample instead of crashing the entire training process.
        if last_valid_sample is not None:
            return last_valid_sample

        # Extremely unlikely fallback.
        return super().__getitem__(idx)
