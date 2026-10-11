# 本機 ONNX GPU 環境

2026-10-05 將專案虛擬環境的 onnxruntime 1.23.2 改為 onnxruntime-gpu 1.23.2。
硬體為 RTX 5060 Laptop GPU（8 GB），PyTorch 2.11.0+cu128、CUDA 12.8、cuDNN 9.19。
使用 PyTorch 隨附 DLL，未更動系統顯示卡驅動。

車輛、車牌定位、深度三個 ONNX 模型均已透過執行分析紀錄確認 CUDA 節點有實際執行，輸出數值皆有限。
少部分形狀或輔助運算仍由 CPU 處理，這不表示整個模型退回 CPU。
紀錄位於 `outputs/gpu_setup/benchmark.json` 及同目錄的 ONNX profiling JSON。
計時使用隨機輸入、一次暖機及三次推論，包含 profiler 負擔，僅供診斷，不能當作整段影片加速倍數。

完整六階段流程已使用 input/ms01.mp4、--device cuda:0 通過 10 幀驗證，結果位於 outputs/gpu_setup/pipeline/。
首次 GPU 暖機耗時明顯，該次含暖機平均僅 0.09 FPS，不代表穩態處理速度；尚未完成長片吞吐量測試。
原有車牌保留與違規剪輯的 8 項回歸測試通過。

正常啟動的 `--device auto` 已會選用 GPU，也可以明確指定 `--device cuda:0`。
Paddle OCR 維持 CPU；紅綠燈與道路標線原本就經由 PyTorch 使用可用的 CUDA。
仍可使用 `--device cpu`。不要同時安裝 CPU 版 onnxruntime 與 onnxruntime-gpu，兩者共用 Python 模組路徑。
已移除不再被本專案使用、但要求 CPU 版 ONNX 套件的 rapidocr-onnxruntime 1.4.4。

原 CPU 版與本次 GPU 版 wheel 保存在 `outputs/gpu_setup/wheels/`。
如需還原，先卸載 onnxruntime-gpu，再從該目錄安裝 CPU wheel，並將 requirements-perception.txt 的 GPU 依賴改回 CPU 版。

相容性與 DLL 預載方式參考 [ONNX Runtime CUDA 官方文件](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)。
