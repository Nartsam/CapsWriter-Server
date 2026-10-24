# 分段拼接算法

长音频会被切成带重叠的片段分别识别，相邻片段的结果在重叠区里有重复内容，需要去重后拼起来。服务端同时维护两条拼接结果，都在识别子进程的 [`TaskPipeline.process`](../core/server/worker/pipeline.py) 里逐段更新：

| 结果字段 | 算法 | 用途 |
|---|---|---|
| `text` | [`merge_by_text`](../core/server/merger/text_merger.py)：纯文本对齐 | 主输出，语音输入直接用它上屏，不依赖时间戳 |
| `tokens` / `timestamps` / `text_accu` | [`merge_tokens_by_sequence_matcher`](../core/server/merger/token_merger.py)：字级对齐 | 文件转录生成字幕，需要每个字的时间戳 |

## 分段方式

分段由**服务端**完成（[`ws_recv.py`](../core/server/connection/ws_recv.py)），参数由客户端在每条消息里指定：

- 缓冲区累计到 `seg_duration + 2 × seg_overlap` 秒时，切出 `[0, seg_duration + seg_overlap)` 作为一个片段提交，缓冲区前移 `seg_duration`；
- 收到 `is_final` 时把剩余音频作为最后一段提交；
- 客户端默认 `seg_duration = 60s`、`seg_overlap = 4s`，所以 60 秒以内的语音输入只有一个片段，不触发拼接；
- 注意 Qwen3-ASR 适配器会把单段音频截断到 `Qwen3ASRGGUFArgs.chunk_size`（80s），分段长度不要超过它。

## 文本拼接 `merge_by_text`

1. 去掉 `prev_text` 末尾和 `new_text` 开头的标点，避免标点差异干扰匹配；
2. 取 `prev` 最后 100 字（tail）与 `new` 前 100 字（head），用 `difflib.SequenceMatcher` 找出所有公共子串；
3. 候选必须满足：长度 ≥ 2，在 tail 中的终点落在后 1/4 区域之后（`a + size > len(tail) * 3/4`），在 head 中的起点落在前 1/4 区域之内（`b <= len(head) / 4`）——即重叠只能出现在前一段尾部、后一段头部；
4. 评分 `size² + a − b`：长度占主导，越靠近 tail 末尾、head 开头越优；
5. 保留 `prev` 到匹配终点，`new` 从匹配终点之后续接。前一段匹配点之后的“尾部漂移”和后一段匹配点之前的“开头截断”都会被丢弃；
6. 找不到合法候选时直接首尾相接。

## 字级拼接 `merge_tokens_by_sequence_matcher`

与文本拼接同一套匹配策略，区别在于：

- 先把新片段的相对时间戳加上 `offset` 转成全局时间；
- 比较窗口按重叠时长估算：`max(overlap × 5 字/秒, 20) × 3` 个 token；
- 匹配到的字符位置再映射回 token 下标，按下标截断/续接 tokens 与 timestamps；
- 拼接后清理连续重复的标点；找不到重叠时按时间戳兜底拼接（`_fallback_merge`：丢弃新片段里时间不晚于前段最后一个字 + 0.1s 的 token）。

最终段完成后，`TextFormatter`（ITN、中英空格）只作用于文本，[`core/tools/token_sync.py`](../core/tools/token_sync.py) 再把格式化带来的字符变化同步回 token 序列，保证 `tokens` 与 `text_accu` 一致。

时间戳来源：Qwen3-ASR 本身不输出时间戳。文件转录任务（`source == 'file'`）会调用 ForcedAligner 为每个片段生成字级时间戳；语音输入任务或对齐模型不可用时，时间戳按“总时长 / 字数”均分估算。
