"""Markdown 友好的分段与截断。

QQ 单条消息有长度上限，直接按字符数切会把代码块、表格、标题切坏。
这里的策略：
- 优先在空行（段落）切，其次换行，最后句末标点
- 切在围栏代码块（``` / ~~~）内时，当前段补结束围栏，下一段补同语言开始围栏
- 硬截断时把未闭合的围栏代码块补全
"""

from __future__ import annotations

from dataclasses import dataclass

# 段落 > 行 > 句末 > 分句
_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", "，", ",")


@dataclass
class FenceState:
    """围栏代码块的打开状态。"""

    marker: str = ""  # 例如 ``` 或 ~~~~
    info: str = ""  # 语言标识，如 python

    @property
    def open(self) -> bool:
        return bool(self.marker)


def fence_state(text: str) -> FenceState:
    """扫描文本，返回末尾是否仍处于围栏代码块中。"""
    state = FenceState()
    for line in (text or "").splitlines():
        stripped = line.lstrip()
        if not stripped or stripped[0] not in "`~":
            continue
        char = stripped[0]
        run = len(stripped) - len(stripped.lstrip(char))
        if run < 3:
            continue
        if not state.open:
            state.marker = char * run
            state.info = stripped[run:].strip()
        elif char == state.marker[0] and run >= len(state.marker):
            state.marker = ""
            state.info = ""
    return state


def close_fences(text: str) -> str:
    """补全末尾未闭合的围栏代码块。"""
    state = fence_state(text)
    if not state.open:
        return text
    suffix = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{suffix}{state.marker}"


def _pick_cut(text: str, limit: int) -> int:
    """在 limit 以内尽量靠后地找结构边界。"""
    if limit <= 0:
        return 0
    floor = max(1, limit // 2)
    for sep in _SEPARATORS:
        pos = text.rfind(sep, floor, limit)
        if pos >= floor:
            return pos + len(sep)
    return limit


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _table_block_start(text: str, cut: int) -> int:
    """cut 落在表格行内时返回表格块起始位置，否则返回 -1。"""
    line_start = text.rfind("\n", 0, cut) + 1
    current = text[line_start:cut]
    if current:
        if not _is_table_line(current):
            return -1
        start = line_start
    else:
        # cut 正好在行首：只有下一行仍是表格行，才算切在表格中间
        next_end = text.find("\n", line_start)
        next_line = text[line_start:] if next_end < 0 else text[line_start:next_end]
        if not _is_table_line(next_line):
            return -1
        start = line_start

    while start > 0:
        prev_end = start - 1
        prev_start = text.rfind("\n", 0, prev_end) + 1
        if not _is_table_line(text[prev_start:prev_end]):
            break
        start = prev_start
    return start


def _table_header(text: str, start: int) -> str:
    """返回表格头两行（标题行 + 分隔行）；不是合法表格时返回空字符串。"""
    first_end = text.find("\n", start)
    if first_end < 0:
        return ""
    second_start = first_end + 1
    second_end = text.find("\n", second_start)
    if second_end < 0:
        return ""
    second = text[second_start:second_end].strip()
    if "|" not in second or "-" not in second:
        return ""
    if any(char not in "|-: " for char in second):
        return ""
    return text[start : second_end + 1]


def split_markdown(text: str, limit: int) -> tuple[str, str]:
    """按 Markdown 结构切分，返回 (当前段, 剩余段)。

    切在代码块内时，当前段会自动补结束围栏，剩余段会带上开始围栏；
    切在表格内时，剩余段会补上表头，保证两张表都能正常渲染。
    """
    if limit <= 0:
        return "", text
    if len(text) <= limit:
        return text, ""

    probe = fence_state(text[:limit])
    margin = len(probe.marker) + 2 if probe.open else 0
    cut = _pick_cut(text, max(1, limit - margin))
    state = fence_state(text[:cut])
    if state.open:
        head = text[:cut]
        close = state.marker if head.endswith("\n") else f"\n{state.marker}"
        opened = (
            f"{state.marker}{state.info}\n" if state.info else f"{state.marker}\n"
        )
        return f"{head}{close}", f"{opened}{text[cut:]}"

    table_start = _table_block_start(text, cut)
    if table_start >= 0:
        if not text[:cut].endswith("\n"):
            prev = text.rfind("\n", 0, cut)
            if prev > 0:
                cut = prev + 1
        header = _table_header(text, table_start) if table_start > 0 else ""
        return text[:cut], f"{header}{text[cut:]}"

    return text[:cut], text[cut:]


def split_markdown_chunks(text: str, limit: int, max_chunks: int = 5) -> list[str]:
    """把文本切成若干段，每段围栏代码块都保持闭合。"""
    text = (text or "").strip()
    if not text or limit <= 0:
        return []

    chunks: list[str] = []
    remaining = text
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= limit:
            chunks.append(remaining)
            remaining = ""
            break
        chunk, remaining = split_markdown(remaining, limit)
        chunks.append(chunk)

    if remaining and chunks:
        # 最后一段仍然超限：硬截断后补全围栏，避免把消息切坏
        tail = chunks[-1][: max(1, limit - 8)].rstrip() + "…"
        chunks[-1] = close_fences(tail)
    return [chunk for chunk in chunks if chunk]
