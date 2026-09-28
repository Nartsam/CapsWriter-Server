#!/usr/bin/env bash
# 创建 CapsWriter-Server 的 Linux Python 环境并安装依赖（可重复运行）
#
#   ./setup_linux.sh [--conda | --venv] [--system-cuda] [--python PYTHON]
#
#   --conda        用 conda 在 .conda-env/ 建 Python 3.10 环境（有 conda 时的默认选择）
#   --venv         用系统 Python 在 .venv/ 建 venv（需要 python3-venv；没有 conda 时的默认选择）
#   --python P     --venv 模式下使用的解释器，默认 python3（要求 >= 3.10）
#   --system-cuda  不通过 pip 安装 CUDA/cuDNN 运行库，改用系统里已有的 CUDA 12 + cuDNN 9
#
# 完成后：./download_models.sh 下载模型，再 ./start_server.sh 启动。详见 docs/LINUX.md

set -Eeuo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$BASE_DIR"

MODE=auto
SYSTEM_CUDA=0
BASE_PYTHON=python3

while [[ $# -gt 0 ]]; do
    case "$1" in
        --conda) MODE=conda; shift ;;
        --venv) MODE=venv; shift ;;
        --system-cuda) SYSTEM_CUDA=1; shift ;;
        --python) BASE_PYTHON="$2"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

if [[ "$MODE" == auto ]]; then
    if command -v conda >/dev/null 2>&1; then MODE=conda; else MODE=venv; fi
fi

# ---- 1. 创建环境 ----
if [[ "$MODE" == conda ]]; then
    PYTHON="$BASE_DIR/.conda-env/bin/python"
    if [[ ! -x "$PYTHON" ]]; then
        echo ">> conda 创建环境 .conda-env (Python 3.10)"
        conda create -y -p "$BASE_DIR/.conda-env" python=3.10 pip
    fi
else
    PYTHON="$BASE_DIR/.venv/bin/python"
    if [[ ! -x "$PYTHON" ]]; then
        echo ">> venv 创建环境 .venv（$BASE_PYTHON）"
        "$BASE_PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python >= 3.10"'
        "$BASE_PYTHON" -m venv "$BASE_DIR/.venv" || {
            echo "创建 venv 失败：Debian/Ubuntu 上请先 sudo apt install python3-venv，或改用 --conda" >&2
            exit 1
        }
    fi
fi
echo ">> 使用 Python: $PYTHON ($("$PYTHON" --version 2>&1))"

# ---- 2. 安装依赖 ----
REQ="$BASE_DIR/requirements-linux.txt"
if [[ "$SYSTEM_CUDA" == 1 ]]; then
    REQ="$(mktemp)"
    trap 'rm -f "$REQ"' EXIT
    grep -v '^nvidia-' "$BASE_DIR/requirements-linux.txt" > "$REQ"
fi
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r "$REQ"

# ---- 3. 自检（只提示，不中断） ----
echo
echo ">> 自检"
PYTHONNOUSERSITE=1 "$PYTHON" -c 'import onnxruntime as ort; print("   onnxruntime", ort.__version__, "可用后端:", ort.get_available_providers())'

if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader | sed 's/^/   GPU /'
else
    echo "   [警告] 找不到 nvidia-smi：ONNX 编码器与 llama.cpp 都会退回 CPU，识别会慢很多"
fi

if ! ls /usr/share/vulkan/icd.d/*nvidia* /etc/vulkan/icd.d/*nvidia* >/dev/null 2>&1; then
    echo "   [警告] 未找到 NVIDIA 的 Vulkan ICD（/usr/share/vulkan/icd.d/nvidia_icd.json）：llama.cpp 解码会退回 CPU"
fi

if ! ldconfig -p 2>/dev/null | grep -q 'libgomp.so.1'; then
    echo "   [警告] 系统缺少 libgomp.so.1（llama.cpp 的 CPU 后端需要），Debian/Ubuntu: sudo apt install libgomp1"
fi

"$PYTHON" - <<'PY'
from core.server.model_download import missing_packages
missing = missing_packages()
if missing:
    print("   模型未就绪：" + "、".join(p.name for p in missing) + " —— 请运行 ./download_models.sh")
else:
    print("   模型已就绪")
PY

echo
echo "完成。启动：./start_server.sh [--port N] [--gpu N]"
