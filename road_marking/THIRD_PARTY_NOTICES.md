# Attribution and scope

This source distribution contains MMSegmentation 1.2.2 from OpenMMLab, with local
road-marking changes (including slide inference, loss and dataset code).
The original Apache-2.0 LICENSE and CITATION.cff are retained; upstream copyright
headers are preserved. This is a custom inference distribution, not an official
OpenMMLab release. External dependencies retain their own licenses.

This branch contains the project best56000 checkpoint (Git LFS) and
the fixed 374-image RLMD validation subset with its masks. They are runtime
assets, not part of the framework's Apache-2.0 license grant; their original
dataset/model terms continue to apply. No user input video is distributed.
The tokenizer vocabulary bundled by MMSegmentation is preserved as an upstream
runtime resource. FFmpeg is installed through the imageio-ffmpeg wheel, which
includes its own binary/license notices; it is not copied from a developer PC.
