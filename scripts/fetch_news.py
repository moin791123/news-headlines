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
    ("startup", "해외", search("해외 스타트업 투자 유치 OR 실리콘밸리 스타트업 OR 美 스타트업 OR 中 스타트업 when:2d"), 54),
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


CACHE = Path(__file__).resolve().parent.parent / ".cache" / "translations.json"


def _gtx_batch(b):
    q = "\n".join(b)
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ko&dt=t&q="
           + urllib.parse.quote(q))
    body = fetch(url, tries=1)
    if not body:
        return {}
    res = json.loads(body)
    text = "".join(seg[0] for seg in res[0] if seg and seg[0])
    lines = [l.strip() for l in text.split("\n")]
    return {s: d for s, d in zip(b, lines) if d} if len(lines) == len(b) else {}


def _clients5(t):
    url = ("https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=en&tl=ko&q="
           + urllib.parse.quote(t))
    body = fetch(url, tries=1)
    if not body:
        return None
    res = json.loads(body)
    while isinstance(res, list) and res:
        res = res[0]
    return res if isinstance(res, str) and res.strip() else None


def _mymemory(t):
    url = "https://api.mymemory.translated.net/get?langpair=en|ko&q=" + urllib.parse.quote(t[:450])
    body = fetch(url, tries=1)
    if not body:
        return None
    res = json.loads(body)
    txt = (res.get("responseData") or {}).get("translatedText") or ""
    if res.get("responseStatus") != 200 or not txt or "MYMEMORY WARNING" in txt or not re.search(r"[가-힣]", txt):
        return None
    return html.unescape(txt).strip()


def translate_en_ko(titles):
    """영문 제목을 한국어로 번역합니다.

    무료 번역 서버들이 GitHub 쪽 요청을 자주 막기 때문에, 여러 곳을 차례로 시도하고
    한 번 번역한 제목은 .cache/translations.json 에 저장해 다음 실행 때 다시 씁니다.
    """
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cache = {}
    todo = [t for t in titles if t not in cache]

    # 1) 구글 번역(묶음)
    batches, batch, size = [], [], 0
    for t in todo:
        if batch and size + len(t) > 1500:
            batches.append(batch); batch, size = [], 0
        batch.append(t); size += len(t) + 1
    if batch:
        batches.append(batch)
    for b in batches:
        try:
            got = _gtx_batch(b)
        except Exception:  # noqa: BLE001
            got = {}
        if not got:
            break  # 막혔으면 다음 방법으로
        cache.update(got)
        time.sleep(1.5)

    # 2) 다른 구글 엔드포인트, 3) MyMemory — 하나씩, 막히면 바로 다음 방법으로
    for fn in (_clients5, _mymemory):
        for t in [t for t in todo if t not in cache]:
            try:
                d = fn(t)
            except Exception:  # noqa: BLE001
                d = None
            if not d:
                break
            cache[t] = d
            time.sleep(0.8)

    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        keep = dict(list(cache.items())[-3000:])
        CACHE.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"번역 저장 실패: {e}", file=sys.stderr)
    return {t: cache[t] for t in titles if t in cache}


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


# ---------------- 조회수 랭킹 (네이버 '많이 본 뉴스') ----------------
RANK_URL = "https://news.naver.com/main/ranking/popularDay.naver"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
PARTICLES = re.compile(r"(에서는|에서|으로는|으로|에게|까지|부터|보다|처럼|이라|라는|이란|에는|과의|와의|은|는|이|가|을|를|의|에|로|와|과|도|만)$")
STOP = set("속보 단독 종합 오늘 내일 어제 기자 뉴스 이번 대한 관련 위해 이후 올해 지난 사진 영상 포토 가운데 통해 대해 따라 있다 없다 했다 한다 하는 있는 위한 만에 이상 이하 최대 최고 최저 사상 결국".split())


def tokens(title):
    clean = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title)
    out = []
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", clean):
        w = w.lower()
        if re.search(r"[가-힣]", w) and len(w) > 2:
            w = PARTICLES.sub("", w)
        if len(w) < 2 or w in STOP or w.isdigit() or w in out:
            continue
        out.append(w)
    return set(out[:14])


def similar(a, b):
    n = len(a & b)
    r = n / max(1, min(len(a), len(b)))
    return (n >= 3 and r >= 0.4) or (n >= 2 and r >= 0.6)


def parse_ago(text, now):
    m = re.match(r"(\d+)\s*(분|시간|일)", text or "")
    if not m:
        return now
    n, u = int(m.group(1)), m.group(2)
    return now - n * {"분": 60, "시간": 3600, "일": 86400}[u]


def fetch_ranking():
    """네이버 언론사별 '많이 본 뉴스'(조회수 기준 1~5위)를 가져옵니다."""
    try:
        req = urllib.request.Request(RANK_URL, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except Exception as e:  # noqa: BLE001
        print(f"랭킹 실패: {e}", file=sys.stderr)
        return []
    s = raw.decode("cp949", errors="replace")
    if "list_title" not in s:
        s = raw.decode("utf-8", errors="replace")
    now = time.time()
    out = []
    for block in s.split('class="rankingnews_name">')[1:]:
        press = html.unescape(block.split("<", 1)[0]).strip()
        for m in re.finditer(r'<em class="list_ranking_num">(\d+).*?<a href="([^"]+)" class="list_title[^"]*"[^>]*>(.*?)</a>\s*<span class="list_time[^"]*">([^<]*)<',
                             block, re.S):
            title = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
            if title:
                out.append({"rank": int(m.group(1)), "url": m.group(2).split("?")[0], "title": title,
                            "source": press, "ts": int(parse_ago(m.group(4), now))})
    return out


KW = [  # 랭킹 기사 분류용 단어 (앞쪽 규칙 우선)
    ("football", "프리미어리그 epl 라리가 분데스리가 리그앙 psg 토트넘 맨유 맨시티 리버풀 아스널 첼시 레알 바르셀로나 바르사 바이에른 뮌헨 손흥민 이강인 김민재 황희찬 챔피언스리그"),
    ("startup", "스타트업 투자유치 시리즈a 시리즈b 벤처 유니콘 엑시트 액셀러레이터"),
    ("realestate", "부동산 아파트 전세 월세 분양 청약 집값 재건축 재개발 주택 매매 공시가 토지거래허가"),
    ("stock", "코스피 코스닥 증시 주가 주식 상장 공모주 증권 etf 개미 순매수 순매도 사이드카 시가총액 서학개미"),
    ("world_econ", "뉴욕증시 나스닥 다우 연준 파월 국제유가 달러 엔화 위안"),
    ("kr_econ", "경제 금리 환율 물가 수출 수입 실적 영업이익 매출 한은 한국은행 기재부 세금 연금 반도체 관세 고용 취업 일자리 소비 가계부채 대출 은행 카드 보험"),
    ("ent", "배우 가수 아이돌 드라마 예능 연예 열애 결혼 이혼 컴백 방송인 유튜버 걸그룹 보이그룹 영화 콘서트 개그맨 아나운서"),
    ("world", "美 中 日 北 트럼프 미국 중국 일본 러시아 우크라이나 이스라엘 유럽 영국 프랑스 독일 백악관 푸틴 시진핑"),
]
KW = [(c, w.split()) for c, w in KW]


def classify(title):
    t = title.lower()
    for cat, words in KW:
        if any(w in t for w in words):
            return cat
    return "society"


def apply_ranking(merged, ranking):
    """랭킹 기사와 비슷한 기사에 조회수 점수(views)를 주고, 랭킹에만 있는 기사는 새로 넣습니다."""
    items = list(merged.values())
    for it in items:
        it["_t"] = tokens(it["title"])
        it["views"] = 0
    added = 0
    for r in ranking:
        rt = tokens(r["title"])
        pts = 6 - min(5, r["rank"])  # 1위 5점 ~ 5위 1점
        hit = [it for it in items if similar(rt, it["_t"])]
        for it in hit:
            it["views"] += pts
        if not hit:
            key = norm(r["title"])[:40]
            if not key:
                continue
            if key in merged:
                merged[key]["views"] += pts
                continue
            new = {"cat": classify(r["title"]), "tag": None, "title": r["title"], "source": r["source"],
                   "url": r["url"], "ts": r["ts"], "related": 1, "rank": 0.5, "seen": 1,
                   "views": pts, "_t": rt}
            merged[key] = new
            items.append(new)
            added += 1
    for it in merged.values():
        it.pop("_t", None)
    print(f"조회수 랭킹 {len(ranking)}건 반영, 랭킹에만 있던 기사 {added}건 추가, "
          f"조회수 점수 받은 기사 {sum(1 for i in merged.values() if i['views'])}건")


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

    # 해외 스타트업 기사는 최신 50건만 번역해서 싣고, 번역이 안 된 기사는 뺍니다.
    foreign = sorted((k for k, it in merged.items() if it["cat"] == "startup" and it["tag"] == "해외"
                      and not re.search(r"[가-힣]", it["title"])),
                     key=lambda k: -merged[k]["ts"])
    keep, drop = foreign[:50], foreign[50:]
    tr = translate_en_ko([merged[k]["title"] for k in keep]) if keep else {}
    print(f"해외 스타트업 기사 번역 {len(tr)}/{len(keep)}건")
    for k in keep:
        it = merged[k]
        if it["title"] in tr:
            it["orig"] = it["title"]
            it["title"] = tr[it["title"]]
        else:
            drop.append(k)
    for k in drop:
        merged.pop(k, None)

    apply_ranking(merged, fetch_ranking())

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
