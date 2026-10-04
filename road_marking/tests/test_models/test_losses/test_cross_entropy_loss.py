# Copyright (c) OpenMMLab. All rights reserved.
import torch
import torch.nn.functional as F

from mmseg.models.losses import CrossEntropyLoss, weight_reduce_loss


def test_cross_entropy_loss_class_weights():
    loss_class = CrossEntropyLoss
    pred = torch.rand((1, 10, 4, 4))
    target = torch.randint(0, 10, (1, 4, 4))
    class_weight = torch.ones(10)
    avg_factor = target.numel()

    cross_entropy_loss = F.cross_entropy(
        pred, target, weight=class_weight, reduction='none', ignore_index=-100)

    expected_loss = weight_reduce_loss(
        cross_entropy_loss,
        weight=None,
        reduction='mean',
        avg_factor=avg_factor)

    # Test loss forward
    loss = loss_class(class_weight=class_weight.tolist())(pred, target)

    assert isinstance(loss, torch.Tensor)
    assert expected_loss == loss


def test_weighted_cross_entropy_ignored_labels():
    from mmseg.models.losses.cross_entropy_loss import cross_entropy

    devices = ['cpu'] + (['cuda'] if torch.cuda.is_available() else [])
    for device in devices:
        weights = torch.tensor([1., 5., 10.], device=device)
        for ignore_index in (255, -100):
            for avg_non_ignore in (False, True):
                for all_ignored in (False, True):
                    pred = torch.randn(1, 3, 2, 2, device=device,
                                       requires_grad=True)
                    target = torch.tensor(
                        [[[0, 1], [2, ignore_index]]], device=device)
                    if all_ignored:
                        target.fill_(ignore_index)
                    valid = target != ignore_index
                    loss = cross_entropy(
                        pred, target, class_weight=weights,
                        ignore_index=ignore_index,
                        avg_non_ignore=avg_non_ignore)
                    numerator = F.cross_entropy(
                        pred, target, weight=weights,
                        ignore_index=ignore_index, reduction='sum')
                    denominator = weights[target[valid]].sum()
                    if not avg_non_ignore:
                        denominator = denominator + (~valid).sum()
                    expected = numerator / (
                        denominator + torch.finfo(torch.float32).eps)
                    torch.testing.assert_close(loss, expected)
                    loss.backward()
                    assert torch.isfinite(pred.grad).all()
                    assert (pred.grad.permute(0, 2, 3, 1)[~valid] == 0).all()
