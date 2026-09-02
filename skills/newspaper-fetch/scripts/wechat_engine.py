#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中国建设报 · 每日读报自动抓取

微信公众号「中国建设报」每个工作日约 10:08 发布《读报 | 《中国建设报》M月D日》，
内含当日报纸各版高清图片。本脚本每天自动：

  1. 用搜狗微信搜索定位当日读报文章（精确标题查询）
  2. 解析搜狗签名链接 -> 抓取文章完整 HTML（提取发布时刻 ct / biz / mid / idx）
  3. 校验发布年份/日期与目标一致（消除跨年同名文章歧义）
  4. 调用 wechat-article-pdf 的 download_article.py --from-html 本地化图片并渲染 PDF
  5. 按参考案例生成「电子报_YYYY-MM-DD/」：高清热图/N版_版名.jpg + A4 高清 PDF（期号 OCR）
  6. 校验产物（PDF/MD/JSON/图片）并记日志 _dailylog.jsonl，发 macOS 通知，同步知识库

用法：
  python3 fetch_daily.py                     # 今天（找不到则回退昨天）
  python3 fetch_daily.py --date 2026-09-01   # 指定日期
  python3 fetch_daily.py --force             # 已下载过也重新下载（生成 -2 后缀，不覆盖）
  python3 fetch_daily.py --rebuild-epaper    # 仅重建电子报文件夹（高清热图+PDF）
  python3 fetch_daily.py --max-retries 0     # 不等待重试，单次即退
  定时任务（launchd）：见同目录 ../SKILL.md
"""
import argparse
import datetime
import glob
import html as html_mod
import json
import os
import re
import subprocess
import sys
import tempfile
import time

ACCOUNT = "中国建设报"
BIZ = "MzA4MDQ2NDYxNg=="  # 中国建设报 公众号 biz，用于候选校验
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")  # 与已装 Chrome 一致；旧 UA 被搜狗拉黑
SG_REF = "https://weixin.sogou.com/"
DEFAULT_OUT = os.path.expanduser("~/Library/Application Support/readdaily/wechat-articles")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
BUNDLED_DOWNLOADER = os.path.join(
    REPO_ROOT, "third_party", "wechat-article-pdf", "scripts", "download_article.py")
EXTERNAL_DOWNLOADER = os.path.expanduser(
    "~/.agents/skills/wechat-article-pdf/scripts/download_article.py")
DOWNLOADER = (
    os.environ.get("READDAILY_WECHAT_DOWNLOADER")
    or (BUNDLED_DOWNLOADER if os.path.isfile(BUNDLED_DOWNLOADER) else EXTERNAL_DOWNLOADER)
)
DAILY_LOG = os.path.join(DEFAULT_OUT, "_dailylog.jsonl")

# ---- 电子报（参考案例格式） ----
EP_WIDTH = 1280                      # 高清热图宽度（与参考案例一致：1080→1280 高清放大）
EP_QUALITY = 92
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]
VOCR = os.environ.get("READDAILY_VOCR") or os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "bin", "vocr"))
DEFAULT_KB = os.path.expanduser(
    "~/Maitty的知识库/08-DeepSeek Harness/中国建设报-电子报PDF下载")
ED_NAME_DEFAULT = {1: "要闻", 2: "综合新闻"}   # 1/2版出现长标题时用默认版名（与参考案例一致）

MIN_PAGE_BYTES = 10000          # 低于此长度的搜狗响应视为反爬空壳
MIN_ARTICLE_BYTES = 50000       # 微信文章页最小合理体积

TITLE_PAIR_RE = re.compile(
    r'<h3>\s*<a[^>]*href="(/link\?url=[^"]+)"[^>]*>(.*?)</a>', re.S)
CHUNK_RE = re.compile(r"url\s*\+=\s*'([^']*)'")
DIRECT_SRC11_RE = re.compile(r"(https://mp\.weixin\.qq\.com/s\?src=11[^'\"\s<>]*?)(?:'|\"|<|\s|$)")
CT_RE = re.compile(r'var\s+ct\s*=\s*["\'](\d{10,})["\']')
BIZ_RE = re.compile(r'var\s+biz\s*=\s*["\']([^"\']+)')
MID_RE = re.compile(r'var\s+mid\s*=\s*["\']?(\d+)')
IDX_RE = re.compile(r'var\s+idx\s*=\s*["\']?(\d+)')
OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"')
TXT_TITLE_RE = re.compile(r'<title>(.*?)</title>', re.S)
ACTIVITY_NAME_RE = re.compile(r'id="activity-name"[^>]*>\s*(.*?)\s*</h1>', re.S)
ENV_BAD_WORDS = ("环境异常", "访问过于频繁", "完成验证", "secitptpage", "TCaptcha")


class TransientError(Exception):
    """搜狗/微信瞬时反爬，值得稍后重试。"""


# ---------------------------------------------------------------------------
# HTTP：requests 优先，无 requests 时回退 urllib（保持与下载脚本一致的策略）
# ---------------------------------------------------------------------------
try:
    import requests  # type: ignore
    _SHARED_HTTP = requests.Session()
    _SHARED_HTTP.headers["User-Agent"] = UA

    def _http_get(url, referer=None):
        r = _SHARED_HTTP.get(
            url, headers={"Referer": referer} if referer else {}, timeout=30)
        return r.status_code, r.url, r.text
except ImportError:  # pragma: no cover - 标准库回退
    import http.cookiejar
    import urllib.request
    _CJ = http.cookiejar.CookieJar()
    _OP = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_CJ))
    _OP.addheaders = [("User-Agent", UA)]

    def _http_get(url, referer=None):
        headers = {"Referer": referer} if referer else {}
        req = urllib.request.Request(url, headers=headers)
        with _OP.open(req, timeout=30) as resp:
            return resp.status, resp.geturl(), resp.read().decode("utf-8", "ignore")


def norm_title(raw):
    t = html_mod.unescape(re.sub(r"<[^>]+>", "", raw))
    t = re.sub(r"[\s\u00a0|｜《》]", "", t)  # 去空白/竖线/书名号
    return t


def month_day_label(d):
    return f"{d.month}月{d.day}日"


def is_read_article_title(norm, d):
    return ("读报" in norm and "中国建设报" in norm
            and month_day_label(d) in norm)


def parse_article_meta(html_text):
    """从文章页提取 (ct秒级, biz, mid, idx, og_title)。ct=None 表示无法判定年份。"""
    ct = None
    for m in CT_RE.findall(html_text):
        v = int(m)
        if 1577836800 <= v <= 2050000000:  # 2020-01-01 ~ 2034-12-31
            ct = v
            break
    def first(pat):
        m = re.search(pat, html_text)
        return m.group(1) if m else None
    biz = first(BIZ_RE)
    mid = first(MID_RE)
    idx = first(IDX_RE)
    title = first(OG_TITLE_RE)
    if not title:
        title = first(TXT_TITLE_RE)
    if not title:
        title = first(ACTIVITY_NAME_RE)
    title = html_mod.unescape(title).strip() if title else ""
    return ct, biz, mid, idx, title


# ---------------------------------------------------------------------------
# 搜狗搜索与链接解析
# ---------------------------------------------------------------------------
_WARMED = {"done": False}


def sogou_search(query):
    """返回 [(rel_link, 原始标题)]；反爬空壳抛 TransientError。"""
    if not _WARMED["done"]:
        try:
            _http_get("https://weixin.sogou.com/")
        except Exception:  # noqa: BLE001
            pass
        _WARMED["done"] = True
        time.sleep(1.5)
    url = "https://weixin.sogou.com/weixin?type=2&query=" + query.replace(" ", "%20")
    status, _, body = _http_get(url, referer=SG_REF)
    if status != 200 or len(body) < MIN_PAGE_BYTES or "news-list" not in body:
        raise TransientError(f"搜狗搜索空响应/反爬 (status={status}, len={len(body)})")
    pairs = []
    for rel, raw_title in TITLE_PAIR_RE.findall(body):
        rel = rel.replace("&amp;", "&")
        pairs.append((rel, raw_title))
    return pairs


def sogou_resolve(rel, search_url):
    """搜狗 /link -> 微信 src=11 直链"""
    status, _, body = _http_get("https://weixin.sogou.com" + rel, referer=search_url)
    if status != 200 or len(body) < 100:
        raise TransientError(f"搜狗 /link 解析响应异常 (status={status}, len={len(body)})")
    chunks = CHUNK_RE.findall(body)
    if chunks:
        return "".join(chunks)
    m = DIRECT_SRC11_RE.search(body)
    if m:
        return m.group(1)
    raise TransientError("搜狗 /link 未返回签名直链（可能触发反爬）")


def fetch_article(src11_url):
    """抓 src=11 签名文章页；被风控抛 TransientError。"""
    time.sleep(2.0)  # 微信侧节流
    status, _, body = _http_get(src11_url, referer=SG_REF)
    if status != 200:
        raise TransientError(f"微信文章页 HTTP {status}")
    if any(w in body for w in ENV_BAD_WORDS):
        raise TransientError("微信文章页触发安全验证（环境异常）")
    if len(body) < MIN_ARTICLE_BYTES or "js_content" not in body:
        raise TransientError(f"微信文章页异常 (len={len(body)})")
    return body


# ---------------------------------------------------------------------------
# 核心：搜索并定位目标日期文章
# ---------------------------------------------------------------------------
def find_article(target_day, max_candidates=8):
    """返回 dict(src11, ct, biz, mid, idx, title, url搜索串) 或 None。"""
    m, d = target_day.month, target_day.day
    queries = [
        f"读报 | 《中国建设报》{m}月{d}日",
        f"读报|《中国建设报》{m}月{d}日",
        f"读报 《中国建设报》{m}月{d}日",
    ]
    for q in queries:
        pairs = sogou_search(urllib_quote(q))
        search_url = "https://weixin.sogou.com/weixin?type=2&query=" + urllib_quote(q)
        cands = [(rel, t) for rel, t in pairs if is_read_article_title(norm_title(t), target_day)]
        if not cands:
            time.sleep(3.0)
            continue
        for rel, raw in cands[:max_candidates]:
            time.sleep(1.5)
            try:
                src11 = sogou_resolve(rel, search_url)
            except TransientError:
                continue
            if not src11:
                continue
            try:
                body = fetch_article(src11)
            except TransientError:
                time.sleep(8.0)
                continue
            ct, biz, mid, idx, title = parse_article_meta(body)
            if biz and biz != BIZ:
                continue  # 同名标题但非本账号
            if ct is None:
                continue  # 无法确认年份，跳过（防跨年同名歧义）
            pub = datetime.datetime.fromtimestamp(ct)
            if (pub.year, pub.month, pub.day) != (target_day.year, target_day.month, target_day.day):
                continue  # 往年同月同日文章
            # 命中
            return {"src11": src11, "ct": ct, "biz": biz, "mid": mid,
                    "idx": idx, "title": title or f"读报 | 《中国建设报》{m}月{d}日",
                    "query": q, "body": body}
        time.sleep(3.0)
    return None


def urllib_quote(s):
    try:
        from urllib.parse import quote
        return quote(s)
    except ImportError:  # pragma: no cover
        from urllib import quote
        return quote(s)


# ---------------------------------------------------------------------------
# 下载与校验
# ---------------------------------------------------------------------------
def already_done(out_dir, d):
    pat = os.path.join(out_dir, ACCOUNT, f"{d.isoformat()}_读报*.pdf")
    return bool(glob.glob(pat))


def run_downloader(page_html, src11, out_dir):
    """调用 download_article.py --from-html；返回 (ok, 摘要文本)。"""
    if not os.path.isfile(DOWNLOADER):
        return False, (
            "缺少微信文章下载器；请设置 READDAILY_WECHAT_DOWNLOADER，"
            "或安装 wechat-article-pdf Skill。"
        )
    cmd = [sys.executable, DOWNLOADER, "--from-html", page_html,
           "--source-url", src11, "-o", out_dir]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    tail = (proc.stdout or "") + (proc.stderr or "")
    tail = tail[-3000:]
    ok = proc.returncode == 0 and "✔ 完成" in (proc.stdout or "")
    return ok, tail


def verify_artifacts(out_dir, d):
    """返回 dict(pdf, md, json, html, images_complete) 或抛错。"""
    base = os.path.join(out_dir, ACCOUNT, f"{d.isoformat()}_读报")
    cands = glob.glob(base + "*.pdf")
    pdf = sorted(cands)[-1] if cands else None
    if not pdf or not os.path.getsize(pdf) > 100 * 1024:
        raise RuntimeError("PDF 缺失或过小")
    with open(pdf, "rb") as f:
        if f.read(5) != b"%PDF-":
            raise RuntimeError("PDF 头无效")
    md = pdf[:-4] + ".md"
    js = pdf[:-4] + ".json"
    html_f = pdf[:-4] + "_原文.html"
    meta = {}
    if os.path.exists(js):
        with open(js, encoding="utf-8") as stream:
            meta = json.load(stream)
    return {
        "pdf": os.path.basename(pdf),
        "md": os.path.basename(md) if os.path.exists(md) else None,
        "json": os.path.basename(js) if os.path.exists(js) else None,
        "html": os.path.basename(html_f) if os.path.exists(html_f) else None,
        "images_complete": meta.get("images_complete"),
        "images_downloaded": meta.get("images_downloaded"),
        "title": meta.get("title"),
        "publish_date": meta.get("publish_date"),
        "publish_time_full": meta.get("publish_time_full"),
    }


def log_entry(entry):
    os.makedirs(os.path.dirname(DAILY_LOG), exist_ok=True)
    with open(DAILY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("[log]", json.dumps(entry, ensure_ascii=False)[:400])


def notify(title, msg):
    if sys.platform != "darwin":
        print("[notify]", title, "-", msg)
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}"'],
            capture_output=True, timeout=15)
        print("[notify]", title, "-", msg)
    except Exception as e:  # noqa: BLE001
        print("[notify:skip]", e)


# ---------------------------------------------------------------------------
# 电子报：参考案例格式（电子报_YYYY-MM-DD/高清热图/N版_版名.jpg + A4 高清 PDF）
# ---------------------------------------------------------------------------
def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    return os.environ.get("CHROME_BIN") or None


def find_article_html(out_dir, d):
    pat = os.path.join(out_dir, ACCOUNT, f"{d.isoformat()}_读报*_原文.html")
    fs = sorted(glob.glob(pat), key=os.path.getmtime)
    return fs[-1] if fs else None


def parse_guide_and_pages(html_path):
    """从清洗后文章 HTML 解析 (rows=[(版号, 版名)], page_srcs=[assets 相对路径])。

    导读 = 两列表格（版号 15% + 栏目/标题 85%）：短文本=栏目名直接用作版名；
    长文本（头条标题）在 1/2 版用默认版名（要闻/综合新闻），其余版截取前 12 字。
    """
    with open(html_path, encoding="utf-8") as stream:
        h = stream.read()
    g = h.find("各版导读")
    seg = h[g:] if g >= 0 else h
    rows, pos = [], 0
    while True:
        m = re.search(r'<span leaf="">(\d+)版</span>', seg[pos:])
        if not m:
            break
        n = int(m.group(1))
        after = seg[pos + m.end():]
        txt = ""
        m2 = re.search(r'<span leaf="">([^<]*)</span>', after)
        if m2:
            txt = html_mod.unescape(m2.group(1)).strip()
        if txt and len(txt) <= 15:
            name = txt
        else:
            name = ED_NAME_DEFAULT.get(
                n, (txt[:12] if txt else f"第{n}版"))
        rows.append((n, name))
        pos += m.end()
        if pos >= len(seg):
            break
    # 版面图：导读表之后、左右滑动/编辑 标记之前的 assets 图片
    m_end = seg.find("左右滑动")
    if m_end < 0:
        m_end = seg.find("编辑")
    if m_end < 0:
        m_end = len(seg)
    page_srcs = re.findall(r'<img\s+src="(assets/[^"]+\.(?:jpg|jpeg|png|webp))"', seg[:m_end])
    if len(page_srcs) != len(rows):
        raise ValueError(
            "导读版次与版面图数量不一致 rows=%s imgs=%s" % (
                len(rows), len(page_srcs)
            )
        )
    return rows, page_srcs


def ocr_issue_number(img_path):
    """从版面图 OCR 期号（如 9167）。Vision 全图优先，失败则裁剪报头区放大再试。"""
    if not os.path.exists(VOCR):
        return None
    outs = []
    try:
        p = subprocess.run([VOCR, img_path], capture_output=True, text=True, timeout=120)
        outs.append(p.stdout)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"第(\d{3,5})期", " ".join(outs))
    if m:
        return m.group(1)
    try:
        p = subprocess.run(
            [VOCR, img_path, "--crop-top", "0.22"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        m = re.search(r"第(\d{3,5})期", p.stdout)
        return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None


def pdf_page_count(path):
    """尽力获取 PDF 页数（macOS mdls）；失败返回 None。"""
    try:
        p = subprocess.run(["mdls", "-name", "kMDItemNumberOfPages", path],
                           capture_output=True, text=True, timeout=20)
        m = re.search(r"= (\d+)", p.stdout)
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def update_latest_link(out_dir, d, pdf_name):
    """维护「最新电子报.pdf」直达指针（相对符号链接），方便每日阅读。"""
    if not pdf_name:
        return None
    link = os.path.join(out_dir, ACCOUNT, "最新电子报.pdf")
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(os.path.join(f"电子报_{d.isoformat()}", pdf_name), link)
        return link
    except OSError:
        return None


def acquire_lock(lock_path):
    """fcntl 非阻塞独占锁，防止定时任务与手动运行重叠。返回锁对象或 None（已被占用）。"""
    import fcntl
    try:
        f = open(lock_path, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        return f
    except OSError:
        return None


def generate_epaper(out_dir, d, force=False):
    """生成 电子报_YYYY-MM-DD/ 文件夹（参考案例格式）。

    返回: {"status": ok|exists|no_source|parse_failed|chrome_missing,
           "dir":..., "pdf":..., "pages":..., "qihao":...}
    """
    html_path = find_article_html(out_dir, d)
    if not html_path:
        return {"status": "no_source", "note": "未找到当天文章的 原文.html"}
    try:
        rows, page_srcs = parse_guide_and_pages(html_path)
    except ValueError as exc:
        return {"status": "parse_failed", "note": str(exc)}
    if not rows or not page_srcs:
        return {"status": "parse_failed", "note": f"导读/版面图解析失败 rows={len(rows)} imgs={len(page_srcs)}"}

    ep_dir = os.path.join(out_dir, ACCOUNT, f"电子报_{d.isoformat()}")
    if os.path.isdir(ep_dir) and not force:
        existing = sorted(glob.glob(os.path.join(ep_dir, "*_电子报_高清.pdf")),
                          key=os.path.getmtime)
        return {"status": "exists", "dir": ep_dir, "pages": len(rows),
                "pdf": os.path.basename(existing[-1]) if existing else None}

    import shutil
    hd = os.path.join(ep_dir, "高清热图")
    os.makedirs(hd, exist_ok=True)
    asset_dir = os.path.join(out_dir, ACCOUNT, "assets")
    files = []
    html_dir = os.path.dirname(html_path)
    for (n, name), src in zip(rows, page_srcs):
        src_path = os.path.join(html_dir, src)
        if not os.path.exists(src_path):
            src_path = os.path.join(asset_dir, os.path.basename(src))
        if not os.path.exists(src_path):
            return {"status": "asset_missing", "note": f"缺图 {src}"}
        fname = f"{n}版_{name}.jpg"
        output_path = os.path.join(hd, fname)
        sips = "/usr/bin/sips"
        if not os.path.isfile(sips):
            return {"status": "image_tool_missing", "note": "缺少 macOS sips 图片转换工具"}
        converted = subprocess.run(
            [sips, "--resampleWidth", str(EP_WIDTH),
             "--setProperty", "format", "jpeg",
             "--setProperty", "formatOptions", str(EP_QUALITY),
             src_path, "--out", output_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if converted.returncode != 0 or not os.path.isfile(output_path):
            detail = (converted.stderr or converted.stdout or "").strip()[-500:]
            return {"status": "image_convert_failed", "note": f"版面图转换失败：{detail}"}
        files.append(fname)

    # 期号 OCR（命名《中国建设报》YYYY-MM-DD_第XXXX期_电子报_高清.pdf）
    qihao = ocr_issue_number(os.path.join(hd, files[0]))
    pdf_name = (f"《中国建设报》{d.isoformat()}"
                + (f"_第{qihao}期" if qihao else "") + "_电子报_高清.pdf")

    chrome = find_chrome()
    if not chrome:
        return {"status": "chrome_missing", "dir": ep_dir,
                "note": f"缺 Chrome；高清热图已生成（{len(files)} 张），PDF 未渲染"}
    tmpd = tempfile.mkdtemp(prefix="cjsb_ep_")
    try:
        html_body = "\n".join(
            f'<figure class="pg" style="margin:0"><img src="高清热图/{f}" alt="{f}"></figure>'
            for f in files)
        doc = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
               '<style>@page{size:A4;margin:0}html,body{margin:0;padding:0;height:100%}'
               '.pg{width:100%;height:100vh;page-break-after:always;overflow:hidden}'
               '.pg:last-child{page-break-after:auto}'
               'img{width:100%;height:100%;object-fit:contain;display:block}</style></head><body>'
               + html_body + '</body></html>')
        doc_p = os.path.join(ep_dir, "_ep_render.html")
        with open(doc_p, "w", encoding="utf-8") as f:
            f.write(doc)
        pdf_path = os.path.join(ep_dir, pdf_name)
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
               "--disable-extensions", "--disable-background-networking",
               f"--user-data-dir={os.path.join(tmpd, 'prof')}",
               "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}",
               "file://" + doc_p]
        # Chrome 打印完成后常驻 helper 进程不退：独立进程组 + 文件稳定即停
        import signal
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=(os.name == "posix"))
        complete, stable, previous = False, 0, -1
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if os.path.exists(pdf_path):
                    size = os.path.getsize(pdf_path)
                    if size > 100 * 1024 and size == previous:
                        stable += 1
                        if stable >= 3:
                            complete = True
                            break
                    else:
                        stable = 0
                    previous = size
                time.sleep(1.0)
        finally:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:  # noqa: BLE001
                    pass
        if not complete or not os.path.exists(pdf_path):
            return {"status": "pdf_failed", "dir": ep_dir,
                    "note": "PDF 渲染超时或失败", "qihao": qihao}
        try:
            os.remove(doc_p)
        except OSError:
            pass
        with open(pdf_path, "rb") as f:
            if f.read(5) != b"%PDF-":
                return {"status": "pdf_failed", "dir": ep_dir, "note": "PDF 头无效"}
        result = {"status": "ok", "dir": ep_dir, "pdf": pdf_name,
                  "pages": len(files), "qihao": qihao}
        actual = pdf_page_count(pdf_path)
        if actual is not None:
            result["pages_actual"] = actual
            if actual != len(files):
                result["warn"] = f"PDF 页数 {actual} != 版数 {len(files)}，请人工复核"
        return result
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def sync_kb(out_dir, d, kb_dir, force=False):
    """把当日三件套同步到知识库项目目录（参考案例位置）：wechat md/pdf + 电子报 pdf。"""
    if not kb_dir:
        return []
    import shutil
    acct = os.path.join(out_dir, ACCOUNT)
    cands = sorted(glob.glob(os.path.join(acct, f"{d.isoformat()}_读报*.pdf")),
                   key=os.path.getmtime)
    names = []
    if cands:
        wechat_pdf = cands[-1]
        names.append(wechat_pdf)
        for ext in (".md",):
            f = wechat_pdf[:-4] + ext
            if os.path.exists(f):
                names.append(f)
    ep_pdfs = sorted(glob.glob(os.path.join(acct, f"电子报_{d.isoformat()}", "*.pdf")),
                     key=os.path.getmtime)
    if ep_pdfs:
        names.append(ep_pdfs[-1])
    os.makedirs(kb_dir, exist_ok=True)
    copied = []
    for src in names:
        dst = os.path.join(kb_dir, os.path.basename(src))
        if force or not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
            shutil.copy2(src, dst)
            copied.append(os.path.basename(src))
    return copied


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_once(target_day, args):
    out_dir = args.out
    # 已下载过：无需再搜索（省搜狗配额、离线可用），直接补/重建电子报 + 知识库
    if already_done(out_dir, target_day) and not args.force:
        ep = generate_epaper(out_dir, target_day, force=args.rebuild_epaper)
        ep_ok = ep.get("status") in ("ok", "exists")
        copied = sync_kb(out_dir, target_day, args.kb) if ep_ok else []
        latest = update_latest_link(out_dir, target_day, ep.get("pdf")) if ep_ok else None
        title = ""
        url = ""
        try:
            cands = sorted(glob.glob(os.path.join(
                out_dir, ACCOUNT, f"{target_day.isoformat()}_读报*.json")),
                key=os.path.getmtime)
            if cands:
                meta = json.load(open(cands[-1], encoding="utf-8"))
                url = meta.get("url") or ""
                title = meta.get("title") or ""
        except Exception:  # noqa: BLE001
            pass
        entry = {"date": target_day.isoformat(), "status": ("exists" if ep_ok else "epaper_failed"),
                 "title": title, "url": url,
                 "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                 "note": "当日产物已存在，跳过下载", "epaper": ep,
                 "kb_copied": copied, "latest_link": latest}
        log_entry(entry)
        if not args.no_notify:
            if ep_ok:
                notify("中国建设报 · 每日读报",
                       f"{target_day.month}月{target_day.day}日电子报已就绪（{ep.get('pages')} 版），可开始读报")
            else:
                notify("中国建设报 · 每日读报 ⚠️",
                       f"{target_day.month}月{target_day.day}日电子报生成失败：{ep.get('note') or ep.get('status')}")
        return entry
    try:
        found = find_article(target_day)
    except TransientError as e:
        return {"status": "search_transient_error", "error": str(e), "target": target_day.isoformat()}
    if not found:
        return {"status": "not_found", "target": target_day.isoformat(),
                "note": "搜狗未收录当日文章（可能尚未发布或索引滞后）"}

    import shutil
    tmp = tempfile.mkdtemp(prefix="jianshebao_page_")
    page_html = os.path.join(tmp, "page.html")
    try:
        s11 = found["src11"]
        body = found.get("body") or ""
        if len(body) < MIN_ARTICLE_BYTES:
            return {"status": "fetch_failed", "target": target_day.isoformat(),
                    "error": f"文章页体积异常 {len(body)}"}
        with open(page_html, "w", encoding="utf-8") as f:
            f.write(body)
        # 下载/渲染失败时快速内重试（60s 间隔 × 3），避免整段主循环重试
        ok, tail = False, ""
        for attempt in range(3):
            ok, tail = run_downloader(page_html, s11, out_dir)
            if ok:
                break
            print(f"   downloader attempt {attempt+1} 失败，60s 后重试")
            time.sleep(60)
        if not ok:
            return {"status": "download_failed", "target": target_day.isoformat(),
                    "url": s11, "tail": tail}
        art = verify_artifacts(out_dir, target_day)
        ep = generate_epaper(out_dir, target_day, force=(args.force or args.rebuild_epaper))
        ep_ok = ep.get("status") in ("ok", "exists")
        copied = sync_kb(out_dir, target_day, args.kb) if ep_ok else []
        latest = update_latest_link(out_dir, target_day, ep.get("pdf")) if ep_ok else None
        entry = {"date": target_day.isoformat(), "status": "ok" if ep_ok else "epaper_failed",
                 "title": art.get("title") or found["title"],
                 "url": s11,
                 "publish_time_full": art.get("publish_time_full"),
                 "images_downloaded": art.get("images_downloaded"),
                 "images_complete": art.get("images_complete"),
                 "files": art, "epaper": ep, "kb_copied": copied,
                 "latest_link": latest,
                 "ts": datetime.datetime.now().isoformat(timespec="seconds")}
        log_entry(entry)
        if not args.no_notify:
            if ep_ok:
                notify("中国建设报 · 每日读报",
                       f"{target_day.month}月{target_day.day}日电子报已就绪 ✅ "
                       f"（{ep.get('pages')} 版，第{ep.get('qihao') or '?'}期）")
            else:
                notify("中国建设报 · 每日读报 ⚠️",
                       f"{target_day.month}月{target_day.day}日电子报生成失败：{ep.get('note') or ep.get('status')}")
        return entry
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="中国建设报每日读报自动抓取")
    ap.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--force", action="store_true", help="已下载过也重新下载（不覆盖旧文件）")
    ap.add_argument("--max-retries", type=int, default=6, help="最多重试次数（默认 6，0=不重试）")
    ap.add_argument("--retry-interval", type=int, default=0,
                    help="固定重试间隔秒；默认按 --retry-gaps 时间表")
    ap.add_argument("--retry-gaps", default="600,900,1200,1500,1800",
                    help="重试等待时间表（秒，逗号分隔），默认 600,900,1200,1500,1800")
    ap.add_argument("--no-notify", action="store_true", help="不发 macOS 通知")
    ap.add_argument("--rebuild-epaper", action="store_true",
                    help="重建电子报文件夹（高清热图 + 高清 PDF），即使已存在")
    ap.add_argument("--kb", default=DEFAULT_KB,
                    help="知识库同步目录（默认 ~/Maitty的知识库/08-DeepSeek Harness/中国建设报-电子报PDF下载）")
    ap.add_argument("--no-kb", action="store_true", help="不同步知识库")
    args = ap.parse_args()
    if args.no_kb:
        args.kb = None

    # 进程锁：定时任务与手动运行互斥（防重叠；被占用时直接退出）
    os.makedirs(args.out, exist_ok=True)
    lock = acquire_lock(os.path.join(args.out, ".jianshebao.lock"))
    if lock is None:
        print("已有实例在运行，本次退出")
        sys.exit(0)

    if args.date:
        target = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = datetime.date.today()

    gaps = ([args.retry_interval] * args.max_retries
            if args.retry_interval > 0
            else [int(x) for x in args.retry_gaps.split(",")][:args.max_retries])

    print(f"== 中国建设报每日读报 | 目标 {target} | {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")

    entry = None
    attempt = 0
    while True:
        if attempt:
            wait = gaps[attempt - 1] if attempt - 1 < len(gaps) else 1800
            print(f"-- 第 {attempt} 次重试（{wait}s 后）--")
            time.sleep(wait)
        entry = run_once(target, args)
        if entry.get("status") in ("ok", "exists", "download_failed", "epaper_failed"):
            break
        print(f"   {entry.get('status')}: {entry.get('error') or entry.get('note') or ''}")
        attempt += 1
        if attempt > len(gaps):
            break

    if entry and entry.get("status") in ("not_found", "search_transient_error", "fetch_failed"):
        # 今日确认无果后回退昨天：保障「每日有报可读」（若昨天产物已存在则直接 exists）
        yesterday = target - datetime.timedelta(days=1)
        print(f"-- 今日未果，回退 {yesterday} --")
        fb = run_once(yesterday, args)
        if fb and fb.get("status") in ("ok", "exists"):
            fb["fallback_from"] = target.isoformat()
            entry = fb
    else:
        pass

    print("== 结果 ==")
    print(json.dumps(entry, ensure_ascii=False, indent=2) if entry else "无结果")
    sys.exit(0 if entry and entry.get("status") in ("ok", "exists") else 1)


if __name__ == "__main__":
    main()
