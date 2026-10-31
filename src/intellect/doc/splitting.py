"""保留原文字符的确定性切分；结构块之间不跨页/跨单元格重叠。"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterator

from intellect.core.config import ChunkConfig
from intellect.doc.batches import ChunkInput
from intellect.doc.counting import ChunkTokenCounter
from intellect.doc.parsers import ParsedDocument

SPLITTER_VERSION = "source-character-v1"


def split_document(document: ParsedDocument, config: ChunkConfig, counter: ChunkTokenCounter) -> Iterator[ChunkInput]:
    """执行 `split_document` 对应的功能并返回处理结果。 参数包括：`document`、`config`、`counter`。"""
    line_ends = [match.end() for match in re.finditer(r"\r\n|\r|\n", document.content)]
    index = 0
    for block in document.blocks:
        start, end = block.start, block.start + len(block.text)
        while start < end:
            stop = min(start + config.chunk_size, end)
            if stop < end:
                for separator in config.separators:
                    if not separator:
                        break
                    position = document.content.rfind(separator, start, stop)
                    candidate = position + len(separator)
                    if position >= start and candidate - start > config.chunk_overlap:
                        stop = candidate
                        break
            text = document.content[start:stop]
            location = {
                **block.location, "text_start": start, "text_end": stop,
                "offset_unit": "unicode-codepoints",
                "line_start": bisect_right(line_ends, start) + 1,
                "line_end": bisect_right(line_ends, stop - 1) + 1,
            }
            yield ChunkInput(index, text, counter.count(text), location)
            index += 1
            start = stop - config.chunk_overlap if stop < end else end
