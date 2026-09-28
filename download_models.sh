#!/usr/bin/env bash
# 下载/补全模型（约 2.5GB，只需一次）。需要先运行 ./setup_linux.sh 建好 Python 环境。
# 等同于 ./start_server.sh --download-models；下载源与 SHA-256 清单见 core/server/model_download.py
set -Eeuo pipefail
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/start_server.sh" --download-models "$@"
