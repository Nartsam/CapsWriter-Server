"""
ASR 引擎语言标识符映射

客户端发来的 language 字段使用统一代码（全称小写英文，如 auto / chinese / english），
这里转换为 Qwen3-ASR / Qwen3-ForcedAligner 所需的英文名称（首字母大写）。
两个引擎的标识符完全相同；"auto" 映射为 None，表示不在提示词里指定语言、交给模型自动判断。
"""

from typing import Dict, List, Optional

# ── 引擎名称常量 ──
ENGINE_QWEN_ASR = "qwen_asr"
ENGINE_ALIGNER = "aligner"

# 统一代码 -> Qwen3 语言名称（按使用频率排序）
LANGUAGE_MAP: Dict[str, Optional[str]] = {
    "auto": None,               # 自动检测
    "chinese": "Chinese",
    "english": "English",
    "cantonese": "Cantonese",
    "japanese": "Japanese",
    "korean": "Korean",
    "arabic": "Arabic",
    "german": "German",
    "french": "French",
    "spanish": "Spanish",
    "portuguese": "Portuguese",
    "indonesian": "Indonesian",
    "italian": "Italian",
    "russian": "Russian",
    "thai": "Thai",
    "vietnamese": "Vietnamese",
    "turkish": "Turkish",
    "hindi": "Hindi",
    "malay": "Malay",
    "dutch": "Dutch",
    "swedish": "Swedish",
    "danish": "Danish",
    "finnish": "Finnish",
    "polish": "Polish",
    "czech": "Czech",
    "filipino": "Filipino",
    "persian": "Persian",
    "greek": "Greek",
    "romanian": "Romanian",
    "hungarian": "Hungarian",
    "macedonian": "Macedonian",
}


def get_language(engine: str, unified_code: str) -> Optional[str]:
    """
    将统一语言代码转换为指定引擎的标识符。

    Args:
        engine: 引擎名称常量 (ENGINE_*)；目前两个引擎的映射相同，保留该参数以便将来区分
        unified_code: 统一语言代码 (如 "chinese", "english")，不区分大小写

    Returns:
        引擎特定的语言标识符；"auto" 或不支持的代码返回 None（即不指定语言）
    """
    return LANGUAGE_MAP.get(unified_code.lower())


def supported_codes() -> List[str]:
    """获取支持的所有统一语言代码（不含 auto）"""
    return [code for code, name in LANGUAGE_MAP.items() if name is not None]
