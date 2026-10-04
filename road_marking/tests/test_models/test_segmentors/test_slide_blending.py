"""Run directly with runpy, or collect with pytest."""
import torch
from mmengine import ConfigDict
from mmseg.models.segmentors.encoder_decoder import EncoderDecoder


class SlideStub:
    out_channels = 2
    slide_inference = EncoderDecoder.slide_inference

    def __init__(self, blend):
        self.test_cfg = ConfigDict(crop_size=(8, 8), stride=(5, 5),
                                   blend=blend, blend_sigma=.25)

    def encode_decode(self, image, metas):
        return image[:, :2]

    def whole_inference(self, image, metas):
        return self.encode_decode(image, metas)


def test_slide_blending():
    for device in ['cpu'] + (['cuda'] if torch.cuda.is_available() else []):
        for shape in [(19, 23), (3, 5), (8, 8), (7, 21)]:
            image = torch.randn(2, 3, *shape, device=device)
            metas = [dict(img_shape=shape), dict(img_shape=shape)]
            for blend in ['uniform', 'gaussian']:
                result = SlideStub(blend).slide_inference(image, metas)
                torch.testing.assert_close(result, image[:, :2])
                assert all(meta['img_shape'] == shape for meta in metas)
                assert torch.isfinite(result).all()
        # A crop-edge-only false activation should contribute less inside
        # overlaps than a prediction at a neighbouring crop's centre.
        class EdgeStub(SlideStub):
            def encode_decode(self, image, metas):
                result = torch.zeros_like(image[:, :2])
                result[:, :, :, 0] = 1
                return result
        image = torch.zeros(1, 3, 8, 13, device=device)
        uniform = EdgeStub('uniform').slide_inference(image, [dict(img_shape=(8,13))])
        smooth = EdgeStub('gaussian').slide_inference(image, [dict(img_shape=(8,13))])
        assert smooth[0,0,4,5] < uniform[0,0,4,5]
        # A border with only one covering tile retains its original value.
        torch.testing.assert_close(smooth[:,:,:,0], uniform[:,:,:,0])
        hybrid = EdgeStub('gaussian')
        hybrid.test_cfg.whole_weight = .25
        actual = hybrid.slide_inference(image, [dict(img_shape=(8,13))])
        expected = .75 * smooth + .25 * hybrid.whole_inference(image, [])
        torch.testing.assert_close(actual, expected)


if __name__ == '__main__':
    test_slide_blending()
    print('PASS: CPU/CUDA coverage, normalization, borders, edge weighting, metadata')
