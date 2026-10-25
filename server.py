#!/usr/bin/env python3
"""MissAV 非公式クライアント — サーバー本体（スクレイプ / API / HLSプロキシ / 静的配信）"""
from __future__ import annotations

import json, os, re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse, urlunparse

from curl_cffi import requests as creq
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from lxml import html as LH

ROOT = Path(__file__).resolve().parent
STATIC, DATA = ROOT / "static", ROOT / "data"
DATA.mkdir(exist_ok=True)
COOKIE_FILE = DATA / "cookies.json"

MIRRORS = [m.strip().rstrip("/") for m in os.getenv(
    "MISS_MIRRORS", "https://missav.ai,https://missav.ws,https://missav.com").split(",") if m.strip()]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LANGS = {"ja": "ja-JP", "en": "en-US", "cn": "zh-CN", "zh": "zh-CN", "tw": "zh-TW",
         "ko": "ko-KR", "th": "th-TH", "vi": "vi-VN", "id": "id-ID",
         "de": "de-DE", "fr": "fr-FR", "pt": "pt-BR", "": "en-US"}

SESSION = creq.Session(impersonate="chrome")
STATE = {"host": MIRRORS[0]}

# 一覧ソース: サイト改変に耐えるよう候補パスを複数持つ
SOURCES: dict[str, list[str]] = {
    "new": ["/new"], "release": ["/release"],
    "hot-today": ["/today-hot", "/dm291/today-hot"],
    "hot-weekly": ["/weekly-hot", "/dm169/weekly-hot"],
    "hot-monthly": ["/monthly-hot", "/dm256/monthly-hot"],
    "uncensored": ["/uncensored-leak", "/dm620/uncensored-leak"],
    "fc2": ["/fc2", "/dm97/fc2"],
    "heyzo": ["/heyzo", "/dm53663/heyzo"],
    "siro": ["/siro", "/dm23/siro"],
    "luxu": ["/luxu", "/dm20/luxu"],
    "madou": ["/madou", "/dm34/madou"],
    "vr": ["/genres/VR", "/dm2091/genres/VR"],
    "chinese-subtitle": ["/chinese-subtitle", "/dm265/chinese-subtitle"],
    "english-subtitle": ["/english-subtitle", "/dm265/english-subtitle"],
    "saved": ["/saved"], "history": ["/history"],
}

# ───────────────── HTTP ─────────────────
def _swap_host(url: str, host: str) -> str:
    a, b = urlparse(url), urlparse(host)
    return urlunparse(a._replace(scheme=b.scheme, netloc=b.netloc))


def _is_ok(r) -> bool:
    return (r.status_code == 200 and len(r.text) > 800
            and not ("Just a moment" in r.text and "cf-chl" in r.text))


def _load_cookies() -> None:
    if COOKIE_FILE.exists():
        try:
            for k, v in json.loads(COOKIE_FILE.read_text("utf-8")).items():
                SESSION.cookies.set(k, v)
        except Exception:
            pass


def _save_cookies() -> None:
    try:
        COOKIE_FILE.write_text(json.dumps(dict(SESSION.cookies.get_dict())), "utf-8")
    except Exception:
        pass


_load_cookies()


def get(url: str, params: dict | None = None, lang: str = "ja",
        referer: str | None = None, timeout: int = 30, allow_fail: bool = False):
    """ミラーを順に試してHTMLを取得。成功したホストを記憶する。"""
    errors: list[str] = []
    for host in [STATE["host"]] + [m for m in MIRRORS if m != STATE["host"]]:
        target = _swap_host(url, host)
        try:
            r = SESSION.get(target, params=params, timeout=timeout,
                            headers={"User-Agent": UA,
                                     "Accept-Language": LANGS.get(lang, "ja-JP"),
                                     "Accept": "text/html,application/xhtml+xml,*/*",
                                     "Referer": referer or target})
            if _is_ok(r):
                STATE["host"] = host
                _save_cookies()
                return r
            errors.append(f"{host}:{r.status_code}")
        except Exception as exc:
            errors.append(f"{host}:{type(exc).__name__}")
    if allow_fail:
        return None
    raise HTTPException(502, detail="missav 取得失敗 → " + ", ".join(errors))


def page_url(path: str, lang: str = "ja") -> str:
    lang = lang if lang in LANGS else "ja"
    if path and not path.startswith("/"):
        path = "/" + path
    return f"{STATE['host']}/{lang}{path}" if lang else f"{STATE['host']}{path}"


def fetch_first(paths: list[str], lang: str, params: dict | None = None):
    last = None
    for p in paths:
        try:
            return p, get(page_url(p, lang), params=params, lang=lang)
        except HTTPException as exc:
            last = exc
    raise last or HTTPException(404, "not found")


# ───────────────── パーサ ─────────────────
def txt(el) -> str:
    return " ".join(el.text_content().split()) if el is not None else ""


DUR_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
CODE_RE = re.compile(r"^[A-Za-z0-9]{2,14}[-_ ]?\d{2,6}$")


def _code_from_href(href: str) -> str:
    return (href or "").split("?")[0].rstrip("/").split("/")[-1].upper()


def parse_cards(tree) -> list[dict[str, Any]]:
    nodes = tree.xpath(
        "//div[contains(concat(' ',normalize-space(@class),' '),' aspect-w-16 ') "
        "and contains(concat(' ',normalize-space(@class),' '),' aspect-h-9 ')]")
    if not nodes:
        nodes = tree.xpath("//a[.//img and .//video]/..")
    if not nodes:
        nodes = tree.xpath("//div[.//a[.//img[@data-src]]]")

    out, seen = [], set()
    for node in nodes:
        anchors = node.xpath("./a[@href]") or node.xpath(".//a[@href]")
        if not anchors:
            continue
        main, img = anchors[0], node.xpath(".//a[@href]//img")
        if not img:
            continue
        img = img[0]
        href = main.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        vids = main.xpath(".//video")
        number = (main.get("alt") or "").strip()
        if not CODE_RE.match(number):
            number = _code_from_href(href)
        duration = subtitle = ""
        for extra in anchors[1:]:
            t = txt(extra)
            if DUR_RE.match(t):
                duration = t
            elif t and len(t) < 12:
                subtitle = t
        out.append({"title": (img.get("alt") or main.get("title") or txt(main)).strip(),
                    "link": href, "number": number,
                    "cover": img.get("data-src") or img.get("src") or "",
                    "preview": (vids[0].get("data-src") or vids[0].get("src") or "") if vids else "",
                    "duration": duration, "subtitle": subtitle})
    return out


def parse_last_page(tree) -> int:
    last = 0
    for a in tree.xpath('//a[contains(@aria-label,"Go to page")]'):
        m = re.search(r"(\d+)", a.get("aria-label", ""))
        if m:
            last = max(last, int(m.group(1)))
    if not last:
        for a in tree.xpath('//a[contains(@href,"page=")]'):
            m = re.search(r"page=(\d+)", a.get("href", ""))
            if m:
                last = max(last, int(m.group(1)))
    return last


def _actress_box(tree):
    b = tree.xpath("//div[contains(@class,'p-8') and contains(@class,'bg-nord1')]")
    return b[0] if b else None


def parse_actresses(tree) -> list[dict[str, Any]]:
    box = _actress_box(tree)
    items = box.xpath(".//li") if box is not None else tree.xpath("//li[.//a[.//h4]]")
    out, seen = [], set()
    for li in items:
        anchors = li.xpath(".//a[@href]")
        if not anchors:
            continue
        link = next((a for a in anchors if a.xpath(".//h4")), anchors[-1])
        h4 = link.xpath(".//h4")
        if not h4:
            continue
        name = txt(h4[0])
        if not name or name in seen:
            continue
        seen.add(name)
        ps = link.xpath(".//p")
        m = re.search(r"\d+", txt(ps[0])) if ps else None
        img = li.xpath(".//img")
        out.append({"name": name, "link": link.get("href", ""),
                    "videos": int(m.group(0)) if m else 0,
                    "debut": txt(ps[1]) if len(ps) > 1 else "",
                    "cover": (img[0].get("src") or img[0].get("data-src")) if img else ""})
    return out


def parse_taxonomy(tree) -> list[dict[str, Any]]:
    out, seen = [], set()
    for box in tree.xpath("//div[contains(@class,'grid') and contains(@class,'gap-4')]"):
        for div in box.xpath("./div"):
            a = div.xpath("./a[@href]")
            if not a:
                continue
            name = txt(a[0])
            if not name or name in seen:
                continue
            seen.add(name)
            p = div.xpath("./p")
            m = re.search(r"\d+", txt(p[0])) if p else None
            out.append({"name": name, "link": a[0].get("href", ""),
                        "count": int(m.group(0)) if m else None})
    return out


LABELS = {
    "number": ["品番", "番號", "番号", "識別碼", "dvd id"],
    "release_date": ["発売日", "配信開始日", "リリース", "release date", "發行日期"],
    "actress": ["出演者", "女優", "演员", "actress"],
    "genres": ["ジャンル", "类型", "genre"],
    "series": ["シリーズ", "系列", "series"],
    "maker": ["メーカー", "製作", "制作", "maker", "studio", "label"],
    "director": ["監督", "导演", "director"],
    "tags": ["タグ", "標籤", "tag"],
}


def _label_key(label: str) -> Optional[str]:
    low = label.lower()
    for key, words in LABELS.items():
        if any(w in low for w in words):
            return key
    return None


M3U8_PATTERNS = [
    r'<source[^>]+src="([^"]+\.m3u8[^"]*)"',
    r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)',
    r'source\s*[:=]\s*["\']([^"\']+\.m3u8[^"\']*)',
    r'["\'](https?://[^"\']+?\.m3u8[^"\']*)["\']',
    r'(https?://[^"\'\s<>]+\.m3u8[^"\'\s<>]*)',
]


def parse_movie(tree, url: str, raw: str) -> dict[str, Any]:
    info: dict[str, Any] = {"link": url}
    for meta in tree.xpath('//meta[@property="og:title" or @property="og:url"'
                           ' or @property="og:description" or @property="og:image"]'):
        prop, content = meta.get("property", ""), meta.get("content", "")
        if prop == "og:title":
            info["title"] = content
        elif prop == "og:url":
            info["link"] = content
        elif prop == "og:description":
            info["description"] = content
        elif prop == "og:image":
            info["cover"] = content

    for row in tree.xpath('//div[contains(@class,"text-secondary")]'):
        lines = [l.strip() for l in row.text_content().split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        key = _label_key(lines[0])
        if key and not info.get(key):
            info[key] = lines[1]

    vids = tree.xpath("//video[@data-src or @src]")
    if vids:
        info["preview"] = vids[0].get("data-src") or vids[0].get("src")

    for pat in M3U8_PATTERNS:
        m = re.search(pat, raw)
        if m:
            info["m3u8"] = m.group(1).replace("&amp;", "&")
            break

    info.setdefault("number", _code_from_href(url))
    return info


# ───────────────── API ─────────────────
app = FastAPI(title="MissAV Client API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _list(tree, page: int) -> dict[str, Any]:
    items = parse_cards(tree)
    return {"page": page, "count": len(items), "last_page": parse_last_page(tree), "items": items}


@app.get("/api/config")
def api_config():
    c = SESSION.cookies.get_dict()
    return {"host": STATE["host"], "mirrors": MIRRORS, "sources": sorted(SOURCES),
            "logged_in": bool(c.get("user_uuid") or c.get("missav_session"))}


@app.post("/api/login")
def api_login(body: dict):
    email, password = body.get("email", ""), body.get("password", "")
    if not email or not password:
        raise HTTPException(400, "email と password が必要です")
    url = f"{STATE['host']}/api/login"
    h = {"User-Agent": UA, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
         "Referer": f"{STATE['host']}/"}
    for as_json in (True, False):
        try:
            r = (SESSION.post(url, json={"email": email, "password": password}, headers=h, timeout=25)
                 if as_json else
                 SESSION.post(url, data={"email": email, "password": password}, headers=h, timeout=25))
            if r.status_code < 400 and SESSION.cookies.get_dict().get("user_uuid"):
                _save_cookies()
                return {"ok": True}
        except Exception:
            continue
    raise HTTPException(401, "ログイン失敗")


@app.post("/api/cookies")
def api_cookies(body: dict):
    for part in (body.get("cookie") or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            SESSION.cookies.set(k.strip(), v.strip())
    _save_cookies()
    return {"ok": True, "count": len(SESSION.cookies.get_dict())}


@app.post("/api/logout")
def api_logout():
    SESSION.cookies.clear()
    _save_cookies()
    return {"ok": True}


@app.get("/api/list")
def api_list(src: str = "new", page: int = 1, lang: str = "ja",
             filters: Optional[str] = None, sort: Optional[str] = None):
    if src not in SOURCES:
        raise HTTPException(404, f"unknown src: {src}")
    _, r = fetch_first(SOURCES[src], lang, {"page": page, "filters": filters, "sort": sort})
    return _list(LH.fromstring(r.text), page)


@app.get("/api/search/{keyword}")
def api_search(keyword: str, page: int = 1, lang: str = "ja",
               filters: Optional[str] = None, sort: Optional[str] = None):
    r = get(page_url(f"/search/{quote(keyword)}", lang), lang=lang,
            params={"page": page, "filters": filters, "sort": sort})
    tree = LH.fromstring(r.text)
    d = _list(tree, page)
    d["actresses"] = parse_actresses(tree) if _actress_box(tree) is not None else []
    return d


@app.get("/api/movie/{number}")
def api_movie(number: str, lang: str = "ja"):
    r = get(page_url(f"/{quote(number)}", lang), lang=lang)
    return parse_movie(LH.fromstring(r.text), r.url, r.text)


@app.get("/api/actresses")
def api_actresses(page: int = 1, lang: str = "ja", cup: Optional[str] = None,
                  age: Optional[str] = None, height: Optional[str] = None,
                  debut: Optional[str] = None, sort: Optional[str] = None):
    r = get(page_url("/actresses", lang), lang=lang,
            params={"page": page, "cup": cup, "age": age, "height": height,
                    "debut": debut, "sort": sort})
    t = LH.fromstring(r.text)
    return {"page": page, "last_page": parse_last_page(t), "items": parse_actresses(t)}


@app.get("/api/actresses/ranking")
def api_ranking(lang: str = "ja"):
    r = get(page_url("/actresses/ranking", lang), lang=lang)
    return {"items": parse_actresses(LH.fromstring(r.text))}


@app.get("/api/actresses/{name}")
def api_actress_movies(name: str, page: int = 1, lang: str = "ja",
                       filters: Optional[str] = None, sort: Optional[str] = None):
    r = get(page_url(f"/actresses/{quote(name)}", lang), lang=lang,
            params={"page": page, "filters": filters, "sort": sort})
    return _list(LH.fromstring(r.text), page)


@app.get("/api/genres")
def api_genres(page: int = 1, lang: str = "ja"):
    r = get(page_url("/genres", lang), lang=lang, params={"page": page})
    t = LH.fromstring(r.text)
    return {"page": page, "last_page": parse_last_page(t), "items": parse_taxonomy(t)}


@app.get("/api/genres/{name}")
def api_genre_movies(name: str, page: int = 1, lang: str = "ja",
                     filters: Optional[str] = None, sort: Optional[str] = None):
    r = get(page_url(f"/genres/{quote(name)}", lang), lang=lang,
            params={"page": page, "filters": filters, "sort": sort})
    return _list(LH.fromstring(r.text), page)


@app.get("/api/makers")
def api_makers(page: int = 1, lang: str = "ja"):
    r = get(page_url("/makers", lang), lang=lang, params={"page": page})
    t = LH.fromstring(r.text)
    return {"page": page, "last_page": parse_last_page(t), "items": parse_taxonomy(t)}


@app.get("/api/makers/{name}")
def api_maker_movies(name: str, page: int = 1, lang: str = "ja",
                     filters: Optional[str] = None, sort: Optional[str] = None):
    r = get(page_url(f"/makers/{quote(name)}", lang), lang=lang,
            params={"page": page, "filters": filters, "sort": sort})
    return _list(LH.fromstring(r.text), page)


# ───────────────── HLS プロキシ ─────────────────
def _purl(u: str) -> str:
    return "/api/proxy?url=" + quote(u, safe="")


def _rewrite(text: str, base: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            out.append(re.sub(r'URI="([^"]+)"',
                              lambda m: 'URI="%s"' % _purl(urljoin(base, m.group(1))), line))
        else:
            out.append(_purl(urljoin(base, s)))
    return "\n".join(out)


@app.get("/api/proxy")
def api_proxy(url: str = Query(...), ref: Optional[str] = None):
    target = url if url.startswith("http") else urljoin(STATE["host"] + "/", url)
    try:
        r = SESSION.get(target, timeout=40, allow_redirects=True,
                        headers={"User-Agent": UA, "Referer": ref or f"{STATE['host']}/",
                                 "Origin": STATE["host"], "Accept": "*/*"})
    except Exception as exc:
        raise HTTPException(502, str(exc))
    ctype = (r.headers.get("content-type") or "").lower()
    body = r.content
    if target.split("?")[0].endswith(".m3u8") or "mpegurl" in ctype or body[:7] == b"#EXTM3U":
        return Response(_rewrite(body.decode("utf-8", "ignore"), target),
                        media_type="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
    return Response(body, media_type=ctype or "application/octet-stream",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"})


# ───────────────── 静的配信 ─────────────────
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
