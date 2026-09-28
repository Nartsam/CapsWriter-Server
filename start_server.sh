#!/usr/bin/env bash
# CapsWriter-Server Linux 启动入口（与 Windows 的 start_server.exe 等价）
#
#   ./start_server.sh [--port N] [--host ADDR] [--gpu N] [--download-models] [-h]
#
# 本脚本只负责准备运行环境（Python 解释器、动态库路径），然后 exec start_server.py。
# 参数解析、配置覆盖、端口/模型检查都在 Python 侧（core/server/launch.py、app.py），
# Windows 的 exe 走的是同一段代码，所以两个入口的参数与行为完全一致。
#
# 兼容 asmgt LAUNCHER-CONTRACT v1：
#   - 前台运行并 exec 替换进程（M1），输出不缓冲（M2），不依赖 conda activate（M4），先 cd 到项目目录（M5）
#   - 由 ASMGT_PORT / ASMGT_HOST / ASMGT_GPU 注入参数；未知参数由 argparse 报错并以 2 退出（M7）
#   - 忙闲探针：logs/server_status.json 的 queued + processing（文件型，因为本服务是 WebSocket 不是 HTTP）
#
# Python 解释器查找顺序：$CAPSWRITER_PYTHON → .conda-env/bin/python → .venv/bin/python
# （环境由 setup_linux.sh 创建，详见 docs/LINUX.md）

set -Eeuo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$BASE_DIR"

# ---- asmgt 注入的参数 → 服务端统一的环境变量（命令行参数优先级更高） ----
[[ -n "${ASMGT_PORT:-}" ]] && export CAPSWRITER_PORT="$ASMGT_PORT"
[[ -n "${ASMGT_HOST:-}" ]] && export CAPSWRITER_HOST="$ASMGT_HOST"
[[ -n "${ASMGT_GPU:-}"  ]] && export CAPSWRITER_GPU="$ASMGT_GPU"

# ---- Python 解释器 ----
PYTHON="${CAPSWRITER_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in "$BASE_DIR/.conda-env/bin/python" "$BASE_DIR/.venv/bin/python"; do
        if [[ -x "$candidate" ]]; then PYTHON="$candidate"; break; fi
    done
fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
    echo "错误：找不到 Python 环境。请先运行 ./setup_linux.sh，或用 CAPSWRITER_PYTHON 指定解释器" >&2
    exit 1
fi

# ---- 动态库搜索路径 ----
# llama.cpp 的 .so 自带 RUNPATH=$ORIGIN，本不需要；加上 bin 目录与旧版部署保持一致。
# onnxruntime-gpu 需要 CUDA 12 / cuDNN 9：优先用 pip 装进环境的 nvidia-* 包，其次兼容旧环境的 lib/cudnn-local。
ENV_PREFIX="$(cd -- "$(dirname -- "$PYTHON")/.." && pwd -P)"
LIB_PATHS="$BASE_DIR/core/server/engines/llama/bin"
shopt -s nullglob
for dir in "$ENV_PREFIX"/lib/python3*/site-packages/nvidia/*/lib "$ENV_PREFIX/lib/cudnn-local"; do
    [[ -d "$dir" ]] && LIB_PATHS="$LIB_PATHS:$dir"
done
shopt -u nullglob
export LD_LIBRARY_PATH="$LIB_PATHS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

export PYTHONNOUSERSITE=1
export PYTHONUNBUFFERED=1

echo "[start_server.sh] 实例=${ASMGT_INSTANCE:-手动}  Python=$PYTHON  端口=${CAPSWRITER_PORT:-配置文件}  GPU=${CAPSWRITER_GPU:-全部可见}"

exec "$PYTHON" "$BASE_DIR/start_server.py" "$@"
