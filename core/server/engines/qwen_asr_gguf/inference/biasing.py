# coding=utf-8
"""
解码干预：弱逗号只删不换、服务端热词（词级先验）

asr.py 的 _decode 每生成一个 token 前调用 DecodeBias.step：看一眼 llama.cpp 给出的 logits，
必要时就地修改，再交给原生采样器；热词被采纳时直接给出整个词的 token。
2026-10 用客户端存下的 249 段真实口述录音（约 50 分钟，贪心解码）做的实验数据如下。

弱逗号只删不换（comma_drop_margin）
    Qwen3-ASR 按停顿加标点，口述时边想边说，逗号比书面文字多。某一步最强的标点是中文逗号，
    且它比最可能的非标点 token 强不到 margin（logit 差）时，这一步禁止所有标点，模型直接接着写字。
    margin 0 / 4 / 6：每百字逗号 4.61 / 3.47 / 2.95，句号、问号数量不变，文字只有 1~2 段各变 1 个字；
    6 开始出现该断不断。停顿明显处的逗号余量常在 8 以上，去不掉。
    只删不换是关键：直接给逗号减分（logit bias，-4）会让约 40% 的逗号变成句号，读起来更碎。
    英文逗号不受影响（最强标点是 ',' 时不触发）。

服务端热词（hotword_file）
    不是替换，而是给整个词一个先验加分 b：热词（或它还没写出的后半截）的第一个 token 有竞争力
    （logit + b > 当前最优 token）时，在 KV Cache 上试着把整个词写完，得到它的联合 log 概率；
    加上 b 后胜过当前最优 token 才采用，否则回滚、照常采样。b 相当于「这个词比模型以为的常见 e^b 倍」，
    只有完整写出热词才加分，所以热词「立绘」不会把「例如」「利用」改成「立」。
    7 个技术词、b=4：改了 4 处，全部改对（Cloud Code→Claude Code、Git Bor→GitHub、指图→只读、MegaPOS→MegaPose）；
    b=8 开始误伤（「Git 跟踪」→「GitHub 跟踪」）。
    局限：模型几乎不认识的词翻不过来，例如「立绘」在「例会」的位置上低 16~20，需要的加分已等于机械替换；
    单字热词没有后文可验证，效果等同于给这个字加分。
    为什么不把热词拼进 context：见 docs/DEV_NOTES.md「仍然有效的决策」。
"""
import os
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import logger
from ...llama import llama

# 弱逗号规则里算作「标点」的字符：含这些字符的 token 在触发时一律禁止
_PUNCT_CHARS = '，。、？！；：,.?!;:'
_COMMA = '，'

# 热词匹配时回看的已生成文字长度（字节）
_TAIL_BYTES = 96


class DecodeBias:
    """一个识别引擎一份；begin / push / step 在每次解码中按顺序调用"""

    def __init__(self, model, comma_drop_margin: float, hotword_file: str, hotword_bonus: float):
        self.model = model
        self.comma_margin = float(comma_drop_margin or 0.0)
        self.hotword_path = hotword_file or ''
        self.default_bonus = float(hotword_bonus)
        self.n_vocab = llama.llama_vocab_n_tokens(model.vocab)

        # 词表里每个 token 的文字（不完整的 UTF-8 片段记为空串）
        self.pieces: List[str] = []
        for t in range(self.n_vocab):
            p = model.token_to_bytes(t).decode('utf-8', errors='replace')
            self.pieces.append('' if '�' in p else p)

        self.is_punct = np.array([any(c in _PUNCT_CHARS for c in p) for p in self.pieces], dtype=bool)
        self.punct_ids = np.flatnonzero(self.is_punct)
        self.is_comma = np.array([_COMMA in p for p in self.pieces], dtype=bool)

        self._piece_ids: Optional[Dict[str, List[int]]] = None
        # 已写出的热词前半截 -> [(首 token, 其余 token, 加分)]
        self._entries: Dict[str, List[Tuple[int, List[int], float]]] = {}
        self._max_done = 0
        self._mtime = None
        self._tail = b''
        self.refresh()

    @property
    def active(self) -> bool:
        return self.comma_margin > 0 or bool(self._entries)

    # ------------------------------------------------------------------ 热词文件

    def refresh(self):
        """热词文件有变化就重新加载（每次识别前调用，改完保存即生效）"""
        if not self.hotword_path:
            return
        try:
            mtime = os.stat(self.hotword_path).st_mtime
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        words = _read_hotwords(self.hotword_path, self.default_bonus) if mtime is not None else []
        self._build(words)
        logger.info(f"服务端热词：{len(words)} 个（{self.hotword_path}）")

    def _build(self, words: List[Tuple[str, float]]):
        if words and self._piece_ids is None:
            self._piece_ids = defaultdict(list)
            for t, p in enumerate(self.pieces):
                if p.strip():
                    self._piece_ids[p].append(t)
        entries = defaultdict(list)
        for word, bonus in words:
            # 英文词前面常带空格（模型输出「新对话中的 Claude Code」时 token 是 ' Claude'）
            variants = {word, ' ' + word} if word[0].isascii() else {word}
            for v in variants:
                for i in range(len(v)):
                    rest_text = v[i:]
                    for j in range(1, len(rest_text) + 1):
                        for ft in self._piece_ids.get(rest_text[:j], ()):
                            tail = self.model.tokenize(rest_text[j:]) if j < len(rest_text) else []
                            entries[v[:i]].append((ft, tail, bonus))
        self._entries = dict(entries)
        self._max_done = max((len(k) for k in self._entries), default=0)

    # ------------------------------------------------------------------ 解码

    def begin(self, prefix_text: str = ''):
        """一次解码开始；prefix_text 是 assistant 前缀（流式任务的前几句），热词可以接在它后面"""
        self._tail = prefix_text.encode('utf-8')[-_TAIL_BYTES:]

    def push(self, token: int):
        """记下已写入 KV Cache 的 token"""
        if self._entries:
            self._tail = (self._tail + self.model.token_to_bytes(token))[-_TAIL_BYTES:]

    def step(self, ctx) -> Optional[List[int]]:
        """
        在采样前处理当前 logits

        返回 None：照常用采样器采样（logits 可能已被就地修改）；
        返回 token 列表：采纳了热词，按顺序把它们当作采样结果（KV Cache 已回滚，需要正常 decode）
        """
        if self._entries:
            path = self._try_hotwords(ctx)
            if path:
                return path
        if self.comma_margin > 0:
            self._drop_weak_comma(self._logits(ctx))
        return None

    def _logits(self, ctx) -> np.ndarray:
        # 直接映射 llama.cpp 的 logits 缓冲区，修改后原生采样器读到的就是改过的值
        return np.ctypeslib.as_array(ctx.get_logits_ith(-1), shape=(self.n_vocab,))

    def _drop_weak_comma(self, logits: np.ndarray):
        p = self.punct_ids[np.argmax(logits[self.punct_ids])]
        if not self.is_comma[p]:
            return
        top = int(np.argmax(logits))
        best_text = np.max(np.where(self.is_punct, -np.inf, logits)) if self.is_punct[top] else logits[top]
        if logits[p] - best_text < self.comma_margin:
            logits[self.punct_ids] = -np.inf

    def _try_hotwords(self, ctx) -> Optional[List[int]]:
        logits = self._logits(ctx)
        top = int(np.argmax(logits))
        top_logit = float(logits[top])

        text = self._tail.decode('utf-8', errors='ignore')
        cands = []
        for n in range(min(len(text), self._max_done) + 1):
            for ft, rest, bonus in self._entries.get(text[len(text) - n:], ()):
                # 最优 token 是热词剩余部分的开头（如 ' Git' 之于 ' GitHub'）时仍要比较：
                # 模型下一步常常接别的（' Git Bor'），不会再回到热词上
                if ft == top:
                    continue
                gap = float(logits[ft]) - top_logit     # log P(ft) - log P(top)
                if gap + bonus > 0:
                    cands.append((gap + bonus, gap, ft, rest, bonus))
        if not cands:
            return None

        saved = logits.copy()
        n0 = ctx.n_past()
        taken, touched = None, False
        for _, gap, ft, rest, bonus in sorted(cands, key=lambda c: -c[0]):
            score, path = gap, [ft]
            for t in rest:
                # 试探：把热词的前一个 token 写进 KV Cache，看下一个 token 的概率
                if ctx.decode_token(path[-1]) != 0:
                    score = -np.inf
                    break
                touched = True
                lp = self._logits(ctx)
                score += float(lp[t]) - _logsumexp(lp)
                if score + bonus <= 0:
                    break
                path.append(t)
            ctx.truncate(n0)
            if score + bonus > 0 and len(path) == len(rest) + 1:
                taken = path
                break
        if touched and taken is None:
            # 试探把 logits 缓冲区覆盖了，恢复后交给采样器
            self._logits(ctx)[:] = saved
        return taken


def _logsumexp(x: np.ndarray) -> float:
    m = float(np.max(x))
    return m + float(np.log(np.sum(np.exp(x - m))))


def _read_hotwords(path: str, default_bonus: float) -> List[Tuple[str, float]]:
    """每行「词」或「词 | 加分」，# 开头为注释"""
    words, seen = [], set()
    with open(path, encoding='utf-8-sig') as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [s.strip() for s in line.split('|')]
            word, bonus = parts[0], default_bonus
            if len(parts) > 1 and parts[1]:
                try:
                    bonus = float(parts[1])
                except ValueError:
                    logger.warning(f"{path} 第 {n} 行：加分「{parts[1]}」不是数字，按默认 {default_bonus} 处理")
            if word and word not in seen:
                seen.add(word)
                words.append((word, bonus))
    return words
