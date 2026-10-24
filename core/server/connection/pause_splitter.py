# coding: utf-8
"""
流式任务的停顿切句

麦克风流式任务（AudioMessage.streaming）不按固定时长分段，而是在说话人停顿处切开，
每切出一句就提交识别，客户端因此能在按住录音键时逐句上屏。

用因果的能量 VAD：只看已经收到的音频，切点就是检测到足够长停顿的那一刻。
帧长 50ms，与客户端每次发送的音频块一致，所以停顿一出现就能判定。

参数来自 2026-10 用 42 段真实听写录音（≥8s）做的离线实验：停顿 0.6s 时，配合
worker/streaming.py 的前缀续写，用字差异 1.9%（整段识别两次互比为 0.6%），对人工
标准答案的字错率与整段识别相同，长录音松手后的等待从约 2.9s 降到约 0.4s。
停顿阈值改大切得更少、上屏更慢，改小则碎句增多。

噪声底的上升要限速（NOISE_RISE）。连续说话超过约 4s、窗口里的停顿不到 1s 时，第 20 百分位
会落在说话的音量上，句尾较轻的字（如拖长的「歧义」的「义」，比真实底噪高 20dB 以上）就被
当成静音：句子切在字的中间，这个字没被识别。切点之后剩下的那一截也判不出语音，于是停多久都
不切（等到下一句才和后面的话一起上屏），直接松手时它又被当作静音尾巴跳过（丢字）。
限速为每秒 1dB、下降不限后，用 2026-10 的 582 段真实录音（固定随机种子，前后两版逐段对比）
回归：135 段切点有变化，其中 78 秒以内的 131 段用字差异从 1.17% 降到 0.92%，两例松手丢字修复，
总句数从 708 变为 685。代价是背景噪声突然变大时，噪声底要十几秒才能跟上，这段时间里切得偏少。
"""

from collections import deque
from typing import List

import numpy as np


class PauseSplitter:
    """
    一次流式任务的切句状态（每个任务一个实例）

    feed() 喂入新到的音频，返回切点；调用方按返回值依次从自己的缓冲开头切下各句。
    """

    SAMPLE_RATE = 16000
    FRAME = 800                     # 50ms
    FRAME_SEC = FRAME / SAMPLE_RATE

    DELTA_DB = 15.0                 # 帧能量高于噪声底多少 dB 算语音
    NOISE_WIN = 100                 # 噪声底估计窗口：最近 5s 的帧
    NOISE_PCT = 20                  # 窗口内取第 20 百分位作噪声底
    NOISE_RISE = 0.05               # 噪声底每帧最多上升 0.05dB（每秒 1dB），下降不限；原因见文件头
    FLOOR_DB = -80.0                # 帧能量下限：数字静音（约 -97dB）会把噪声底拉得过低

    MIN_SIL = 0.6                   # 停顿多长才切
    # 一句话说得很长时逐级放宽停顿要求，免得一直不上屏
    LONG1, MIN_SIL_LONG1 = 12.0, 0.4
    LONG2, MIN_SIL_LONG2 = 20.0, 0.25
    MAX_SEG = 40.0                  # 硬上限：始终没有停顿也在这里切（窗口音频 + 前文要放得进模型上下文）

    FIRST_MIN = 2.0                 # 第一句最短时长：让开头的角色名和后面的话落在同一句里
    MIN_SEG = 1.0                   # 之后每句最短时长
    MIN_SPEECH_FRAMES = 4           # 一句里至少这么多帧语音才算有内容、才会在停顿处切

    def __init__(self):
        self._rest = np.zeros(0, dtype=np.float32)        # 不足一帧的尾巴
        self._db = deque(maxlen=self.NOISE_WIN + 1)       # 最近各帧能量（跨句连续）
        self._noise = None                                # 上一帧的噪声底（跨句连续）
        self._seg_frames = 0
        self._sil_run = 0
        self._speech_frames = 0
        self.cuts = 0                                     # 已切出的句数

    @property
    def tail_has_speech(self) -> bool:
        """当前这句（上一个切点之后）是否检测到了语音"""
        return self._speech_frames >= self.MIN_SPEECH_FRAMES

    def feed(self, samples: np.ndarray) -> List[tuple]:
        """
        喂入新收到的音频

        Returns:
            本次产生的切点 [(句长采样数, 是否有语音)]。句长从上一个切点（或任务开头）算起，
            一次喂入可能产生多个切点，调用方要按顺序逐个切。
        """
        x = np.concatenate([self._rest, samples]) if len(self._rest) else samples
        n = len(x) // self.FRAME
        self._rest = x[n * self.FRAME:].copy()
        if n == 0:
            return []

        frames = x[:n * self.FRAME].reshape(n, self.FRAME).astype(np.float64)
        dbs = 10 * np.log10(np.mean(frames * frames, axis=1) + 1e-10)
        cuts = []
        for e in np.maximum(dbs, self.FLOOR_DB):
            self._db.append(e)
            noise = np.percentile(self._db, self.NOISE_PCT)
            if self._noise is not None:
                noise = min(noise, self._noise + self.NOISE_RISE)
            self._noise = noise
            if e > noise + self.DELTA_DB:
                self._sil_run = 0
                self._speech_frames += 1
            else:
                self._sil_run += 1
            self._seg_frames += 1

            seg_len = self._seg_frames * self.FRAME_SEC
            min_len = self.FIRST_MIN if self.cuts == 0 else self.MIN_SEG
            if seg_len < self.LONG1:
                need = self.MIN_SIL
            elif seg_len < self.LONG2:
                need = self.MIN_SIL_LONG1
            else:
                need = self.MIN_SIL_LONG2

            paused = (self.tail_has_speech and seg_len >= min_len
                      and self._sil_run * self.FRAME_SEC >= need)
            if paused or seg_len >= self.MAX_SEG:
                cuts.append((self._seg_frames * self.FRAME, self.tail_has_speech))
                self.cuts += 1
                self._seg_frames = 0
                self._sil_run = 0
                self._speech_frames = 0
        return cuts
