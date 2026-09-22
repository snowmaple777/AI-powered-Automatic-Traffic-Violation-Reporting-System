"""Experimental stop-line crops; other classes use the original RCS policy."""
import random
import numpy as np
from mmseg.datasets.rlmd_rcs_dataset import RLMDRCSDataset
from mmseg.datasets import BaseSegDataset
from mmseg.registry import DATASETS


@DATASETS.register_module()
class RLMDStopCropDataset(RLMDRCSDataset):
    def __init__(self, stop_crop_min_pixels=128, **kwargs):
        self.stop_crop_min_pixels = int(stop_crop_min_pixels)
        if self.stop_crop_min_pixels < 1:
            raise ValueError('stop_crop_min_pixels must be positive')
        super().__init__(**kwargs)
        if self.rcs_max_retries < 1:
            raise ValueError('rcs_max_retries must be positive')

    def _sample_stopline(self, idx):
        # Keep this source image across retries, including small/distant lines.
        chosen_idx = random.choice(self.samples_with_class[3])
        best_sample, best_pixels = None, -1
        for _ in range(self.rcs_max_retries):
            packed = self.prepare_data(chosen_idx)
            if packed is None:
                continue
            pixels = self._count_class_pixels_in_packed_sample(packed, 3)
            if pixels > best_pixels:
                best_sample, best_pixels = packed, pixels
            if pixels >= self.stop_crop_min_pixels:
                return packed
        # A small positive crop is retained even below 128 pixels.
        # Zero pixels remain possible if every valid attempt misses the line.
        if best_sample is not None:
            return best_sample
        return BaseSegDataset.__getitem__(self, idx)

    def __getitem__(self, idx):
        if self.test_mode:
            return BaseSegDataset.__getitem__(self, idx)

        # RCS intentionally ignores the sampler-provided index:
        # choose class first, then an image containing that class.
        target_class = int(
            np.random.choice(
                self.rcs_classes,
                p=self.rcs_probs,
            )
        )

        if target_class == 3:
            return self._sample_stopline(idx)

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
        return BaseSegDataset.__getitem__(self, idx)
