"""网页搜索（必应）与网页抓取（curl / Playwright），对模型暴露为工具。"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import ipaddress
import json
import logging
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit
from xml.etree import ElementTree

import aiohttp

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)
BING_URL = "https://www.bing.com/search"
MAX_SEARCH_COUNT = 10
MAX_REDIRECTS = 5

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "使用必应搜索网页，返回 JSON（包含 title / url / snippet）。"
                "适合查询实时信息、新闻、资料链接。只返回摘要，需要正文时再用 web_fetch。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "count": {
                        "type": "integer",
                        "description": f"返回条数 1-{MAX_SEARCH_COUNT}，默认 5",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "抓取指定网页并返回 JSON（title / description / text）。"
                "优先用浏览器等待页面 JS 加载完成后再解析；"
                "未安装 Playwright 时回退到 curl 抓取原始 HTML。"
                "只允许 http/https 公网地址，不能访问内网或本机。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的完整网页 URL"},
                    "max_chars": {
                        "type": "integer",
                        "description": "正文最多返回的字符数，默认 6000",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


class WebError(RuntimeError):
    """工具参数或网络错误，会作为结果返回给模型。"""


def _squash(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", unescape(value or "")).strip()


def _strip_tags(value: str) -> str:
    return _squash(re.sub(r"<[^>]+>", " ", value or ""))


def _validate_url(url: str) -> str:
    target = (url or "").strip()
    parts = urlsplit(target)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise WebError("仅支持 http/https 公网 URL")
    host = parts.hostname.lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        raise WebError("不允许访问本机或内网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private
        or ip.is_loopback
        or ip.is_reserved
        or ip.is_link_local
        or ip.is_multicast
    ):
        raise WebError("不允许访问本机或内网地址")
    return target


def _decode_body(raw: bytes, content_type: str) -> str:
    charset = ""
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    if not charset:
        head = raw[:4096].decode("ascii", "ignore")
        match = re.search(r"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", head, re.I)
        if match:
            charset = match.group(1)
    for encoding in filter(None, (charset, "utf-8", "gb18030")):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore")


class _BingParser(HTMLParser):
    """解析必应搜索结果页中的 <li class="b_algo"> 块。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._in_algo = False
        self._li_depth = 0
        self._current: dict | None = None
        self._field: str | None = None
        self._field_tag: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = values.get("class", "").split()
        if tag == "li":
            if self._in_algo:
                self._li_depth += 1
            elif "b_algo" in classes:
                self._in_algo = True
                self._li_depth = 1
                self._current = {"title": "", "url": "", "snippet": ""}
                return
        if not self._in_algo or self._current is None:
            return

        if tag == "h2":
            self._set_field("title", "h2")
        elif tag == "a" and self._field == "title":
            href = values.get("href", "")
            if href and not self._current["url"]:
                self._current["url"] = _unwrap_bing_url(urljoin(BING_URL, href))
        elif tag == "p" and not self._field:
            self._set_field("snippet", "p")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_algo:
            return
        if tag == self._field_tag:
            self._flush_field()
        if tag == "li":
            self._li_depth -= 1
            if self._li_depth <= 0:
                self._finish_current()

    def handle_data(self, data: str) -> None:
        if self._in_algo and self._field:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        if self._in_algo:
            self._finish_current()

    def _set_field(self, name: str, tag: str) -> None:
        self._flush_field()
        self._field = name
        self._field_tag = tag
        self._buf = []

    def _flush_field(self) -> None:
        if self._field and self._current is not None:
            text = _squash("".join(self._buf))
            if text and not self._current.get(self._field):
                self._current[self._field] = text
        self._field = None
        self._field_tag = None
        self._buf = []

    def _finish_current(self) -> None:
        self._flush_field()
        current = self._current or {}
        title = current.get("title", "")
        url = current.get("url", "")
        if title and url:
            self.results.append(
                {"title": title, "url": url, "snippet": current.get("snippet", "")}
            )
        self._in_algo = False
        self._li_depth = 0
        self._current = None


class _PageParser(HTMLParser):
    """从 HTML 中提取标题、描述和可见正文。"""

    SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "nav", "footer", "aside"}
    BLOCK = {
        "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "pre", "blockquote", "table",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self._skip_depth = 0
        self._in_title = False
        self._title_buf: list[str] = []
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name in ("description", "og:description") and not self.description:
                self.description = _squash(values.get("content", ""))
        elif tag == "br":
            self._parts.append("\n")
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            self.title = _squash("".join(self._title_buf))
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
        elif self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [_squash(line) for line in raw.splitlines()]
        text = "\n".join(line for line in lines if line)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _unwrap_bing_url(url: str) -> str:
    """展开必应 /ck/a 跳转链接中的真实地址。"""
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if "bing.com" not in hostname or not parts.path.startswith("/ck/a"):
        return url
    encoded = parse_qs(parts.query).get("u", [""])[0]
    if not encoded.startswith("a1"):
        return url
    payload = encoded[2:]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        decoded = raw.decode("utf-8", "ignore")
    except (ValueError, TypeError):
        return url
    return decoded if decoded.startswith(("http://", "https://")) else url


def _parse_bing_html(html_text: str) -> list[dict]:
    parser = _BingParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        logger.warning("必应页面解析失败", exc_info=True)
    return parser.results


def _parse_bing_rss(xml_bytes: bytes) -> list[dict]:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return []
    results = []
    for item in root.iter("item"):
        title = _squash(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        snippet = _strip_tags(item.findtext("description") or "")
        if title and link.startswith("http"):
            results.append({"title": title, "url": link, "snippet": snippet})
    return results


def _playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


class WebTools:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        search_count: int = 5,
        fetch_timeout: float = 20.0,
        fetch_max_chars: int = 6000,
        fetch_max_bytes: int = 2 * 1024 * 1024,
        render_mode: str = "auto",
    ) -> None:
        self._session = session
        self._search_count = max(1, min(MAX_SEARCH_COUNT, search_count))
        self._fetch_timeout = max(5.0, fetch_timeout)
        self._fetch_max_chars = max(500, fetch_max_chars)
        self._fetch_max_bytes = max(64 * 1024, fetch_max_bytes)
        self._render_mode = render_mode if render_mode in ("auto", "curl") else "auto"

    def schemas(self) -> list[dict]:
        return TOOL_SCHEMAS

    async def execute(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise WebError("参数必须是 JSON 对象")
            if name == "web_search":
                return await self.search(args.get("query"), args.get("count"))
            if name == "web_fetch":
                return await self.fetch(args.get("url"), args.get("max_chars"))
            raise WebError(f"未知工具：{name}")
        except json.JSONDecodeError:
            logger.warning("网页工具参数 JSON 解析失败：%s", name)
            return (
                "工具参数不是有效 JSON，通常是单次输出过长被截断。"
                "请缩短参数后重试。"
            )
        except WebError as exc:
            return f"工具执行失败：{exc}"
        except Exception as exc:
            logger.exception("网页工具执行异常：%s", name)
            return f"工具执行异常：{exc}"

    # ---------- 搜索 ----------

    async def search(self, query: object, count: object = None) -> str:
        if not isinstance(query, str) or not query.strip():
            raise WebError("query 不能为空")
        query = query.strip()
        amount = self._resolve_count(count)

        html_text = await self._bing_html(query, amount)
        results = _parse_bing_html(html_text)[:amount]
        if not results:
            xml_bytes = await self._bing_rss(query, amount)
            results = _parse_bing_rss(xml_bytes)[:amount]

        payload = {"query": query, "engine": "bing", "results": results}
        if not results:
            payload["note"] = "未解析到搜索结果，可能触发了必应的人机验证"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _resolve_count(self, count: object) -> int:
        if count is None:
            return self._search_count
        try:
            value = int(count)
        except (TypeError, ValueError):
            return self._search_count
        return max(1, min(MAX_SEARCH_COUNT, value))

    async def _bing_html(self, query: str, count: int) -> str:
        params = {"q": query, "count": count, "setlang": "zh-hans", "mkt": "zh-CN"}
        timeout = aiohttp.ClientTimeout(total=self._fetch_timeout)
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
        async with self._session.get(
            BING_URL, params=params, headers=headers, timeout=timeout
        ) as resp:
            raw = await resp.read()
            content_type = resp.headers.get("Content-Type", "")
            if resp.status != 200:
                raise WebError(f"必应搜索失败：HTTP {resp.status}")
        return _decode_body(raw, content_type)

    async def _bing_rss(self, query: str, count: int) -> bytes:
        params = {"q": query, "format": "rss", "count": count, "setlang": "zh-hans"}
        timeout = aiohttp.ClientTimeout(total=self._fetch_timeout)
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
        async with self._session.get(
            BING_URL, params=params, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                return b""
            return await resp.read()

    # ---------- 抓取 ----------

    async def fetch(self, url: object, max_chars: object = None) -> str:
        if not isinstance(url, str) or not url.strip():
            raise WebError("url 不能为空")
        target = _validate_url(url)
        limit = self._resolve_chars(max_chars)

        final_url = target
        mode = "curl"
        content_type = ""
        body = b""

        if self._render_mode == "auto" and _playwright_available():
            rendered = await self._render(target)
            if rendered is not None:
                final_url, html_text = rendered
                mode = "playwright"
                return self._page_payload(final_url, html_text, limit, mode, "text/html")

        final_url, content_type, body = await self._curl(target)
        if "json" in content_type.lower() or content_type.startswith("text/plain"):
            text = _decode_body(body, content_type)
            truncated = len(text) > limit
            return json.dumps(
                {
                    "url": final_url,
                    "content_type": content_type,
                    "mode": mode,
                    "content": text[:limit] + ("…（已截断）" if truncated else ""),
                    "truncated": truncated,
                },
                ensure_ascii=False,
                indent=2,
            )

        html_text = _decode_body(body, content_type)
        return self._page_payload(final_url, html_text, limit, mode, content_type)

    def _resolve_chars(self, max_chars: object) -> int:
        if max_chars is None:
            return self._fetch_max_chars
        try:
            value = int(max_chars)
        except (TypeError, ValueError):
            return self._fetch_max_chars
        return max(200, min(20000, value))

    def _page_payload(
        self, url: str, html_text: str, limit: int, mode: str, content_type: str
    ) -> str:
        parser = _PageParser()
        try:
            parser.feed(html_text)
            parser.close()
        except Exception:
            logger.warning("网页正文解析失败：%s", url, exc_info=True)
        text = parser.text()
        truncated = len(text) > limit
        if truncated:
            text = text[:limit] + "…（已截断）"
        return json.dumps(
            {
                "url": url,
                "title": parser.title,
                "description": parser.description,
                "mode": mode,
                "content_type": content_type,
                "truncated": truncated,
                "text": text,
            },
            ensure_ascii=False,
            indent=2,
        )

    async def _curl(self, url: str) -> tuple[str, str, bytes]:
        """用 curl 抓取网页，返回 (最终 URL, Content-Type, 原始响应体)。"""
        marker = "__CXBOT_CURL_META__"
        command = [
            "curl", "-sS", "-L", "--compressed",
            "--max-time", str(int(self._fetch_timeout)),
            "--max-redirs", str(MAX_REDIRECTS),
            "--max-filesize", str(self._fetch_max_bytes),
            "-A", USER_AGENT,
            "-H", "Accept-Language: zh-CN,zh;q=0.9",
            "-w", f"\n{marker}%{{http_code}}|%{{content_type}}|%{{url_effective}}",
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("系统没有 curl，改用 aiohttp 抓取")
            return await self._http_fetch(url)

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._fetch_timeout + 5
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise WebError("抓取超时")

        raw = stdout or b""
        meta = ""
        if marker.encode() in raw:
            raw, meta_bytes = raw.rsplit(marker.encode(), 1)
            meta = meta_bytes.decode("utf-8", "ignore").strip()

        if proc.returncode != 0:
            message = (stderr or b"").decode("utf-8", "ignore").strip()
            if proc.returncode == 63:
                raise WebError(f"页面超过 {self._fetch_max_bytes // 1024}KB 上限")
            raise WebError(f"curl 抓取失败：{message or f'退出码 {proc.returncode}'}")

        status, content_type, final_url = (meta.split("|", 2) + ["", "", ""])[:3]
        if status and not status.isdigit():
            status = ""
        if status and not status.startswith("2"):
            raise WebError(f"抓取失败：HTTP {status}")
        return final_url or url, content_type, raw

    async def _http_fetch(self, url: str) -> tuple[str, str, bytes]:
        timeout = aiohttp.ClientTimeout(total=self._fetch_timeout)
        async with self._session.get(url, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                raise WebError(f"抓取失败：HTTP {resp.status}")
            raw = await resp.content.read(self._fetch_max_bytes + 1)
            if len(raw) > self._fetch_max_bytes:
                raise WebError(f"页面超过 {self._fetch_max_bytes // 1024}KB 上限")
            return str(resp.url), resp.headers.get("Content-Type", ""), raw

    async def _render(self, url: str) -> tuple[str, str] | None:
        """用 Playwright 等待 JS 加载完成后取渲染后的 HTML；不可用时返回 None。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=USER_AGENT, locale="zh-CN"
                    )
                    page = await context.new_page()
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self._fetch_timeout * 1000),
                    )
                    try:
                        await page.wait_for_load_state(
                            "networkidle", timeout=int(self._fetch_timeout * 1000)
                        )
                    except Exception:
                        logger.info("等待 JS 加载稳定超时，使用当前页面内容：%s", url)
                    html_text = await page.content()
                    return page.url, html_text
                finally:
                    await browser.close()
        except Exception:
            logger.info("Playwright 渲染失败，回退 curl：%s", url, exc_info=True)
            return None
