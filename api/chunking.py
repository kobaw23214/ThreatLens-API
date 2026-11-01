"""
long-text-chunking-ner 分块服务模块

职责:
  - 按 GLiNER 模型真实 tokenizer 精确计算 labels 与 chunk 文本的 token 数
  - 按句优先 (中英混排) → 字符级二次切分 → 字符级再切分的递归策略 (最多 3 层)
  - 生成相邻 Chunk 的 overlap 字符重叠,避免实体被切在边界
  - 产出 (List[Chunk], ChunkMetadata) 供上层做 offset 回写与持久化

所有 token 数均使用 tokenizer.encode(text, add_special_tokens=False) 精确计算,
不使用字符数估算。tokens <= effective_max_tokens 是整个模块的核心不变量。

对应 design.md:
  - Components and Interfaces § 1 (ChunkingService)
  - Data Models § 2 (Chunk / ChunkConfig / ChunkingCapacityError / ChunkingError)
  - Correctness Properties P1 / P2 / P3 / P4 / P5 / P6 / P12
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─── 常量 ────────────────────────────────────────────────────────────────────

# GLiNER prompt 约定: 每个 label 在送入模型前被包裹为 <<ENT>>label<<SEP>>
# 参考: https://github.com/urchade/GLiNER 源码中 data_processor 的 prompt 模板
ENT_TAG = "<<ENT>>"
SEP_TAG = "<<SEP>>"

# 分句阶段使用的句边界字符
CHINESE_BOUNDARIES = "。？！；…"
ENGLISH_BOUNDARIES = ".?!;"
NEWLINE_BOUNDARIES = "\n\r"

# 英文缩写白名单 (Requirement 10.2); 匹配时转小写,大小写不敏感
# 只列最常见,命中后仅在 "." 后不接换行且不接大写开头新词时抑制断句
ENGLISH_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof",
    "e.g", "i.e", "u.s", "u.k", "etc", "vs",
    "st", "jr", "sr", "no",
})

# effective_max_tokens 的下限: 低于该值视为模型窗口过小,不执行分块 (Requirement 2.6)
MIN_EFFECTIVE_TOKENS = 50

# 递归切分最大层级 (Requirement 1.4): 句级 → 字符切分 → 字符再切分
MAX_SPLIT_DEPTH = 3


# ─── 异常 ────────────────────────────────────────────────────────────────────

class ChunkingCapacityError(Exception):
    """
    effective_max_tokens <= 0,模型容量配置异常。
    典型触发:labels 过长占满 model_max_tokens,或 safety_margin 过大。
    对应 Requirement 1.11 / 2.6。
    """

    def __init__(self, effective_max_tokens: int, model_max_tokens: int,
                 labels_token_count: int, safety_margin: int):
        self.effective_max_tokens = effective_max_tokens
        self.model_max_tokens = model_max_tokens
        self.labels_token_count = labels_token_count
        self.safety_margin = safety_margin
        super().__init__(
            f"effective_max_tokens={effective_max_tokens} <= 0 "
            f"(model_max_tokens={model_max_tokens}, "
            f"labels_token_count={labels_token_count}, "
            f"safety_margin={safety_margin}); "
            f"labels 过长或 safety_margin 过大,模型容量不足以执行分块。"
        )


class ChunkingError(Exception):
    """其他分块失败:递归层级超过 MAX_SPLIT_DEPTH、或字符级切分仍无法满足上限。"""


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    """
    一次切分产生的文本片段。

    字段语义:
      chunk_index  : 0 起按文本顺序单调递增的块号
      char_start   : 在原始输入文本中的起始字符偏移 (闭)
      char_end     : 在原始输入文本中的结束字符偏移 (开)
      text         : 原文切片,严格满足 input_text[char_start:char_end] == text
      token_count  : 使用模型真实 tokenizer 编码后的 token 序列长度 (不含 special tokens)
      status       : "done" / "failed",推理阶段由上层回写
      error        : status=="failed" 时的错误摘要 (<= 200 字符)
    """
    chunk_index: int
    char_start: int
    char_end: int
    text: str
    token_count: int
    status: str = "done"
    error: Optional[str] = None

    @property
    def char_length(self) -> int:
        return self.char_end - self.char_start

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 ChunkInfo 兼容的 dict (用于 chunk_metadata.chunks[])。"""
        return {
            "chunk_index": self.chunk_index,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "char_length": self.char_length,
            "token_count": self.token_count,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class ChunkConfig:
    """
    一次请求的分块配置。由 main.py::build_chunk_cfg 合并 env 默认值与 ExtractRequest 可选字段产生。
    """
    enabled: bool = True
    overlap: int = 32           # 相邻 Chunk 的字符重叠数
    safety_margin: int = 16     # effective 从 (model_max - labels) 再扣除的 token 余量
    threshold: float = 0.5      # 送入 model.predict_entities 的 threshold

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChunkMetadata:
    """
    一次请求的全部分块元数据,落库到 task_results.chunk_metadata 并通过 API 透出。
    """
    chunking_enabled: bool
    model_max_tokens: Optional[int]
    labels_token_count: Optional[int]
    effective_max_tokens: Optional[int]
    overlap: int
    safety_margin: int
    threshold: float
    chunk_count: int
    chunks: List[Chunk] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunking_enabled": self.chunking_enabled,
            "model_max_tokens": self.model_max_tokens,
            "labels_token_count": self.labels_token_count,
            "effective_max_tokens": self.effective_max_tokens,
            "overlap": self.overlap,
            "safety_margin": self.safety_margin,
            "threshold": self.threshold,
            "chunk_count": self.chunk_count,
            "chunks": [c.to_dict() for c in self.chunks],
        }


# ─── 分句工具 ────────────────────────────────────────────────────────────────

def _ends_with_abbreviation(text: str, dot_pos: int) -> bool:
    """
    判断 text[dot_pos] == '.' 前是否构成一个英文缩写。

    支持两类:
      - 单词缩写:   Mr / Dr / Prof / etc / vs  (dot_pos 前是字母,追溯连续字母到空格/起点)
      - 多点缩写:   e.g. / i.e. / U.S. / U.K.  (dot_pos 前也是字母,会被追溯为 "u.s" / "e.g" 等)

    追溯方式:从 dot_pos-1 往前,把连续的 [A-Za-z.] 取下来,小写后去掉首尾点号,匹配白名单。
    """
    if dot_pos <= 0 or text[dot_pos] != ".":
        return False

    # 向前取 [A-Za-z.] 字符,形成候选缩写串
    start = dot_pos
    while start > 0 and (text[start - 1].isalpha() or text[start - 1] == "."):
        start -= 1
    candidate = text[start:dot_pos].lower().strip(".")
    if not candidate:
        return False
    return candidate in ENGLISH_ABBREVIATIONS


def _is_middle_dot_of_multi_dot_abbr(text: str, dot_pos: int) -> bool:
    """
    多点缩写中的"中间点"检测。

    例如 `e.g.` 的第一个 `.` (dot_pos 指向第一个点),向后看到 `g` 再跟 `.`,
    形成 `x.y.` 模式,则当前点是多点缩写的一部分,不应该断。

    条件: text[dot_pos+1] 是单个字母(非空白非数字),text[dot_pos+2] 是 `.`。
    """
    if dot_pos >= len(text) - 2:
        return False
    next_ch = text[dot_pos + 1]
    dot_ch = text[dot_pos + 2]
    return next_ch.isalpha() and dot_ch == "."


def _followed_by_digit(text: str, dot_pos: int) -> bool:
    """
    英文版本号保护:text[dot_pos] == '.' 且紧邻下一个非空白字符是数字 → 不断句。
    """
    j = dot_pos + 1
    if j >= len(text):
        return False
    return text[j].isdigit()


def _next_non_space_is_newline(text: str, dot_pos: int) -> bool:
    """
    "." 之后跳过空白后第一个字符是否为换行符。
    缩写后紧跟换行视为真的句子结束,允许断句;否则视为专名/缩写内部的点,不断句。
    文末也视为真实结束。
    """
    j = dot_pos + 1
    while j < len(text) and text[j] in " \t":
        j += 1
    if j >= len(text):
        return True  # 文末,按结束处理
    return text[j] in NEWLINE_BOUNDARIES


# ─── 核心服务 ────────────────────────────────────────────────────────────────

class ChunkingService:
    """
    文本分块服务。线程安全性与无状态性:
      - 实例持有 tokenizer / 配置,split() 方法本身不修改实例状态,可在同一模型的多个
        请求之间复用 (但每次请求通常在 predict_with_chunks 内新建一个 ChunkingService,
        以简化并发推理时的封装)。
    """

    def __init__(
        self,
        tokenizer: Any,
        model_max_tokens: int,
        safety_margin: int = 16,
        overlap: int = 32,
    ):
        if tokenizer is None:
            raise ValueError("ChunkingService 需要一个真实的 tokenizer")
        if model_max_tokens <= 0:
            raise ValueError(f"model_max_tokens 必须 > 0,收到 {model_max_tokens}")
        if safety_margin < 0:
            raise ValueError(f"safety_margin 必须 >= 0,收到 {safety_margin}")
        if overlap < 0:
            raise ValueError(f"overlap 必须 >= 0,收到 {overlap}")

        self.tokenizer = tokenizer
        self.model_max_tokens = int(model_max_tokens)
        self.safety_margin = int(safety_margin)
        self.overlap = int(overlap)

    # ─── tokenizer 工具 ────────────────────────────────────────────────────

    def _count_tokens(self, text: str) -> int:
        """
        使用真实 tokenizer 对纯文本编码,返回 token 序列长度。
        add_special_tokens=False 对齐 GLiNER predict_entities 内部的文本编码口径。
        """
        if not text:
            return 0
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def compute_labels_token_count(self, labels: List[str]) -> int:
        """
        按 GLiNER prompt 约定: <<ENT>>label1<<SEP>><<ENT>>label2<<SEP>>...<<ENT>>labelN<<SEP>>
        用真实 tokenizer encode 并返回 token 总数。

        labels 为空时返回 0。
        """
        if not labels:
            return 0
        prompt = "".join(f"{ENT_TAG}{lb}{SEP_TAG}" for lb in labels)
        return self._count_tokens(prompt)

    def compute_effective_max_tokens(self, labels: List[str]) -> Tuple[int, int]:
        """
        返回 (effective_max_tokens, labels_token_count)。
        公式: effective = model_max_tokens − labels_token_count − safety_margin
        """
        labels_tok = self.compute_labels_token_count(labels)
        effective = self.model_max_tokens - labels_tok - self.safety_margin
        return effective, labels_tok

    # ─── 分句 ──────────────────────────────────────────────────────────────

    def _sentence_split(self, text: str) -> List[Tuple[int, int, str]]:
        """
        中英混合分句。返回 [(char_start, char_end, sent_text)],char_end 为开区间。

        规则 (Requirement 10):
          1. 断句符: 中文 。？！；… / 英文 .?!; / 换行 \\n\\r\\n
          2. 英文句号后紧跟数字(版本号)→ 不断句
          3. 英文缩写白名单 (Mr./Dr./e.g./i.e./U.S./etc./...) 后非换行且非大写开头新词 → 不断句
          4. 连续标点合并 (!!!  。。。  ?!  。\\n) 视为单一边界,避免产生空 Chunk
          5. 无任何断句符 → 返回空列表,由上层退化到字符切分
        """
        if not text:
            return []

        sentences: List[Tuple[int, int, str]] = []
        n = len(text)
        sent_start = 0
        i = 0
        found_boundary = False  # 退化判定: 是否命中过真实断句符
        while i < n:
            ch = text[i]
            is_boundary = False

            if ch in CHINESE_BOUNDARIES or ch in NEWLINE_BOUNDARIES:
                is_boundary = True
            elif ch in ENGLISH_BOUNDARIES:
                if ch == ".":
                    # 版本号保护: `.` 后紧邻数字 (1.2.3)
                    if _followed_by_digit(text, i):
                        is_boundary = False
                    # 多点缩写中间点: `e.g.` 的第一个 `.` 前瞻到 `g.`
                    elif _is_middle_dot_of_multi_dot_abbr(text, i):
                        is_boundary = False
                    # 缩写保护: Mr./Dr./e.g./i.e./U.S./etc. 后非换行 → 不断
                    elif _ends_with_abbreviation(text, i) and \
                            not _next_non_space_is_newline(text, i):
                        is_boundary = False
                    else:
                        is_boundary = True
                else:
                    is_boundary = True

            if is_boundary:
                found_boundary = True
                # 合并连续的断句符与空白 → 单一边界
                j = i + 1
                while j < n and (
                    text[j] in CHINESE_BOUNDARIES
                    or text[j] in ENGLISH_BOUNDARIES
                    or text[j] in NEWLINE_BOUNDARIES
                    or text[j] in " \t"
                ):
                    # 对合并后的英文 "." 也需要保护一次,避免把 "1.2" 当成两个边界
                    if text[j] == "." and _followed_by_digit(text, j):
                        break
                    j += 1
                sent_end = j
                sent_text = text[sent_start:sent_end]
                if sent_text:  # 跳过纯空白片段
                    sentences.append((sent_start, sent_end, sent_text))
                sent_start = sent_end
                i = sent_end
            else:
                i += 1

        # 处理尾部残留 (最后一句不以断句符结束)
        if sent_start < n:
            tail = text[sent_start:n]
            sentences.append((sent_start, n, tail))

        # Requirement 10.5: 若扫描过程完全没有命中任何真实断句符,视为"纯连续字符",
        # 返回空列表,由上层触发字符切分退化路径。
        if not found_boundary:
            return []

        return sentences

    # ─── 字符级切分 ────────────────────────────────────────────────────────

    def _find_max_end(self, text: str, start: int, target_tokens: int) -> int:
        """
        找到最大的 end,使得 _count_tokens(text[start:end]) <= target_tokens。

        策略:先以指数步长探到 end 使得 tokens > target,再二分。
        """
        n = len(text)
        if start >= n:
            return start
        if target_tokens <= 0:
            return start + 1  # 极端情况:给出最小可能的切片,由上层再判定是否失败

        # 指数探测
        step = 256
        end = min(start + step, n)
        while end < n and self._count_tokens(text[start:end]) <= target_tokens:
            step *= 2
            end = min(start + step, n)

        # 若整段都不超限,直接返回
        if end == n and self._count_tokens(text[start:end]) <= target_tokens:
            return n

        # 二分: 在 [start+1, end] 中找最大 j 使 tokens(text[start:j]) <= target
        lo, hi = start + 1, end
        while lo < hi:
            mid = (lo + hi + 1) // 2  # 向上取整,避免 lo==hi-1 时死循环
            if self._count_tokens(text[start:mid]) <= target_tokens:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _chunk_by_chars(
        self,
        text: str,
        base_offset: int,
        target_tokens: int,
        overlap: int,
    ) -> List[Chunk]:
        """
        按字符切分 + overlap 重叠。base_offset 为 text 在原文中的起始偏移,
        返回的 Chunk 的 char_start / char_end 已经加上 base_offset。

        每个产出的 Chunk 用 tokenizer 重新校验 token_count <= target_tokens。
        """
        if not text:
            return []
        if target_tokens <= 0:
            raise ChunkingError(
                f"_chunk_by_chars: target_tokens={target_tokens} <= 0,无法切分"
            )

        chunks: List[Chunk] = []
        n = len(text)
        i = 0
        while i < n:
            j = self._find_max_end(text, i, target_tokens)
            if j <= i:
                # 单字符都超限:极端情况,直接取 1 char 继续,避免死循环
                j = i + 1

            piece = text[i:j]
            piece_tokens = self._count_tokens(piece)
            chunks.append(Chunk(
                chunk_index=-1,  # 由 split() 统一赋值
                char_start=base_offset + i,
                char_end=base_offset + j,
                text=piece,
                token_count=piece_tokens,
            ))

            if j >= n:
                break
            # 下一块的起点: j - overlap,overlap 不超过当前块字符长度一半
            step_overlap = min(overlap, (j - i) // 2)
            next_i = max(i + 1, j - step_overlap)
            i = next_i

        return chunks

    # ─── 句级贪心打包 + 递归 ───────────────────────────────────────────────

    def _pack_sentences(
        self,
        text: str,
        sentences: List[Tuple[int, int, str]],
        effective_max_tokens: int,
        overlap: int,
        depth: int = 0,
    ) -> List[Chunk]:
        """
        贪心把多个句子合并成一个 Chunk,直到再加一句就会超限。

        单句超长 → 递归下探到字符切分 (depth+1)。
        递归层级 >= MAX_SPLIT_DEPTH 仍超限 → 抛 ChunkingError。
        """
        if depth >= MAX_SPLIT_DEPTH:
            raise ChunkingError(
                f"_pack_sentences: 递归层级超过 {MAX_SPLIT_DEPTH},分块失败"
            )

        chunks: List[Chunk] = []
        n = len(sentences)
        i = 0
        while i < n:
            s_start, s_end, s_text = sentences[i]
            s_tokens = self._count_tokens(s_text)

            if s_tokens > effective_max_tokens:
                # 单句超长:对该句调用字符切分 (depth+1)
                char_chunks = self._chunk_by_chars(
                    s_text, s_start, effective_max_tokens, overlap
                )
                # 字符切分的结果也要满足 token 上限;若不满足,再递归一层
                for cc in char_chunks:
                    if cc.token_count > effective_max_tokens:
                        if depth + 2 >= MAX_SPLIT_DEPTH:
                            raise ChunkingError(
                                f"字符级切分后 token_count={cc.token_count} "
                                f"仍 > effective_max_tokens={effective_max_tokens},"
                                f"且已达最大递归层级 {MAX_SPLIT_DEPTH}"
                            )
                        # 再次用更小 target 切分
                        sub_chunks = self._chunk_by_chars(
                            cc.text, cc.char_start, effective_max_tokens, overlap
                        )
                        chunks.extend(sub_chunks)
                    else:
                        chunks.append(cc)
                i += 1
                continue

            # 贪心扩展:把下一句并入,只要不超限
            buf_start = s_start
            buf_end = s_end
            j = i + 1
            while j < n:
                next_start, next_end, _ = sentences[j]
                tentative = text[buf_start:next_end]
                if self._count_tokens(tentative) > effective_max_tokens:
                    break
                buf_end = next_end
                j += 1

            chunk_text = text[buf_start:buf_end]
            chunk_tokens = self._count_tokens(chunk_text)
            chunks.append(Chunk(
                chunk_index=-1,
                char_start=buf_start,
                char_end=buf_end,
                text=chunk_text,
                token_count=chunk_tokens,
            ))
            i = j

        return chunks

    def _apply_overlap(
        self,
        text: str,
        chunks: List[Chunk],
        overlap: int,
        effective_max_tokens: int,
    ) -> None:
        """
        对已有的 chunks 按字符层面扩展 char_start 向前,制造 overlap 区域。

        约束 (Requirement 1.7):
          - overlap 字符数不超过当前 Chunk 字符长度的一半 (也不超过前一块长度的一半)
          - 扩展后 token_count 必须仍 <= effective_max_tokens,否则二分缩小 overlap
        就地修改 chunks[k] 的 char_start / text / token_count。
        """
        if overlap <= 0 or len(chunks) < 2:
            return

        for k in range(1, len(chunks)):
            prev = chunks[k - 1]
            cur = chunks[k]

            # 最大可接受的 overlap: 不超过配置值、不超过当前块一半、不超过前一块一半、不超过前一块实际可用字符
            max_allowed = min(
                overlap,
                cur.char_length // 2,
                prev.char_length // 2,
                cur.char_start - prev.char_start,  # 不能退到前一块的起点
            )
            if max_allowed <= 0:
                continue

            # 先尝试直接用 max_allowed 扩展,若 token 超限再二分缩小
            target_start = cur.char_start - max_allowed
            extended_text = text[target_start:cur.char_end]
            extended_tokens = self._count_tokens(extended_text)

            if extended_tokens <= effective_max_tokens:
                cur.char_start = target_start
                cur.text = extended_text
                cur.token_count = extended_tokens
                continue

            # 二分: 找到最大的 delta 使得 tokens(text[cur.char_start - delta:cur.char_end]) <= max
            lo, hi = 0, max_allowed
            best = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                cand_start = cur.char_start - mid
                cand_tokens = self._count_tokens(text[cand_start:cur.char_end])
                if cand_tokens <= effective_max_tokens:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1

            if best > 0:
                cur.char_start = cur.char_start - best
                cur.text = text[cur.char_start:cur.char_end]
                cur.token_count = self._count_tokens(cur.text)
            # best==0 时无可扩展空间,保持原样

    # ─── 主入口 ────────────────────────────────────────────────────────────

    def split(self, text: str, labels: List[str]) -> Tuple[List[Chunk], ChunkMetadata]:
        """
        分块主入口。决策流程:
          1. 空文本 → 返回 ([], chunk_count=0, chunking_enabled=False)
          2. effective <= 0 → 抛 ChunkingCapacityError
          3. effective < MIN_EFFECTIVE_TOKENS (50) → 返回单 Chunk + chunking_enabled=False 并打 warning
          4. 全文 tokens <= effective → 返回单 Chunk (Requirement 1.9)
          5. 否则 → 分句 → 贪心打包 → (超长时) 字符切分 → 应用 overlap
          6. 最终对每个 Chunk 用 tokenizer 再次校验 token_count
        """
        effective, labels_tok = self.compute_effective_max_tokens(labels)

        # ── 1. 空文本 ─────────────────────────────────────────────────
        if not text:
            return [], ChunkMetadata(
                chunking_enabled=False,
                model_max_tokens=self.model_max_tokens,
                labels_token_count=labels_tok,
                effective_max_tokens=effective,
                overlap=self.overlap,
                safety_margin=self.safety_margin,
                threshold=0.0,  # 由上层覆盖为真实 threshold
                chunk_count=0,
                chunks=[],
            )

        # ── 2. effective_max_tokens <= 0: 模型容量异常 (Req 1.11 / 2.6) ─
        if effective <= 0:
            raise ChunkingCapacityError(
                effective_max_tokens=effective,
                model_max_tokens=self.model_max_tokens,
                labels_token_count=labels_tok,
                safety_margin=self.safety_margin,
            )

        # ── 3. effective < 50: 窗口过小,按不分块降级 (Req 2.6) ─────────
        if effective < MIN_EFFECTIVE_TOKENS:
            logger.warning(
                "chunking skipped: effective_max_tokens too small (%s)", effective
            )
            full_tokens = self._count_tokens(text)
            return (
                [Chunk(
                    chunk_index=0,
                    char_start=0,
                    char_end=len(text),
                    text=text,
                    token_count=full_tokens,
                )],
                ChunkMetadata(
                    chunking_enabled=False,
                    model_max_tokens=self.model_max_tokens,
                    labels_token_count=labels_tok,
                    effective_max_tokens=effective,
                    overlap=self.overlap,
                    safety_margin=self.safety_margin,
                    threshold=0.0,
                    chunk_count=1,
                    chunks=[Chunk(
                        chunk_index=0,
                        char_start=0,
                        char_end=len(text),
                        text=text,
                        token_count=full_tokens,
                    )],
                ),
            )

        # ── 4. 全文 tokens <= effective: 单块即可 (Req 1.9) ────────────
        full_tokens = self._count_tokens(text)
        if full_tokens <= effective:
            only = Chunk(
                chunk_index=0,
                char_start=0,
                char_end=len(text),
                text=text,
                token_count=full_tokens,
            )
            return [only], ChunkMetadata(
                chunking_enabled=True,
                model_max_tokens=self.model_max_tokens,
                labels_token_count=labels_tok,
                effective_max_tokens=effective,
                overlap=self.overlap,
                safety_margin=self.safety_margin,
                threshold=0.0,
                chunk_count=1,
                chunks=[only],
            )

        # ── 5. 分句 → 贪心打包 ───────────────────────────────────────
        sentences = self._sentence_split(text)
        if not sentences:
            # Req 10.5: 无任何断句符,退化到字符切分
            chunks = self._chunk_by_chars(text, 0, effective, self.overlap)
        else:
            chunks = self._pack_sentences(
                text, sentences, effective, self.overlap, depth=0
            )

        # ── 应用 overlap ─────────────────────────────────────────────
        self._apply_overlap(text, chunks, self.overlap, effective)

        # ── 赋 chunk_index ────────────────────────────────────────────
        for idx, ch in enumerate(chunks):
            ch.chunk_index = idx

        # ── 6. 最终校验: 每个 Chunk 必须满足 token_count <= effective ──
        for ch in chunks:
            recount = self._count_tokens(ch.text)
            if recount > effective:
                raise ChunkingError(
                    f"final validation failed: chunk_index={ch.chunk_index}, "
                    f"token_count={recount} > effective_max_tokens={effective}"
                )
            ch.token_count = recount

        return chunks, ChunkMetadata(
            chunking_enabled=True,
            model_max_tokens=self.model_max_tokens,
            labels_token_count=labels_tok,
            effective_max_tokens=effective,
            overlap=self.overlap,
            safety_margin=self.safety_margin,
            threshold=0.0,
            chunk_count=len(chunks),
            chunks=chunks,
        )
