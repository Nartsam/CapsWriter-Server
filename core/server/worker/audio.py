# coding: utf-8
"""
音频预处理模块

负责将原始音轨数据 (bytes) 转换为模型可用的采样数组 (numpy.float32)，
并处理时长累加等与信号处理相关的逻辑。
"""

import numpy as np
from typing import Optional
from core.constants import AudioFormat
from core.server.schema import Task, Result

# 短于这个长度的片段不送进模型（模型会把纯静音识别成「嗯。」之类的幻觉）
MIN_SAMPLES = int(AudioFormat.SAMPLE_RATE * 0.1)


def process_audio_task(task: Task, result: Result) -> Optional[np.ndarray]:
    """
    处理音频片段任务并更新结果中的时长信息
    
    Args:
        task: 识别任务对象
        result: 识别结果对象
        
    Returns:
        samples: 转换后的 numpy 数组 (float32)，空音频时返回 None
    """
    # 1. 转换字节流为 float32 采样数组
    samples = np.frombuffer(task.data, dtype=np.float32)

    # 用片段的实际终点计算总时长，重叠窗口和不足 0.1 秒的尾段均不重复或漏计。
    result.duration = max(result.duration, task.offset + len(samples) / task.samplerate)

    # 空音频或极短片段直接跳过
    if len(samples) < MIN_SAMPLES:
        return None

    return samples
