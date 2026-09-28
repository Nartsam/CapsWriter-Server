# coding: utf-8
"""
Qwen3-ForcedAligner 强制对齐引擎适配层

推理内核与 Qwen3-ASR 共用 core/server/engines/qwen_asr_gguf/inference/。
只在文件转录任务里按需加载（见 engines/manager.py 的 ManagedAlignerProxy）。
"""
