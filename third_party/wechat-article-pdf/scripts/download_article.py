#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
download_article.py —— 微信公众号文章下载器（纯 Python 标准库 + requests 可选）

输入一个或多个 mp.weixin.qq.com 文章链接，输出:
  <输出目录>/<公众号名>/<日期>_<标题>.pdf    —— 排版完成的 PDF（A4）
  <输出目录>/<公众号名>/<日期>_<标题>.md     —— Markdown（图片本地化，适合后续整合分析）
  <输出目录>/<公众号名>/<日期>_<标题>.json   —— 元数据
  <输出目录>/<公众号名>/assets/...          —— 下载的图片
  <输出目录>/_manifest.jsonl                —— 每次下载追加一条记录（机器可读）

用法示例:
  python3 download_article.py "https://mp.weixin.qq.com/s/xxxx"
  python3 download_article.py "URL1" "URL2" -o ~/Downloads/wechat-articles
  python3 download_article.py -l urls.txt
  python3 download_article.py --from-html /path/saved_page.html   # 处理已保存的页面 HTML（含验证页兜底）
  python3 download_article.py URL --no-images --no-pdf            # 只出 MD/JSON
  python3 download_article.py URL --max-images 30                 # 限制图片数量（测试或轻量场景）

依赖: Python 3.8+；请求用 requests（缺省时回退 urllib）；PDF 渲染需要本机 Chrome/Chromium。
"""

import argparse
import base64
import hashlib
import html as html_std
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
_UA = None  # --ua 覆盖


def current_ua():
    return _UA or DEFAULT_UA


REFERER = "https://mp.weixin.qq.com/"
MAX_IMAGE_BYTES = 12 * 1024 * 1024      # 单图上限 12MB
FETCH_RETRIES = 3
MIN_BASE64_IMAGE = 512                  # 内嵌 base64 图片小于此字节数直接丢弃（多为占位符）

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

BLOCK_MARKERS = {
    "verify": ["secitptpage", "环境异常", "访问过于频繁", "完成验证", "TCaptcha"],
    "deleted": ["参数错误", "该内容已被发布者删除", "此内容违反", "因违规无法查看",
                "此内容因违规无法查看", "已被发布者移除", "内容已被删除"],
}

TAG_BLOCK_RE = re.compile(r"<(script|style|noscript|form|button|input|textarea)\b[^>]*>.*?</\1>",
                          re.DOTALL | re.IGNORECASE)

COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# 文章内嵌的媒体/广告容器 → 占位说明
MEDIA_RE = re.compile(
    r"(<(?:mpvoice|mpvideo|video|audio|iframe)\b[^>]*>.*?</(?:mpvoice|mpvideo|video|audio|iframe)>|"
    r"<(?:mpvoice|mpvideo|video|audio|iframe)\b[^>]*/?>)",
    re.DOTALL | re.IGNORECASE)

IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def warn(msg):
    print("  [warn] " + msg, flush=True)


def find_chrome():
    env = os.environ.get("CHROME_BIN")
    if env and os.path.exists(env):
        return env
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    return None


def sanitize_name(name, max_len=60):
    """生成安全文件名：保留中文/字母/数字/常见符号，去掉文件系统非法字符。"""
    if not name:
        name = "untitled"
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "untitled"


def unique_path(path):
    """已存在则追加 -2/-3 …，避免覆盖。"""
    if not os.path.lexists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.lexists(f"{root}-{i}{ext}"):
        i += 1
    return f"{root}-{i}{ext}"


def extract_url(text):
    """从任意文本中提取第一个 mp.weixin.qq.com 文章链接（支持 /s/xxx 与 ?__biz= 或出现微信小卡片链接）。"""
    m = re.search(r"https?://mp\.weixin\.qq\.com/[^\s\"'<>)\u4e00-\u9fff]+", text)
    if m:
        return m.group(0).rstrip("，。；,.;、")
    m = re.search(r"https?://weixin\.qq\.com/[^\s\"'<>)\u4e00-\u9fff]+", text)
    if m:
        return m.group(0).rstrip("，。；,.;、")
    return None


def http_get(url, headers=None, timeout=25):
    if requests is not None:
        r = requests.get(url, headers=headers or {}, timeout=timeout, allow_redirects=True)
        return r.status_code, r.content
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def decode_html(raw):
    """按页面声明或 meta 推测编码解码 HTML。"""
    if isinstance(raw, str):
        return raw
    text = raw.decode("utf-8", errors="replace")
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4096])
    if m:
        enc = m.group(1).decode("ascii", "replace").lower()
        if enc not in ("utf-8", "utf8"):
            try:
                text = raw.decode(enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                pass
    return text


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def classify_blocked(html):
    if re.search(r'id="js_content"', html):
        return None  # 正常文章页
    low = html
    for kind, markers in BLOCK_MARKERS.items():
        for marker in markers:
            if marker in low:
                return kind
    return "unknown"


def fetch_page(url, retries=FETCH_RETRIES):
    """抓取文章页；返回 (html, final_url, blocked_kind|None|错误信息)。

    blocked_kind: None=成功; 'verify'=安全验证页; 'deleted'=已删除/失效; 其他=错误描述。
    """
    headers = {
        "User-Agent": current_ua(),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": REFERER,
    }
    last_err = None
    last_blocked = None
    for attempt in range(1, retries + 1):
        try:
            status, raw = http_get(url, headers=headers)
            html = decode_html(raw)
        except Exception as e:  # noqa: BLE001
            last_err = f"请求异常: {e}"
            time.sleep(1.5 * attempt)
            continue
        if status != 200:
            last_err = f"HTTP {status}"
            time.sleep(1.5 * attempt)
            continue
        blocked = classify_blocked(html)
        if blocked is None:
            return html, url, None
        last_blocked = blocked
        if blocked == "verify":
            # 验证页有重试价值：稍等再试
            time.sleep(2 + attempt)
            continue
        return html, url, blocked
    if last_blocked:
        return None, url, last_blocked
    return None, url, last_err or "fetch-failed"


# ---------------------------------------------------------------------------
# 解析元数据
# ---------------------------------------------------------------------------

TAG_ATTR_CACHE = {}


def attr_value(tag_html, attr):
    m = re.search(r"%s\s*=\s*[\"']([^\"']*)[\"']" % re.escape(attr), tag_html)
    if m:
        return html_std.unescape(m.group(1))
    m = re.search(r"%s\s*=\s*([^\s>\"']+)" % re.escape(attr), tag_html)
    return html_std.unescape(m.group(1)) if m else None


def extract_meta(html, final_url):
    """标题 / 公众号名 / 作者 / 发布日期。"""
    meta = {"title": None, "account": None, "author": None, "publish_date": None,
            "publish_ts": None, "url": final_url}

    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    if m:
        meta["title"] = html_std.unescape(m.group(1))
    if not meta["title"]:
        m = re.search(r'id="activity-name"[^>]*>([^<]+)', html)
        if m:
            meta["title"] = m.group(1).strip()

    m = re.search(r'id="js_name"[^>]*>(.*?)</a>', html, re.DOTALL)
    if m:
        acc = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if acc:
            meta["account"] = acc
    if not meta["account"]:
        m = re.search(r'<meta\s+property="og:article:author"\s+content="([^"]*)"', html)
        if m:
            meta["account"] = html_std.unescape(m.group(1))

    m = re.search(r'<meta\s+property="og:article:author"\s+content="([^"]*)"', html)
    if m:
        meta["author"] = html_std.unescape(m.group(1))
    if not meta["author"]:
        m = re.search(r'var\s+author\s*=\s*["\']([^"\']*)["\']', html)
        if m:
            meta["author"] = m.group(1)

    # 发布时间：优先 createTime 变量，其次 createTimestamp/oriCreateTime 时间戳
    m = re.search(r"var\s+createTime\s*=\s*['\"](\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2})?)['\"]", html)
    if m:
        full = m.group(1).replace("T", " ")
        meta["publish_date"] = full[:10]
        if len(full) > 10:
            meta["publish_time_full"] = full
    if not meta["publish_date"]:
        ts = None
        m = re.search(r"createTimestamp\s*=\s*['\"](\d{9,13})['\"]", html)
        if not m:
            m = re.search(r"oriCreateTime\s*=\s*['\"](\d{9,13})['\"]", html)
        if m:
            ts = m.group(1)
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone()
                meta["publish_date"] = dt.strftime("%Y-%m-%d")
                meta["publish_ts"] = int(ts)
                meta["publish_time_full"] = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, OverflowError):
                pass
    if not meta["publish_date"]:
        m = re.search(r'<meta\s+property="article:published_time"\s+content="([^"]*)"', html)
        if m:
            meta["publish_date"] = m.group(1)[:10]
    if not meta["publish_date"]:
        m = re.search(r"var\s+createTime\s*=\s*['\"](\d{4}-\d{1,2}-\d{1,2})['\"]", html)
        if m:
            meta["publish_date"] = m.group(1)
    return meta


def _extract_at(html, idx):
    """从 id="js_content" 出现的下标处提取其 div 内部 HTML。"""
    tag_start = html.rfind("<div", 0, idx)
    if tag_start < 0:
        return None
    open_idx = html.find(">", idx)
    if open_idx < 0:
        return None
    pos = open_idx + 1
    depth = 1
    i = pos
    pattern = re.compile(r"<div\b|</div>|<!--|-->|<script\b", re.IGNORECASE)
    while i < len(html):
        if html.startswith("<!--", i):
            end = html.find("-->", i + 4)
            i = end + 3 if end >= 0 else len(html)
            continue
        m = pattern.match(html, i)
        if not m:
            i += 1
            continue
        tok = m.group(0)
        if tok == "</div>":
            depth -= 1
            if depth == 0:
                return html[pos:m.start()]
            i = m.end()
        elif tok.lower().startswith("<script"):
            end = re.search(r"</script\s*>", html[m.end():], re.IGNORECASE)
            if end is None:
                return None
            i = m.end() + end.end()
        else:
            if tok == "-->":
                i = m.end()
                continue
            depth += 1
            i = m.end()
    return html[pos:]


def extract_content_html(html):
    """取出 #js_content 元素的内部 HTML。

    页面 JS 里也可能出现 'js_content' 字符串；若首个结果过短（<200 字符），
    遍历所有出现位置取最长片段，避免误中 JS 模板。
    """
    found = []
    idx = 0
    while True:
        idx = html.find('id="js_content"', idx)
        if idx < 0:
            break
        inner = _extract_at(html, idx)
        if inner:
            found.append((len(inner), inner))
        idx += 1
    if not found:
        return None
    found.sort(key=lambda x: -x[0])
    return found[0][1] if len(found[0][1]) >= 200 else (found[0][1] or None)


# ---------------------------------------------------------------------------
# 内容清理
# ---------------------------------------------------------------------------

def clean_content(inner):
    """清理: 注释/脚本/样式/表单；媒体与 iframe 替换为占位说明；保留文本结构。"""
    inner = COMMENT_RE.sub("", inner)
    inner = TAG_BLOCK_RE.sub("", inner)

    def media_repl(m):
        tag = m.group(0)
        mtag = re.match(r"<(\w+)", tag)
        kind = mtag.group(1).lower() if mtag else "media"
        label = {"mpvoice": "音频", "mpvideo": "视频", "video": "视频",
                 "audio": "音频", "iframe": "嵌入内容"}.get(kind, "嵌入内容")
        src = ""
        msrc = re.search(r'(?:data-src|src)\s*=\s*["\'](https?://[^"\']+)["\']', tag)
        if msrc:
            src = msrc.group(1)
        link = f' <a href="{src}" style="color:#576b95;word-break:break-all;">{src}</a>' if src else ""
        return (f'<div style="border:1px dashed #d0d0d0;background:#f7f7f7;color:#888;'
                f'text-align:center;padding:16px 8px;margin:12px 0;border-radius:6px;">'
                f'<span>【{label}已省略，可打开原文查看】</span>{link}</div>')

    inner = MEDIA_RE.sub(media_repl, inner)

    # 残留的脚本（未闭合/双写）与 JS 事件属性
    inner = re.sub(r"<script\b[^>]*>.*?</script>", "", inner, flags=re.DOTALL | re.IGNORECASE)
    inner = re.sub(r"\s+on\w+\s*=\s*[\"'][^\"']*[\"']", "", inner, flags=re.IGNORECASE)
    inner = re.sub(r"\s+on\w+\s*=\s*[^\s>]+", "", inner, flags=re.IGNORECASE)
    inner = re.sub(r'href\s*=\s*["\']javascript:[^"\']*["\']', 'href="#"', inner, flags=re.IGNORECASE)
    return inner


def pick_image_src(tag_html):
    """按微信优先级选图：data-src(懒加载真图) > data-original > src(http) > src(data:)。"""
    for attr in ("data-src", "data-original", "src"):
        val = attr_value(tag_html, attr)
        if not val:
            continue
        if val.startswith("http"):
            return val
        if val.startswith("data:image/") and attr == "src":
            return val
    return None


def collect_images(inner):
    """按文档顺序收集图片 src/alt；返回 [ {src, alt, idx} ]。"""
    imgs = []
    for m in IMG_TAG_RE.finditer(inner):
        tag = m.group(0)
        src = pick_image_src(tag)
        if not src:
            continue
        if src.startswith("https://res.wx.qq.com") or "res.wx.qq.com" in src:
            continue  # 平台资源图（蒙层/图标），不下载
        imgs.append({
            "src": src,
            "alt": (attr_value(tag, "alt") or "").strip(),
            "idx": len(imgs),
        })
    return imgs


# ---------------------------------------------------------------------------
# 图片下载
# ---------------------------------------------------------------------------

def prepare_asset(src):
    """返回 (ext, data|None)：ext 为文件扩展名；data 仅对 data: URI 给出解码后的字节。

    返回 (None, None) 表示该资源应跳过（平台资源、过小占位图、异常 data URI）。
    """
    if src.startswith("data:"):
        m = re.match(r"data:image/([a-zA-Z0-9+.-]+);base64,(.*)$", src, re.DOTALL)
        if not m:
            return None, None
        ext = m.group(1).lower().replace("jpeg", "jpg")
        if ext in ("svg+xml", "svg"):
            return None, None
        try:
            data = base64.b64decode(m.group(2), validate=False)
        except Exception:  # noqa: BLE001
            return None, None
        if len(data) < MIN_BASE64_IMAGE:
            return None, None
        return ext, data
    ext = "jpg"
    m = re.search(r"wx_fmt=(\w+)", src)
    if m:
        ext = {"jpeg": "jpg", "png": "png", "gif": "gif", "svg": "svg",
               "webp": "webp", "bmp": "bmp"}.get(m.group(1).lower(), "jpg")
    else:
        path = urllib.parse.urlparse(src).path
        if path and "." in path:
            ext = path.rsplit(".", 1)[1].lower()[:5] or "jpg"
    ext = ext if re.fullmatch(r"[a-z0-9]{2,5}", ext) else "jpg"
    return ext, None


def download_images(imgs, assets_dir, dir_url=""):
    """下载 http(s) 图片 / 解码 base64 图片；返回 {src: 本地相对路径}。"""
    os.makedirs(assets_dir, exist_ok=True)
    asset_map = {}
    failures = []
    headers = {"User-Agent": current_ua(), "Referer": REFERER, "Accept": "image/*"}
    n_downloaded = 0

    for img in imgs:
        src = img["src"]
        if src in asset_map:
            continue
        ext, inline_data = prepare_asset(src)
        if ext is None:
            continue
        if src.startswith("data:"):
            data = inline_data
        else:
            data = None
            for attempt in range(2):
                try:
                    status, content = http_get(src, headers=headers, timeout=25)
                    if status == 200 and 0 < len(content) <= MAX_IMAGE_BYTES:
                        data = content
                        break
                    if status == 200 and len(content) > MAX_IMAGE_BYTES:
                        warn(f"图片过大跳过 {len(content)//1024}KB: {src[:70]}")
                        break
                except Exception as e:  # noqa: BLE001
                    warn(f"图片下载失败({attempt+1}): {src[:70]} — {e}")
                    time.sleep(1.0)
            if data is None:
                failures.append(src[:90])
                continue
        md5 = hashlib.md5(data).hexdigest()[:12]
        fname = f"{md5}.{ext}"
        dest = os.path.join(assets_dir, fname)
        rel = os.path.join("assets", fname) if not dir_url else os.path.join(dir_url, "assets", fname)
        if not os.path.exists(dest):
            with open(dest, "wb") as f:
                f.write(data)
        asset_map[src] = rel.replace(os.sep, "/")
        n_downloaded += 1
    return asset_map, failures, n_downloaded


# ---------------------------------------------------------------------------
# HTML → Markdown（纯标准库 HTMLParser 转换）
# ---------------------------------------------------------------------------

from html.parser import HTMLParser  # noqa: E402

_BLOCK_TAGS = {"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
               "ul", "ol", "li", "table", "tr", "pre", "hr", "figure", "figcaption",
               "article", "header", "footer", "br"}


class _MDConverter(HTMLParser):
    def __init__(self, asset_map):
        super().__init__(convert_charrefs=True)
        self.asset_map = asset_map
        self.out = []
        self._list_depth = 0
        self._in_pre = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "pre":
            self._in_pre = True
            self.out.append("\n```\n")
            return
        if tag == "br":
            self.out.append("\n")
            return
        if tag == "img":
            d = {}
            for a, v in attrs:
                d[a.lower()] = html_std.unescape(v)
            # 与 pick_image_src 相同的优先级：data-src > data-original > src
            src = d.get("data-src") or d.get("data-original") or d.get("src")
            alt = d.get("alt") or ""
            local = self.asset_map.get(src or "")
            if local:
                self.out.append(f"![{alt}]({local})\n")
            elif src and src.startswith("http"):
                self.out.append(f"![{alt}]({src})\n")
            return
        if tag == "p":
            self.out.append("\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            n = int(tag[1])
            self.out.append("\n\n" + "#" * n + " ")
        elif tag == "blockquote":
            self.out.append("\n\n> ")
        elif tag in ("ul", "ol"):
            self._list_depth += 1
            self.out.append("\n")
        elif tag == "li":
            marker = "- " if self._list_depth == 1 else "  * "
            self.out.append("\n" + marker)
        elif tag == "td":
            self.out.append(" | ")
        elif tag == "tr":
            self.out.append("\n|")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "code":
            if not self._in_pre:
                self.out.append("`")
        elif tag == "a":
            pass  # 链接以纯文本输出，避免产生孤立的 [
        elif tag == "span":
            pass
        else:
            pass

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "pre":
            self._in_pre = False
            self.out.append("\n```\n")
            return
        if tag == "p":
            self.out.append("\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "div", "section",
                     "figure", "figcaption", "table", "tr"):
            self.out.append("\n\n")
        elif tag in ("ul", "ol"):
            self._list_depth = max(0, self._list_depth - 1)
            self.out.append("\n\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "code":
            if not self._in_pre:
                self.out.append("`")
        elif tag == "li":
            pass

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_pre:
            self.out.append(data)
        else:
            self.out.append(data)

    def render(self):
        text = "".join(self.out)
        text = re.sub(r"[ \t]+", " ", text)
        # 相邻/嵌套的强调与加粗标记合并后留下的 ***/**** 残渣（本转换器从不主动输出 3+ 星号）
        text = re.sub(r"\*{3,}", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def html_to_markdown(inner_html, asset_map):
    conv = _MDConverter(asset_map)
    try:
        conv.feed(inner_html)
        conv.close()
    except Exception as e:  # noqa: BLE001
        warn(f"Markdown 转换部分失败: {e}")
    return conv.render()


# ---------------------------------------------------------------------------
# HTML 渲染 + PDF
# ---------------------------------------------------------------------------

PRINT_CSS = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm; }
* { box-sizing: border-box; }
html { font-family: -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
body { margin: 0; color: #1a1a1a; word-break: break-word; }
.article-head { border-bottom: 2px solid #07c160; padding-bottom: 12px; margin-bottom: 20px; }
.article-head h1 { font-size: 21px; line-height: 1.45; margin: 0 0 10px; font-weight: 700; color: #101010; }
.article-meta { font-size: 12px; color: #999; line-height: 1.6; }
.article-meta .src { word-break: break-all; color: #576b95; text-decoration: none; }
#js_content { max-width: 100%; }
#js_content { visibility: visible !important; opacity: 1 !important; }
#js_content p { font-size: 15.5px; line-height: 1.75; margin: 0 0 12px; }
#js_content img { max-width: 100% !important; height: auto !important; display: block;
                  margin: 10px auto 14px; border-radius: 4px; }
#js_content video, #js_content iframe { max-width: 100%; }
#js_content blockquote { border-left: 3px solid #d9d9d9; margin: 12px 0; padding: 4px 14px;
                         color: #555; background: #fafafa; }
#js_content table { max-width: 100%; border-collapse: collapse; margin: 12px 0; }
#js_content td, #js_content th { border: 1px solid #e5e5e5; padding: 6px 10px; font-size: 13px; }
#js_content pre { background: #f5f5f5; padding: 10px 12px; overflow: hidden;
                  white-space: pre-wrap; word-break: break-all; border-radius: 4px; font-size: 12.5px; }
#js_content code { background: #f2f2f2; padding: 1px 4px; border-radius: 3px; font-size: 13px; }
#js_content h1, #js_content h2, #js_content h3 { font-weight: 700; margin: 18px 0 10px; }
#js_content section { max-width: 100%; }
.article-foot { margin-top: 26px; padding-top: 10px; border-top: 1px solid #eee;
                font-size: 11px; color: #aaa; word-break: break-all; }
"""


def rewrite_img_src(tag, local):
    """把 <img> 的 data-src/data-original 拿掉，并保证 src 生效。

    微信懒加载图片往往只有 data-src（浏览器不会加载 data-src），
    必须显式写进 src 属性；local 可以是本地相对路径或 data: URI。
    """
    class ImageTag(HTMLParser):
        def handle_starttag(self, name, attrs):
            self.attrs = attrs

        handle_startendtag = handle_starttag

    parser = ImageTag(convert_charrefs=True)
    parser.attrs = []
    parser.feed(tag)
    kept = [(k, v) for k, v in parser.attrs
            if k.lower() not in ("src", "srcset", "data-src", "data-original")
            and not k.lower().startswith("on")]
    attrs = [("src", local)] + kept
    return "<img " + " ".join(
        k if v is None else f'{k}="{html_std.escape(v, quote=True)}"'
        for k, v in attrs) + ">"


def build_render_html(meta, content_html, asset_map, embed_base64, article_dir):
    """构造完整页面。embed_base64=True 时把图片内联（PDF 渲染用）。"""
    head = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'img-src data: file:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
        f"<title>{html_escape(meta['title'] or '')}</title>"
        f"<style>{PRINT_CSS}</style></head><body>"
    )
    meta_line = " · ".join(x for x in [meta.get("account"), meta.get("author"),
                                       meta.get("publish_time_full") or meta.get("publish_date")] if x)
    head += (
        '<div class="article-head"><h1>' + html_escape(meta["title"] or "") + "</h1>"
        '<div class="article-meta">' + html_escape(meta_line or "微信公众号") +
        "<br/><a class='src' href='" + html_escape(meta["url"] or "") + "'>来源: " +
        html_escape(meta["url"] or "") + "</a></div></div>"
    )

    body = content_html
    if embed_base64:
        def repl(m):
            tag = m.group(0)
            src = pick_image_src(tag)
            local = asset_map.get(src) if src else None
            if not local:
                return tag
            fpath = os.path.join(article_dir, local)
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                b64 = base64.b64encode(data).decode()
                mime = detect_mime(fpath)
                return rewrite_img_src(tag, f"data:{mime};base64,{b64}")
            except OSError:
                return tag
        body = IMG_TAG_RE.sub(repl, body)
    else:
        def repl_local(m):
            tag = m.group(0)
            src = pick_image_src(tag)
            local = asset_map.get(src)
            if not local:
                return tag
            return rewrite_img_src(tag, local)
        body = IMG_TAG_RE.sub(repl_local, body)

    foot = ('<div class="article-foot">本文由 wechat-article-pdf 技能自动下载生成 · '
            '内容版权归原公众号所有 · 仅供个人学习研究</div>')
    return head + '<div id="js_content">' + body + "</div>" + foot + "</body></html>"


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def detect_mime(path):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
            "svg": "image/svg+xml"}.get(ext, "application/octet-stream")


def render_pdf(render_html_path, pdf_path):
    chrome = find_chrome()
    if not chrome:
        warn("未找到 Chrome/Chromium，跳过 PDF（可用 --pdf-only 查看错误）。请安装 Chrome 或设置 CHROME_BIN。")
        return None
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-pdf-header-footer", "--disable-extensions",
        f"--print-to-pdf={pdf_path}",
        "file://" + render_html_path,
    ]
    # Some Chrome builds finish the PDF but keep their helper processes alive.
    # Own a separate process group and stop only that group after a stable EOF.
    with tempfile.TemporaryDirectory(prefix="wxmp_chrome_") as profile:
        cmd.insert(1, f"--user-data-dir={profile}")
        cmd.insert(2, "--disable-background-networking")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=(os.name == "posix"))
        complete, stable, previous = False, 0, None
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    stat = os.stat(pdf_path)
                    current = (stat.st_size, stat.st_mtime_ns)
                    with open(pdf_path, "rb") as f:
                        f.seek(max(0, stat.st_size - 1024))
                        eof = b"%%EOF" in f.read()
                    stable = stable + 1 if current == previous and eof else 0
                    previous = current
                    if eof and stable >= 3:
                        complete = True
                        break
                except FileNotFoundError:
                    pass
                if proc.poll() is not None and previous is None:
                    break
                time.sleep(0.5)
        finally:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait(timeout=5)
        if not complete:
            warn("Chrome 未生成完整 PDF")
            return None
    if not os.path.exists(pdf_path):
        return None
    with open(pdf_path, "rb") as f:
        head = f.read(5)
    if head != b"%PDF-":
        warn("PDF 文件头校验失败")
        return None
    return os.path.getsize(pdf_path)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def process_article(url, out_root, opts):
    t0 = time.time()
    log("")
    log("=" * 68)
    log(f"▶ {url}")

    if opts.from_html:
        with open(opts.from_html, encoding="utf-8", errors="replace") as f:
            html = f.read()
        blocked = classify_blocked(html)
        if blocked:
            warn("本地文件不是已通过验证的文章正文，停止处理。")
            return None
        log("  使用本地 HTML 文件")
    else:
        html, final_url, blocked = fetch_page(url)
        url = final_url
        if blocked == "deleted":
            warn("文章已删除或链接失效（参数错误/已移除），跳过。")
            return None
        if blocked == "verify":
            warn("触发微信安全验证页（环境异常/访问频繁）。可稍后重试，或按 references/guide.md 用浏览器兜底。")
            return None
        if html is None:
            warn(f"抓取失败: {blocked}")
            return None

    meta = extract_meta(html, url)
    if not meta["title"]:
        meta["title"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", extract_content_html(html) or ""))[:40] or "untitled"
    inner = extract_content_html(html)
    if not inner or not inner.strip():
        warn("未能提取正文（页面是否已改版？可先保存完整 HTML 后用 --from-html 处理）。")
        return None
    inner = clean_content(inner)

    account_dirname = sanitize_name(meta["account"] or "默认公众号", 40)
    date_prefix = (meta.get("publish_date") or datetime.now().strftime("%Y-%m-%d")).replace("/", "-")
    stem = f"{date_prefix}_{sanitize_name(meta['title'])}"
    article_dir = os.path.join(out_root, account_dirname)
    assets_dir = os.path.join(article_dir, "assets")
    os.makedirs(article_dir, exist_ok=True)
    # Reserve a whole bundle: repeat downloads must not overwrite the JSON.
    candidate, suffix = stem, 1
    while True:
        names = [candidate + ext for ext in (".md", ".json", ".pdf", "_原文.html", ".reserve")]
        if not any(os.path.lexists(os.path.join(article_dir, name)) for name in names):
            try:
                with open(os.path.join(article_dir, candidate + ".reserve"), "x"):
                    pass
                stem = candidate
                break
            except FileExistsError:
                pass
        suffix += 1
        candidate = f"{stem}-{suffix}"

    # 图片
    asset_map, failures, n_imgs = {}, [], 0
    all_imgs = collect_images(inner)
    if not opts.no_images:
        imgs = all_imgs
        if opts.max_images and len(imgs) > opts.max_images:
            imgs = imgs[: opts.max_images]
        log(f"  图片 {len(imgs)} 张 -> 下载中...")
        asset_map, failures, n_imgs = download_images(imgs, assets_dir, dir_url="")
        log(f"  图片完成: {n_imgs} 张")
        if failures:
            warn(f"{len(failures)} 张图片下载失败（PDF 中会保留原链接占位）")
    else:
        asset_map = {im["src"]: im["src"] for im in collect_images(inner)}

    # Markdown（图片本地化相对路径）
    md_text = html_to_markdown(inner, asset_map)
    md_path = unique_path(os.path.join(article_dir, stem + ".md"))
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {meta['title']}\n\n")
        f.write(f"> 公众号: {meta.get('account') or '-'} · 作者: {meta.get('author') or '-'} · "
                f"发布时间: {meta.get('publish_time_full') or meta.get('publish_date') or '-'}\n")
        f.write(f"> 原文: {url}\n\n---\n\n")
        f.write(md_text)

    # 静态 HTML（相对路径引用 assets/，供再次渲染/检查）
    clean_html = build_render_html(meta, inner, asset_map, embed_base64=False, article_dir=article_dir)
    html_path = unique_path(os.path.join(article_dir, stem + "_原文.html"))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(clean_html)

    # PDF
    pdf_path = None
    if not opts.no_pdf:
        log("  渲染 PDF ...")
        with tempfile.TemporaryDirectory(prefix="wxmp_pdf_") as td:
            render_html = build_render_html(meta, inner, asset_map, embed_base64=True,
                                            article_dir=article_dir)
            rpath = os.path.join(td, "render.html")
            with open(rpath, "w", encoding="utf-8") as f:
                f.write(render_html)
            pdf_path = unique_path(os.path.join(article_dir, stem + ".pdf"))
            size = render_pdf(rpath, pdf_path)
            if size is None:
                pdf_path = None

    # JSON 元数据
    meta_out = {
        "title": meta["title"], "account": meta["account"], "author": meta["author"],
        "publish_date": meta.get("publish_date"), "publish_time_full": meta.get("publish_time_full"),
        "url": url, "download_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "images_downloaded": n_imgs, "images_failed": failures,
        "images_total_unique": len({im["src"] for im in all_imgs}),
        "images_complete": all(im["src"] in asset_map and
                               asset_map[im["src"]].startswith("assets/") for im in all_imgs),
        "validation_status": "requires-content-completeness-check",
        "chars": len(re.sub(r"<[^>]+>", "", inner)),
        "files": {"md": os.path.basename(md_path),
                  "html": os.path.basename(html_path),
                  "pdf": os.path.basename(pdf_path) if pdf_path else None},
    }
    json_path = os.path.join(article_dir, stem + ".json")
    with open(json_path, "x", encoding="utf-8") as f:
        json.dump(meta_out, f, ensure_ascii=False, indent=2)

    # manifest 追加
    manifest_path = os.path.join(out_root, "_manifest.jsonl")
    with open(manifest_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(meta_out, ensure_ascii=False) + "\n")

    log("")
    log(f"✔ 完成  {meta['title'][:44]}")
    if pdf_path:
        log(f"  PDF → {pdf_path}  ({os.path.getsize(pdf_path)//1024} KB)")
    log(f"  MD  → {md_path}")
    log(f"  JSON→ {json_path}")
    log(f"  耗时 {time.time()-t0:.1f}s")
    return meta_out


def main():
    ap = argparse.ArgumentParser(description="微信公众号文章下载器 → PDF/MD/JSON")
    ap.add_argument("urls", nargs="*", help="mp.weixin.qq.com 文章链接")
    ap.add_argument("-l", "--urls-file", help="每行一个链接的文件")
    ap.add_argument("-o", "--output", default=os.path.expanduser("~/Downloads/wechat-articles"),
                    help="输出根目录（默认 ~/Downloads/wechat-articles）")
    ap.add_argument("--from-html", help="处理已保存的页面 HTML 文件（验证页兜底）")
    ap.add_argument("--source-url", help="本地HTML对应的公开原文链接，保留来源追溯")
    ap.add_argument("--no-pdf", action="store_true", help="只输出 MD/JSON")
    ap.add_argument("--no-images", action="store_true", help="不下载图片（PDF 保留原链）")
    ap.add_argument("--max-images", type=int, default=0, help="限制每篇图片数量")
    ap.add_argument("--ua", default=None, help="自定义 User-Agent")
    opts = ap.parse_args()

    global _UA
    if opts.ua:
        _UA = opts.ua

    urls = list(opts.urls)
    if opts.urls_file:
        with open(opts.urls_file, encoding="utf-8") as f:
            for line in f:
                u = extract_url(line)
                if u:
                    urls.append(u)
    if opts.from_html:
        urls = [opts.source_url or "(local html)"]
    if not urls:
        ap.print_help()
        sys.exit(1)

    out_root = os.path.expanduser(opts.output)
    os.makedirs(out_root, exist_ok=True)
    log(f"输出目录: {out_root}")

    done = 0
    failed = 0
    for u in urls:
        if u in ("(local html)",):
            r = process_article(None, out_root, opts)
        else:
            target = extract_url(u)
            if not target or urllib.parse.urlsplit(target).hostname != "mp.weixin.qq.com":
                warn("只接受 mp.weixin.qq.com 公众号文章链接。")
                failed += 1
                continue
            r = process_article(target, out_root, opts)
        if r:
            done += 1
        else:
            failed += 1
    log("")
    log(f"完成 {done} 篇, 失败 {failed} 篇 → {out_root}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
