# coding: utf-8
"""
流式任务的前缀续写

流式任务的片段是按停顿切出的一句一句（见 connection/pause_splitter.py）。逐句独立识别
会丢掉上下文：句与句之间的标点全变成句号，开头几个字也常认错。这里的做法是：

- 识别第 i 句时，把前面几句的完整音频连同本句一起送进模型（窗口），并把这几句已经
  识别出的文字作为 assistant 的前缀（Qwen3 prompt 里 <asr_text> 之后的内容），
  模型只需续写本句，开头会自己带上两句之间的标点。
- 每句末尾的标点先不发出（暂留），由下一句续写结果的开头决定；最后一句原样发出。
  所以已经发给客户端的文字永远不用改，客户端可以直接打字上屏。

前缀必须与窗口音频逐字对应：前缀只能取整句（不能截半句）、结束在文字上（去掉末尾标点），
否则模型会接错位置或跑题。离线实验里试过把每句末尾几个字也暂留到下一句重新识别，
结果都更差，所以不做。
"""

import re
from typing import List, Tuple

import numpy as np

from core.constants import AudioFormat

# 暂留与前缀处理时视为「末尾标点」的字符（含空格）
HOLD_PUNCT = '，。！？；：、,.!?;: '

# inference/asr.py 熔断重试后附在结果末尾的提示，不能进前缀
_ABORT_MARK = '====解码有误，强制熔断===='

SAMPLE_RATE = AudioFormat.SAMPLE_RATE


class StreamState:
    """一次流式任务的前文状态，保存在 RecognitionSession.stream 中，随会话一起清理"""

    # 窗口取前面的整句：凑够 6s 就停，最多 3 句、不超过 25s
    MIN_PREV_SEC = 6.0
    MAX_PREV_SEGS = 3
    MAX_PREV_SEC = 25.0

    def __init__(self):
        self.history: List[Tuple[np.ndarray, str]] = []    # 最近几句 (音频, 模型原始输出)
        self.committed = ''     # 已发出的原始文字（未格式化，不含暂留的末尾标点）
        self.held = ''          # 上一句暂留的末尾标点

    def window(self, samples: np.ndarray) -> Tuple[np.ndarray, str]:
        """本句的识别窗口：(前几句音频 + 本句音频, 前缀文字)"""
        prev, total = [], 0
        for audio, text in reversed(self.history):
            n = len(audio)
            if (total >= self.MIN_PREV_SEC * SAMPLE_RATE or len(prev) >= self.MAX_PREV_SEGS
                    or total + n > self.MAX_PREV_SEC * SAMPLE_RATE):
                break
            prev.insert(0, (audio, text))
            total += n
        if not prev:
            return samples, ''
        audio = np.concatenate([a for a, _ in prev] + [samples])
        # 各句末尾标点去掉（两句之间的标点在后一句的开头）；窗口从句子边界开始，开头的标点也去掉
        prefix = ''.join(t.rstrip(HOLD_PUNCT) for _, t in prev).lstrip(HOLD_PUNCT)
        return audio, prefix

    def remember(self, samples: np.ndarray, text: str) -> None:
        """记下识别过的一句，供后面的句子做前文"""
        self.history.append((samples, text.replace(_ABORT_MARK, '')))
        del self.history[:-self.MAX_PREV_SEGS]

    def take(self, text: str, is_final: bool) -> str:
        """
        由本句的续写结果算出这次要发出的原始文字

        非最后一句：末尾标点暂留。续写结果为空时保留上一句暂留的标点（两句之间还没有新内容）。
        最后一句：全部发出；结果为空（静音、被跳过）时补发上一句暂留的标点。
        """
        text = clean_text(text, self.committed)
        if not self.committed:
            text = text.lstrip(HOLD_PUNCT)      # 前面还没有字，开头不该有标点
        if not text:
            piece = self.held.strip() if is_final else ''
            if is_final:
                self.held = ''
            return piece
        if is_final:
            self.held = ''
            return text
        piece = text.rstrip(HOLD_PUNCT)
        self.held = text[len(piece):]
        return piece

    def format(self, piece: str, formatter) -> str:
        """
        格式化（ITN、中英空格）要发出的一段，并计入已发出的文字

        单独格式化这一段会丢掉与上一段交界处的空格（「用」+「Git」），所以带上已发出文字的
        最后几个字一起格式化，再去掉这几个字格式化后的样子；交界处被 ITN 改写、对不上时
        退回单独格式化这一段。
        """
        if not piece:
            return ''
        tail = self.committed[-8:]
        self.committed += piece
        if tail:
            head = formatter.format(tail)
            both = formatter.format(tail + piece)
            if both.startswith(head):
                return both[len(head):]
        return formatter.format(piece)


def clean_text(text: str, committed: str) -> str:
    """规整模型输出的空白；续写开头的空格只在两侧都是英文/数字时保留（单词之间的空格）"""
    text = re.sub(r'\s+', ' ', text.replace('@@', '')).rstrip()
    if text.startswith(' '):
        text = text.lstrip()
        if committed and _is_word_char(committed[-1]) and text and _is_word_char(text[0]):
            text = ' ' + text
    return text


def _is_word_char(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()
