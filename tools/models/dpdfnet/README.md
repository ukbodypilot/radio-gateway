# DPDFNet 8 kHz model

`dpdfnet2_8khz.onnx` — DPDFNet (DeepFilterNet2 with dual-path RNN blocks),
2-block narrowband model, streaming ONNX export with its state embedded.

- Source: https://huggingface.co/Ceva-IP/DPDFNet (`onnx/dpdfnet2_8khz.onnx`,
  repo revision `dd6818d00f50c836fed43a6243ebe49116de5964`)
- Paper: "DPDFNet: Boosting DeepFilterNet2" (arXiv 2512.16420)
- Licence: Apache License 2.0, as stated on the model card. Copyright the
  original authors (Ceva). Full licence text: https://www.apache.org/licenses/LICENSE-2.0
- SHA-256: `6218f1dbd6e4bac5768c63b7d899fe7b84b3788f2a35c4e246d4ab0946165c5d`
  (also checked in `audio_util._DPDFNET_MODEL_SHA256` and by
  `tests/test_dpdfnet_stream.py`)
- Unmodified. Loaded by `audio_util._DPDFNet8kStream` through onnxruntime;
  the `dpdfnet` pip package is not used.
