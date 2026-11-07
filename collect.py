"""Daily news collector for the Deeptech Radar (standard library only).

Reads config/sources.json (RSS/Atom feeds and Google News queries), scores each item
with config/keywords.json and config/companies.json, drops duplicates and writes:
  data/inbox/YYYY-MM-DD.json  all new items from this run
  data/latest.json            all items from the last 7 days, highest score first
  data/top.json               items scoring >= min_score in the last 7 days (max max_top)
  data/feeds_status.json      per-feed result of this run (ok / error, item count)
  data/seen.json              ids already collected (kept for keep_days)
"""
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(ROOT, "config")
DATA = os.path.join(ROOT, "data")
UA = "Mozilla/5.0 (compatible; radar-collector/1.0)"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()


def load(name, default):
    p = os.path.join(DATA, name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj):
    p = os.path.join(DATA, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def text(el):
    if el is None:
        return ""
    t = "".join(el.itertext()) if len(el) else (el.text or "")
    t = re.sub(r"<[^>]+>", " ", html.unescape(t))
    return re.sub(r"\s+", " ", t).strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw):
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {local(c.tag): c for c in el}
        link = ""
        if "link" in f:
            link = f["link"].get("href") or text(f["link"])
        for c in el:
            if local(c.tag) == "link" and c.get("rel") in (None, "alternate") and c.get("href"):
                link = c.get("href")
                break
        date = None
        for k in ("pubDate", "published", "updated", "date"):
            if k in f:
                date = parse_date(text(f[k]))
                if date:
                    break
        src = text(f.get("source")) if "source" in f else ""
        out.append({
            "title": text(f.get("title")),
            "summary": text(f.get("description") or f.get("summary") or f.get("content"))[:600],
            "url": link.strip(),
            "date": date,
            "source": src,
        })
    return out


def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def item_id(it):
    key = norm_title(it["title"])[:120] or it["url"]
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def score(it, kw, companies):
    s = 0
    hay = (" " + it["title"] + " " + it["summary"] + " ").lower()
    hits = {}
    for group in ("event", "tech", "industry", "geo"):
        g = kw.get(group, {})
        found = [t for t in g.get("terms", []) if t in hay]
        if found:
            s += g.get("weight", 1)
            hits[group] = found[:5]
    cos = [c for c in companies if re.search(r"(?<![A-Za-z0-9])" + re.escape(c) + r"(?![A-Za-z0-9])", it["title"] + " " + it["summary"])]
    if cos:
        s += kw.get("company_weight", 3)
        hits["companies"] = cos[:5]
    if it.get("kind") == "research":
        s += 1
    return s, hits


def main():
    with open(os.path.join(CFG, "sources.json"), encoding="utf-8") as f:
        src = json.load(f)
    with open(os.path.join(CFG, "keywords.json"), encoding="utf-8") as f:
        kw = json.load(f)
    with open(os.path.join(CFG, "companies.json"), encoding="utf-8") as f:
        companies = json.load(f)
    feeds = list(src.get("feeds", []))
    for q in src.get("google_news_queries", []):
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q + " when:3d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        feeds.append({"name": "Google News: " + q, "url": url, "domain": "", "kind": "news"})

    def run(feed):
        try:
            items = parse_feed(fetch(feed["url"]))
            return feed, items, None
        except Exception as e:
            return feed, [], str(e)[:200]

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(run, feeds))

    keep_days = kw.get("keep_days", 60)
    seen = load("seen.json", {})
    cutoff = (NOW - dt.timedelta(days=keep_days)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    status, new = [], []
    for feed, items, err in results:
        n_new = 0
        for it in items:
            if not it["title"] or not it["url"]:
                continue
            if it["date"] and it["date"] < NOW - dt.timedelta(days=14):
                continue
            it["id"] = item_id(it)
            if it["id"] in seen:
                continue
            it["feed"] = feed["name"]
            it["kind"] = feed.get("kind", "news")
            it["domain_hint"] = feed.get("domain", "")
            it["date"] = (it["date"] or NOW).date().isoformat()
            it["collected"] = TODAY
            it["score"], it["hits"] = score(it, kw, companies)
            seen[it["id"]] = TODAY
            new.append(it)
            n_new += 1
        status.append({"feed": feed["name"], "url": feed["url"], "ok": err is None, "items": len(items), "new": n_new, "error": err})

    save(os.path.join("inbox", TODAY + ".json"), sorted(new, key=lambda x: -x["score"]))
    week = []
    for i in range(7):
        day = (NOW - dt.timedelta(days=i)).date().isoformat()
        week += load(os.path.join("inbox", day + ".json"), [])
    week.sort(key=lambda x: x["date"], reverse=True)
    week.sort(key=lambda x: -x["score"])
    save("latest.json", week)
    top = [x for x in week if x["score"] >= kw.get("min_score", 5)][: kw.get("max_top", 200)]
    save("top.json", {"generated": NOW.isoformat(timespec="minutes"), "count": len(top), "items": top})
    save("feeds_status.json", {"run": NOW.isoformat(timespec="minutes"), "feeds": status,
                               "ok": sum(1 for s in status if s["ok"]), "failed": sum(1 for s in status if not s["ok"]),
                               "new_items": len(new), "top_items": len(top)})
    save("seen.json", seen)
    old = (NOW - dt.timedelta(days=keep_days)).date().isoformat()
    inbox = os.path.join(DATA, "inbox")
    for f in os.listdir(inbox):
        if f.endswith(".json") and f[:10] < old:
            os.remove(os.path.join(inbox, f))
    print(f"{len(new)} new items, {len(top)} in top, {sum(1 for s in status if not s['ok'])} feeds failed")


if __name__ == "__main__":
    main()
