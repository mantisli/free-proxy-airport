#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 自动聚合 / 自愈配置生成器
=================================================
每次运行做的事：
  1. 从「自动维护的接口清单文档」里动态抽取候选接口（点播源 + 直播源）
  2. 并发探测每个候选是否真的可用（点播=合法TVBox配置JSON / 直播=含流的m3u）
  3. 把可用的点播源合并成一份配置（去重、补全必需字段）
  4. 把可用的直播源 m3u 挂到 lives 段
  5. 输出 tvbox.json + STATUS.md（运行报告）
  6. 自愈：点播源全军覆没时保留上一版可用配置，不让订阅挂掉

只依赖 Python 标准库，不需要 pip install。
"""

import base64
import concurrent.futures as futures
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 tvbox-airport/1.0"
    )
}
# 饭太硬/菜妮丝等家族源只对 okhttp UA 返回 JSON（Chrome UA 会返回 HTML 中间页），
# 所以加一个回退 UA。TVBox 客户端本身就是 okhttp，用它最贴合真实场景。
UA_OKHTTP = {"User-Agent": "okhttp/4.12.0"}

TIMEOUT = 12
MAX_WORKERS = 12
MAX_VOD_SITES = 300
OUT_JSON = "tvbox.json"
OUT_STATUS = "STATUS.md"

# --------------------------------------------------------------------------
# 候选清单来源：这些文档由 tvapp_store 仓库的脚本自动更新，属于「动态候选池」
# --------------------------------------------------------------------------
HELP_DOCS = [
    "https://raw.githubusercontent.com/52liulian/tvapp_store/main/help/"
    + urllib.parse.quote("3.TVBox配置指南及接口地址.md"),
    "https://raw.githubusercontent.com/52liulian/tvapp_store/main/help/"
    + urllib.parse.quote("4.影视仓配置指南及接口地址.md"),
    "https://raw.githubusercontent.com/52liulian/tvapp_store/main/help/"
    + urllib.parse.quote("5.IPTV直播源汇总.md"),
]

# 手工确认可用的种子源（实测过能返回合法内容）
SEED_VOD = [
    "https://raw.githubusercontent.com/xyq254245/xyqonlinerule/main/XYQTVBox.json",
    "http://home.jundie.top:81/top98.json",
    "https://tv.xn--yhqu5zs87a.top",
]
SEED_LIVE = [
    "https://live.zbds.top/tv/iptv4.m3u",
    "https://live.zbds.top/tv/iptv6.m3u",
    "https://raw.githubusercontent.com/YanG-1989/m3u/main/Gather.m3u",
    "https://live.yang-1989.eu.org/Live.m3u",
]

SKIP_PAT = re.compile(
    r"(\.apk|\.zip|\.rar|\.png|\.jpg|\.jpeg|\.gif|\.mp4|\.mp3|\.exe|"
    r"\.dmg|/app/|/icons/|/images/|/css/|/js/|github\.com/52liulian|"
    r"baidu|pan\.|aliyundrive|123pan| quark|telegram|t\.me/|weiyun)",
    re.I,
)

# --------------------------------------------------------------------------
# 内置保底配置：首次运行时如果所有外部源都挂了（网络抽风 / 源全失效），
# 至少保证订阅能正常加载，不让用户第一次部署就看到一个红叉。
# 下面 4 个 CMS 接口是实测 HTTP 200 且 code=1 的公开采集站，不依赖任何 GitHub release。
# --------------------------------------------------------------------------
FALLBACK_SPIDER = "http://home.jundie.top:81/jar/top98_1.jar"
FALLBACK_SITES = [
    {
        "key": "dyttzy",
        "name": "电影天堂",
        "type": 1,
        "api": "https://caiji.dyttzyapi.com/api.php/provide/vod",
        "enabled": True,
    },
    {
        "key": "apibdzy",
        "name": "宝岛资源",
        "type": 1,
        "api": "https://api.apibdzy.com/api.php/provide/vod",
        "enabled": True,
    },
    {
        "key": "ffzyapi",
        "name": "非凡资源",
        "type": 1,
        "api": "https://api.ffzyapi.com/api.php/provide/vod",
        "enabled": True,
    },
    {
        "key": "lziapi",
        "name": "量子资源",
        "type": 1,
        "api": "https://cj.lziapi.com/api.php/provide/vod",
        "enabled": True,
    },
]

FALLBACK = {
    "spider": FALLBACK_SPIDER,
    "sites": FALLBACK_SITES,
    "parses": [
        {"name": "Json并发", "type": 2, "url": "Parallel"},
        {"name": "Json顺序", "type": 2, "url": "Series"},
    ],
    "flags": [],
    "lives": [
        {
            "name": "央视卫视·IPv4",
            "type": "m3u",
            "url": "https://live.zbds.top/tv/iptv4.m3u",
            "epg": "https://eepg.51zmt.top:8000/e.xml",
            "group": "直播",
            "playerType": 1,
        },
    ],
    "wallpaper": "",
}
VOD_HINT = re.compile(r"(\.json|\.txt|接口|config|/tv\b|tvbox|box/|api)", re.I)
LIVE_HINT = re.compile(r"(\.m3u8?|\.txt|live|直播|iptv)", re.I)


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def sanitize_url(url):
    """清洗抽取到的 URL：去掉尾部杂质 + 对非 ASCII 路径做百分号编码"""
    url = url.strip().rstrip(".,;:!、，。；")
    # 文档里常见的 "xxx(1)" "配置(示例" 这类尾巴，直接从第一个括号处截断
    m = re.search(r"[（(\[【<]", url)
    if m and m.start() > len("https://"):
        url = url[:m.start()]
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme and any(ord(ch) > 127 for ch in parts.netloc + parts.path):
        netloc = parts.netloc.encode("idna").decode("ascii") if any(
            ord(ch) > 127 for ch in parts.netloc) else parts.netloc
        path = urllib.parse.quote(parts.path, safe="/%")
        query = urllib.parse.quote(parts.query, safe="=&%/:+")
        url = urllib.parse.urlunsplit(
            (parts.scheme, netloc, path, query, parts.fragment))
    return url


def mirror_candidates(url):
    """GitHub 文件的 CDN 镜像回退。

    源站直连经常被墙或握手超时（raw.githubusercontent.com 尤其明显），
    但 CDN 边缘节点往往能通。这里返回等价地址，按顺序重试。
    """
    m = re.match(
        r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", url)
    if not m:
        return []
    owner, repo, ref, path = m.groups()
    return [
        f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/{path}",
        f"https://fastly.jsdelivr.net/gh/{owner}/{repo}@{ref}/{path}",
        f"https://raw.gitmirror.com/{owner}/{repo}/{ref}/{path}",
        f"https://gh-proxy.com/{url}",
    ]


def _looks_like_html(raw):
    """判断响应是不是 HTML 页面（而非 JSON / m3u / 纯文本）。"""
    head = raw[:400].lstrip().lower()
    return head.startswith(b"<!doctype") or b"<html" in head or b"<head" in head


def _fetch(target, timeout, want_text):
    """单次抓取：主 UA 拿到 HTML 时自动用 okhttp UA 重试。

    饭太硬/菜妮丝等家族源只对 okhttp UA 返回 JSON，Chrome UA 会拿到 HTML 中间页，
    所以这里做一次 UA 回退，避免把「其实能用的源」误判成失效。
    """
    req = urllib.request.Request(sanitize_url(target), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw.strip():
        raise ValueError("空响应")
    if want_text and _looks_like_html(raw):
        req2 = urllib.request.Request(sanitize_url(target), headers=UA_OKHTTP)
        with urllib.request.urlopen(req2, timeout=timeout) as resp2:
            raw2 = resp2.read()
        if raw2.strip() and not _looks_like_html(raw2):
            raw = raw2
    return raw


def http_get(url, timeout=TIMEOUT, want_text=True, retries=1, use_mirror=True):
    """直连（含 UA 回退）-> 重试 -> CDN 镜像回退。任一路径成功即返回。"""
    attempts = []
    for attempt in range(retries + 1):
        attempts.append(url)
    if use_mirror:
        attempts += mirror_candidates(url)

    last = None
    for i, target in enumerate(attempts):
        try:
            raw = _fetch(target, timeout, want_text)
            break
        except Exception as exc:
            last = exc
            if i < len(attempts) - 1:
                time.sleep(1.0 if i == 0 else 0.5)
    else:
        raise last

    if not want_text:
        return raw
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def maybe_json(text):
    """兼容 json / base64(json) / 带 BOM 的情况"""
    text = text.strip().lstrip("﻿")
    if not text:
        raise ValueError("空响应")
    if text[0] not in "{[":
        compact = "".join(text.split())
        if len(compact) > 16:
            try:
                text = base64.b64decode(compact + "=" * (-len(compact) % 4)).decode(
                    "utf-8", "ignore"
                )
            except Exception:
                pass
    return json.loads(text)


# --------------------------------------------------------------------------
# 候选抽取
# --------------------------------------------------------------------------
def harvest_docs():
    vod, live = set(), set()
    for doc in HELP_DOCS:
        try:
            text = http_get(doc, timeout=15)
        except Exception as exc:
            log(f"清单文档拉取失败 {doc.rsplit('/', 1)[-1]}: {exc}")
            continue
        found = re.findall(r"https?://[^\s\)\]\}>，。、；'\"`]+", text)
        added = 0
        for url in found:
            url = sanitize_url(url)
            if not url or SKIP_PAT.search(url):
                continue
            # 命中任一特征就都去试一遍，最终按内容分类（比按名字猜更准）
            if LIVE_HINT.search(url) or VOD_HINT.search(url):
                vod.add(url)
                live.add(url)
                added += 1
        log(f"清单 {doc.rsplit('/', 1)[-1]}: 抽出 {added} 个候选")
    return list(vod)[:40], list(live)[:20]


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------
def probe_vod(url, retries=1):
    try:
        data = maybe_json(http_get(url, retries=retries))
    except Exception as exc:
        return url, None, f"{type(exc).__name__}: {str(exc)[:60]}"
    if not isinstance(data, dict):
        return url, None, "不是JSON对象"
    sites = data.get("sites")
    spider = data.get("spider")
    if not isinstance(sites, list) or not sites:
        return url, None, "无sites字段"
    if not spider and not any(isinstance(s, dict) and s.get("api") for s in sites):
        return url, None, "无spider且无api"
    return url, data, None


def count_streams(text):
    return len(re.findall(r"^#EXTINF", text, re.M)), len(
        re.findall(r"https?://[^\s\"']+", text)
    )


def probe_live(url):
    try:
        text = http_get(url, timeout=15)
    except Exception as exc:
        return url, None, f"{type(exc).__name__}: {str(exc)[:60]}"
    if "#EXTINF" not in text:
        return url, None, "不是m3u"
    extinf, links = count_streams(text)
    if links < 5:
        return url, None, f"流太少({links})"
    # m3u 内部通常是裸相对路径或绝对流地址，原样保留即可；
    # 这里记录的是 m3u 本体地址，若本体来自 GitHub 会被镜像替换，需回填真实地址
    return url, {"name": "", "type": "m3u", "url": url, "extinf": extinf,
                 "links": links}, None


def run_pool(fn, urls, label):
    ok, bad = [], []
    if not urls:
        return ok, bad
    t0 = time.time()
    with futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(fn, u): u for u in urls}
        for fut in futures.as_completed(futs):
            url, data, err = fut.result()
            if data is not None:
                ok.append((url, data))
            else:
                bad.append((url, err))
    log(f"{label}: 可用 {len(ok)} / 失效 {len(bad)}（耗时 {time.time() - t0:.0f}s）")
    return ok, bad


# --------------------------------------------------------------------------
# 合并
# --------------------------------------------------------------------------
def _puny_url(url):
    """把 URL 里的中文域名转成 punycode（兼容老旧机顶盒的 DNS 解析）。"""
    if not isinstance(url, str) or not url:
        return url
    try:
        p = urllib.parse.urlsplit(url)
        host = p.netloc.rsplit(":", 1)[0]
        if any(ord(ch) > 127 for ch in host):
            ph = host.encode("idna").decode("ascii")
            port = ":" + p.netloc.rsplit(":", 1)[1] if ":" in p.netloc else ""
            net = ph + port
            return urllib.parse.urlunsplit((p.scheme, net, p.path, p.query, p.fragment))
    except Exception:
        pass
    return url


def absolutize(value, base_url):
    """把配置里的相对路径（./jar/xxx.jar 之类）补成绝对地址 + 中文域名转 punycode。

    这是合并配置的关键：原配置托管在别人仓库里，jar/spider 用的是相对路径，
    直接搬到我们的仓库会 404，必须按「原配置 URL」为基准换算成绝对地址。
    """
    if not isinstance(value, str) or not value:
        return value
    v = value.strip()
    if v.startswith(("http://", "https://", "assets://", "file://", "clan://", "ext://")):
        # 带 md5 校验后缀的 "https://xxx.jar;md5;xxxx" 只对 URL 部分转 punycode
        if ";" in v:
            head, tail = v.split(";", 1)
            return _puny_url(head) + ";" + tail
        return _puny_url(v)
    # 纯模块名（如 csp_Bili / csp_Drpy）不是路径，原样保留
    if re.fullmatch(r"[\w.\-]+", v):
        return v
    # 带 md5 校验后缀的 "./xxx.jar;md5;xxxx" 要一起处理
    parts = v.split(";")
    parts[0] = urllib.parse.urljoin(base_url, parts[0])
    return ";".join(parts)


def normalize_site(s, base_url, src_spider=""):
    if not isinstance(s, dict):
        return None
    key = s.get("key")
    name = s.get("name")
    typ = s.get("type")
    api = s.get("api")
    if not key or not name or typ is None or not api:
        return None
    if str(api).strip() in ("", "null", "None"):
        return None
    out = dict(s)  # 原样复制，保留 ext(dict) / type(数字) 等字段
    out["key"] = str(key)
    out["name"] = str(name)
    out["api"] = absolutize(str(api), base_url)
    # jar / spider：绝对化 + 中文域名转 punycode
    for k in ("jar", "spider"):
        v = s.get(k)
        if isinstance(v, str) and v:
            out[k] = absolutize(v, base_url)
    # 关键：csp_ 模块由对应源自己的 spider jar 提供。
    # 合并多源时若只留顶栏一个 jar，这些站会解析不到模块，所以给站点单独挂上它来源的 jar。
    if not str(out["api"]).startswith(("http://", "https://")):
        if not out.get("jar") and not out.get("spider"):
            out["spider"] = src_spider
    return out


def merge_vod(ok_list):
    """ok_list: [(url, config)] -> (base, sites, parses, flags, wallpaper)"""
    scored = []
    for url, cfg in ok_list:
        sites = [s for s in (cfg.get("sites") or []) if isinstance(s, dict)]
        has_spider = 1 if cfg.get("spider") else 0
        # 优先选：有 spider 且站点多的做主源
        scored.append((has_spider, len(sites), url, cfg))
    if not scored:
        return None, [], [], [], "", ""
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    _, _, base_url, base = scored[0]
    if base.get("spider"):
        base = dict(base)
        base["spider"] = absolutize(base["spider"], base_url)
    seen, sites = set(), []
    for _, _, url, cfg in scored:
        src_spider = cfg.get("spider") or ""
        if src_spider:
            src_spider = absolutize(src_spider, url)
        for s in cfg.get("sites") or []:
            n = normalize_site(s, url, src_spider)
            if not n:
                continue
            k = (n["key"], n["api"])
            if k in seen:
                continue
            seen.add(k)
            sites.append(n)
            if len(sites) >= MAX_VOD_SITES:
                break
        if len(sites) >= MAX_VOD_SITES:
            break

    def dedup_by_name(items):
        out, seen = [], set()
        for it in items or []:
            if not isinstance(it, dict):
                continue
            nm = it.get("name")
            if not nm or nm in seen:
                continue
            seen.add(nm)
            out.append(it)
        return out

    parses, flags = [], []
    for _, _, _, cfg in scored:
        parses += dedup_by_name(cfg.get("parses"))
        flags += dedup_by_name(cfg.get("flags"))
    wallpaper = ""
    for _, _, _, cfg in scored:
        if isinstance(cfg.get("wallpaper"), str) and cfg["wallpaper"].startswith("http"):
            wallpaper = cfg["wallpaper"]
            break
    log(f"主源(base)={base_url} sites={len(sites)} parses={len(parses)} flags={len(flags)}")
    return base, sites, parses[:30], flags[:30], wallpaper, base_url


def build_lives(ok_list):
    lives, seen = [], set()
    labels = {
        "live.zbds.top/tv/iptv4.m3u": "央视卫视·IPv4(每6小时更新)",
        "live.zbds.top/tv/iptv6.m3u": "央视卫视·IPv6(每6小时更新)",
        "YanG-1989/m3u/main/Gather.m3u": "全球直播源·精简版",
        "live.yang-1989.eu.org/Live.m3u": "全球直播源·多平台",
    }
    for url, info in ok_list:
        if url in seen:
            continue
        seen.add(url)
        label = next((v for k, v in labels.items() if k in url), "")
        if not label:
            label = f"直播源 {url.rsplit('/', 1)[-1] or url}"
        lives.append({
            "name": label,
            "type": "m3u",
            "url": url,
            "epg": "https://eepg.51zmt.top:8000/e.xml",
            "group": "直播",
            "playerType": 1,
            "extinf": info.get("extinf", 0),
        })
    return lives


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def load_existing():
    if os.path.exists(OUT_JSON):
        try:
            with open(OUT_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def main():
    started = datetime.datetime.now()
    log("=== TVBox 配置生成开始 ===")

    doc_vod, doc_live = harvest_docs()
    vod_candidates = list(dict.fromkeys(SEED_VOD + doc_vod))
    live_candidates = list(dict.fromkeys(SEED_LIVE + doc_live))
    log(f"候选: 点播 {len(vod_candidates)} 个 / 直播 {len(live_candidates)} 个")

    vod_ok, vod_bad = run_pool(probe_vod, vod_candidates, "点播源探测")
    live_ok, live_bad = run_pool(probe_live, live_candidates, "直播源探测")

    # 二次抢救：第一轮全挂时，对 GitHub 托管的候选加大超时再试一次
    if not vod_ok:
        gh_retry = [u for u, _ in vod_bad if "github.com" in u or "githubusercontent" in u]
        if gh_retry:
            log(f"点播源全挂，对 {len(gh_retry)} 个 GitHub 源加大超时重试")
            retry_ok, retry_bad = run_pool(
                lambda u: probe_vod(u, retries=2), gh_retry[:12], "GitHub 源重试")
            vod_ok = retry_ok
            vod_bad = retry_bad + [b for b in vod_bad if b[0] not in gh_retry]

    base, sites, parses, flags, wallpaper, base_url = merge_vod(vod_ok)
    lives = build_lives(live_ok)

    existing = load_existing()
    degraded = False
    if not sites:
        if existing and existing.get("sites"):
            log("!! 本次点播源全部失效，保留上一版配置（自愈）")
            base = existing
            sites = existing["sites"]
            parses = existing.get("parses", [])
            flags = existing.get("flags", [])
            wallpaper = existing.get("wallpaper", wallpaper)
            degraded = True
        else:
            log("!! 点播源全挂且无历史配置，启用内置保底配置")
            base = FALLBACK
            sites = FALLBACK["sites"]
            parses = FALLBACK["parses"]
            flags = FALLBACK["flags"]
            wallpaper = FALLBACK["wallpaper"]
            degraded = True
    if not lives and existing and existing.get("lives"):
        lives = existing["lives"]
        degraded = True
        log("!! 本次直播源全部失效，保留上一版直播配置")

    if not lives:
        if FALLBACK["lives"]:
            lives = list(FALLBACK["lives"])
            degraded = True
            log("!! 无直播源可用，回落到内置直播源（不影响点播）")
        else:
            log("!! 无直播源可用（不影响点播）")

    config = {
        "wallpaper": wallpaper,
        "spider": (base or {}).get("spider", ""),
        "sites": sites,
        "lives": lives,
        "parses": parses,
        "flags": flags,
        "metadata": {
            "generated_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "tvbox-airport",
            "degraded": degraded,
            "degraded_reason": (
                "点播源与历史配置均不可用，已回落内置保底配置" if not vod_ok and degraded
                else ("部分源失效，已保留上一版配置" if degraded else "")
            ),
            "vod_sources_ok": [u for u, _ in vod_ok],
            "live_sources_ok": [u for u, _ in live_ok],
        },
    }
    if not config["spider"]:
        log("!! 警告: spider 字段为空，TVBox 可能无法加载")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    log(f"已写出 {OUT_JSON}: {len(sites)} 个点播 / {len(lives)} 个直播")

    write_status(started, vod_ok, vod_bad, live_ok, live_bad, config, len(sites), degraded)

    if not sites:
        return 1
    return 0


def write_status(started, vod_ok, vod_bad, live_ok, live_bad, config, n_sites, degraded):
    reason = ""
    if degraded:
        if config and not vod_ok:
            reason = "\n- 降级原因：点播源全部失效且无历史配置，本次使用**内置保底配置**"
        else:
            reason = "\n- 降级原因：部分源本次失效，已自动保留上一版可用配置（订阅不会中断）"
    lines = [
        "# 运行状态报告",
        "",
        f"- 最后运行：{started:%Y-%m-%d %H:%M:%S} (UTC+8)",
        f"- 结果：{'⚠️ 部分降级' if degraded else '✅ 正常'}{reason}",
        f"- 点播源：可用 {len(vod_ok)} / 失效 {len(vod_bad)}，最终收录 {n_sites} 个站点",
        f"- 直播源：可用 {len(live_ok)} / 失效 {len(live_bad)}",
        "",
        "## ✅ 点播源可用",
        "",
    ]
    lines += [f"- {u}" for u, _ in vod_ok] or ["- （无）"]
    lines += ["", "## 📺 直播源可用", ""]
    lines += [f"- {u}（{d.get('extinf', 0)} 个频道）" for u, d in live_ok] or ["- （无）"]
    lines += ["", "## ❌ 失效候选（已自动剔除）", ""]
    lines += [f"- {u} — {e}" for u, e in (vod_bad + live_bad)] or ["- （无）"]
    lines += [
        "",
        "---",
        "",
        "由 [tvbox-airport](https://github.com/) 自动生成，每 6 小时自愈一次。",
        "",
    ]
    with open(OUT_STATUS, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 兜底：生成器自身异常也不要让工作流红着失败
        log(f"生成器异常: {type(exc).__name__}: {exc}")
        sys.exit(1)
