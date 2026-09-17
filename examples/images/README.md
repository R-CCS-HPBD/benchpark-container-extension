# Common Base imageの前提（v0.3.1）

既存のPython 3.10以上とPyTorch等が入ったSIFを使用する。**venv/ensurepipのためにCommon Baseを作り直さない。** 追加構築ツールの固定pipはExtensionが同梱し、Baseのpipは上書きしない。

```bash
apptainer exec /absolute/existing-base.sif python3 -c \
  'import sys, torch; print(sys.version); print(torch.__version__, torch.__file__)'
```

import成功だけで全依存・ABI互換性を保証しない。`verify_upstream.py --base-sif ... --run`で追加wheel、import、benchmark、CERまで確認する。Python/標準モジュールが欠落しているimage、OSライブラリが足りないimageは自動修復しない。

このrepositoryにvendor image、PyTorch、model weightsは同梱しない。実機で測定したSIF SHA-256/元OCI digestはsnapshotに保存する。新version比較には別image/別実験条件を生成する。
