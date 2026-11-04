#!/usr/bin/env python3
"""구글 뉴스 RSS에서 분야별 헤드라인을 모아 site/news.json 으로 저장합니다.

표준 라이브러리만 사용합니다. GitHub Actions에서 30분마다 실행됩니다.
"""
import email.utils
import hashlib
import html
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

KR = "hl=ko&gl=KR&ceid=KR:ko"
US = "hl=en-US&gl=US&ceid=US:en"
BASE = "https://news.google.com/rss"


def topic(name):
    return f"{BASE}/headlines/section/topic/{name}?{KR}"


def search(q, ed=KR):
    return f"{BASE}/search?q={urllib.parse.quote(q)}&{ed}"


TOP = f"{BASE}?{KR}"

# 분류: (id, 이름, 그룹)
CATEGORIES = [
    ("kr_econ", "국내 경제", "국내"),
    ("realestate", "부동산", "국내"),
    ("stock", "주식·증시", "국내"),
    ("world_econ", "해외 경제·증시", "해외"),
    ("world", "해외 주요뉴스", "해외"),
    ("startup", "스타트업·투자유치", "스타트업"),
    ("ent", "연예", "이슈"),
    ("society", "사회·이슈", "이슈"),
    ("football", "유럽 축구", "축구"),
]

# (분류, 세부태그, URL, 최대 기사 나이(시간))
FEEDS = [
    ("realestate", None, search("부동산 OR 아파트값 OR 전세 OR 분양 OR 재건축 when:1d"), 36),
    ("realestate", None, search("집값 OR 주택시장 OR 청약 when:1d"), 36),
    ("stock", None, search("코스피 OR 코스닥 OR 증시 when:1d"), 36),
    ("stock", None, search("주가 OR 외국인 순매수 OR 공모주 when:1d"), 36),
    ("startup", "국내", search("스타트업 투자 유치 OR 투자유치 OR 시리즈A OR 시리즈B OR 프리A when:2d"), 54),
    ("startup", "국내", search("스타트업 OR 벤처캐피털 OR 유니콘 기업 OR 엑시트 when:1d"), 36),
    ("startup", "해외", search("startup raises funding when:1d", US), 36),
    ("startup", "해외", search("\"Series A\" OR \"Series B\" OR \"Series C\" funding round when:1d", US), 36),
    ("startup", "해외", search("venture capital OR unicorn startup valuation when:1d", US), 36),
    ("world_econ", None, search("뉴욕증시 OR 나스닥 OR 연준 OR 파월 when:1d"), 36),
    ("world_econ", None, search("국제유가 OR 달러 환율 OR 미국 경제 OR 중국 경제 when:1d"), 36),
    ("football", "EPL", search("프리미어리그 when:2d"), 54),
    ("football", "라리가", search("라리가 when:2d"), 54),
    ("football", "분데스리가", search("분데스리가 when:2d"), 54),
    ("football", "리그앙", search("리그앙 OR 파리 생제르맹 when:2d"), 54),
    ("kr_econ", None, topic("BUSINESS"), 36),
    ("kr_econ", None, search("경제 OR 금리 OR 물가 OR 수출 when:1d"), 36),
    ("world", None, topic("WORLD"), 36),
    ("ent", None, topic("ENTERTAINMENT"), 36),
    ("society", None, TOP, 36),
    ("society", None, topic("NATION"), 36),
]

# 앞에 있을수록 우선 분류 (같은 기사가 여러 피드에 나오면 이 순서로 분류)
PRIORITY = ["startup", "realestate", "stock", "world_econ", "football", "kr_econ", "world", "ent", "society"]

UA = "Mozilla/5.0 (compatible; news-headlines-bot/1.0)"


def fetch(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                print(f"실패: {url} ({e})", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))


def translate_en_ko(titles):
    """영문 제목을 한국어로 번역합니다 (구글 번역 공개 엔드포인트, 실패하면 원문 유지)."""
    out = {}
    batch, size = [], 0
    batches = []
    for t in titles:
        if batch and size + len(t) > 1500:
            batches.append(batch); batch, size = [], 0
        batch.append(t); size += len(t) + 1
    if batch:
        batches.append(batch)
    for b in batches:
        q = "\n".join(b)
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ko&dt=t&q="
               + urllib.parse.quote(q))
        body = fetch(url)
        if not body:
            continue
        try:
            res = json.loads(body)
            text = "".join(seg[0] for seg in res[0] if seg and seg[0])
            lines = [l.strip() for l in text.split("\n")]
            if len(lines) == len(b):
                out.update({src: dst for src, dst in zip(b, lines) if dst})
        except Exception as e:  # noqa: BLE001
            print(f"번역 실패: {e}", file=sys.stderr)
        time.sleep(0.5)
    return out


def norm(title):
    return re.sub(r"[^0-9a-z가-힣]", "", title.lower())


def split_title(raw, source):
    title = html.unescape(raw).strip()
    if source and title.endswith(" - " + source):
        title = title[: -len(source) - 3].strip()
    else:
        m = re.match(r"^(.*)\s+-\s+([^-]{1,30})$", title)
        if m:
            title = m.group(1).strip()
            source = source or m.group(2).strip()
    return title, source


def parse(feed, xml_bytes):
    cat, tag, _url, max_age = feed
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    items = root.findall("./channel/item")
    now = time.time()
    for pos, it in enumerate(items):
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        title, source = split_title(it.findtext("title", ""), source)
        link = it.findtext("link", "").strip()
        if not title or not link:
            continue
        try:
            ts = email.utils.parsedate_to_datetime(it.findtext("pubDate", "")).timestamp()
        except Exception:  # noqa: BLE001
            ts = now
        if now - ts > max_age * 3600:
            continue
        desc = it.findtext("description", "") or ""
        related = max(1, desc.count("<li>"))
        out.append({
            "cat": cat,
            "tag": tag,
            "title": title,
            "source": source,
            "url": link,
            "ts": int(ts),
            "related": related,
            "rank": round(1 - pos / max(1, len(items)), 3),
        })
    return out


def main():
    with ThreadPoolExecutor(max_workers=8) as ex:
        bodies = list(ex.map(lambda f: fetch(f[2]), FEEDS))

    ok = sum(1 for b in bodies if b)
    merged = {}
    for feed, body in zip(FEEDS, bodies):
        if not body:
            continue
        for item in parse(feed, body):
            key = norm(item["title"])[:40]
            if not key:
                continue
            cur = merged.get(key)
            if cur is None:
                item["seen"] = 1
                merged[key] = item
                continue
            # 여러 피드에 동시에 나온 기사는 화제성이 높다고 봅니다.
            cur["seen"] += 1
            cur["related"] = max(cur["related"], item["related"])
            cur["rank"] = max(cur["rank"], item["rank"])
            if PRIORITY.index(item["cat"]) < PRIORITY.index(cur["cat"]):
                cur["cat"], cur["tag"] = item["cat"], item["tag"]

    en = [it["title"] for it in merged.values() if it["cat"] == "startup" and it["tag"] == "해외"]
    tr = translate_en_ko(en) if en else {}
    print(f"해외 스타트업 기사 번역 {len(tr)}/{len(en)}건")
    for it in merged.values():
        if it["title"] in tr:
            it["orig"] = it["title"]
            it["title"] = tr[it["title"]]

    items = []
    for it in merged.values():
        it["id"] = hashlib.sha1(norm(it["title"]).encode()).hexdigest()[:10]
        # 화제성 점수: 관련 기사 수 + 피드 내 순위 + 중복 노출
        it["hot"] = round(math.log2(1 + it["related"]) * 1.5 + it["rank"] * 2 + (it.pop("seen") - 1) * 1.2, 2)
        items.append(it)
    items.sort(key=lambda x: (-x["hot"], -x["ts"]))

    if ok == 0 or not items:
        print("뉴스를 하나도 받지 못했습니다. 기존 파일을 유지합니다.", file=sys.stderr)
        sys.exit(1)

    data = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": [{"id": c, "name": n, "group": g} for c, n, g in CATEGORIES],
        "items": items,
    }
    out = Path(__file__).resolve().parent.parent / "site" / "news.json"
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    counts = {}
    for it in items:
        counts[it["cat"]] = counts.get(it["cat"], 0) + 1
    print(f"피드 {ok}/{len(FEEDS)}개 성공, 기사 {len(items)}건: {counts}")


if __name__ == "__main__":
    main()
