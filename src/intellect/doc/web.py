"""显式归档 WEB 响应字节；解析/重试只使用归档，不重新访问远端。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from intellect.core.config import get_settings
from intellect.doc.hashing import file_hash
from intellect.doc.parsers import DocumentParseError


@dataclass(frozen=True)
class WebArchive:
    path: Path
    source_url: str
    encoding: str
    file_hash: str


def archive_web(url: str, destination: str | Path) -> WebArchive:
    """执行 `archive_web` 对应的功能并返回处理结果。 参数包括：`url`、`destination`。"""
    if urlparse(url).scheme.lower() not in {"http", "https"}:
        raise DocumentParseError("WEB 归档只支持 HTTP/HTTPS")
    path = Path(destination).resolve()
    if path.exists():
        raise DocumentParseError("归档路径已存在，禁止覆盖原始文件")
    request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "intellect/0.1"})
    try:
        with urlopen(request, timeout=get_settings().document.web_timeout_seconds) as response:
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise DocumentParseError("服务器未返回可直接归档解析的 HTML 字节")
            data = response.read()
            encoding = response.headers.get_content_charset() or get_settings().document.text_encoding
            final_url = response.geturl()
            if urlparse(final_url).scheme.lower() not in {"http", "https"}:
                raise DocumentParseError("WEB 重定向目标不是 HTTP/HTTPS")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as archive:
            archive.write(data)
    except DocumentParseError:
        raise
    except (OSError, ValueError) as exc:
        raise DocumentParseError("WEB 归档失败") from exc
    return WebArchive(path, final_url, encoding, file_hash(data))
