"""Daily research collector for the Deeptech Radar (standard library only).

Reads config/research.json and writes:
  data/research.json         research items from the last 14 days scoring >= min_score (max max_items)
  data/research_status.json  per-source result of this run
  data/research_seen.json    ids already collected
Sources: arXiv (Atom API), OpenAlex (JSON API, Nordic institutions), PubMed (E-utilities JSON),
bioRxiv/medRxiv (JSON API) and RSS/Atom feeds such as EurekAlert and university newsrooms.
"""
import datetime as dt
import json
import os
import re
import urllib.parse

import collect as C

NOW = C.NOW
TODAY = C.TODAY


def get_json(url):
    return json.loads(C.fetch(url).decode("utf-8", "replace"))


def openalex(src, since):
    flt = [f"from_publication_date:{since}", "type:article|preprint", "title_and_abstract.search:" + src["search"]]
    if src.get("countries"):
        flt.append("authorships.institutions.country_code:" + "|".join(src["countries"]))
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode({
        "filter": ",".join(flt), "sort": "publication_date:desc", "per-page": src.get("max", 50)})
    out = []
    for w in get_json(url).get("results", []):
        inv = w.get("abstract_inverted_index") or {}
        words = sorted(((p, k) for k, ps in inv.items() for p in ps))
        abstract = " ".join(k for _, k in words)[:600]
        inst = sorted({i.get("display_name", "") for a in w.get("authorships", []) for i in a.get("institutions", []) if i.get("display_name")})
        url_ = (w.get("primary_location") or {}).get("landing_page_url") or w.get("doi") or w.get("id")
        out.append({"title": w.get("display_name") or "", "summary": abstract, "url": url_ or "",
                    "date": w.get("publication_date"), "source": ((w.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
                    "institutions": inst[:6]})
    return out


def pubmed(src, since):
    term = f"({src['term']}) AND (\"{since}\"[dp] : \"3000\"[dp])"
    ids = get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "term": term, "retmode": "json", "retmax": src.get("max", 40), "sort": "pub_date"})).get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    res = get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "id": ",".join(ids), "retmode": "json"})).get("result", {})
    out = []
    for i in ids:
        r = res.get(i, {})
        out.append({"title": r.get("title", ""), "summary": "", "url": f"https://pubmed.ncbi.nlm.nih.gov/{i}/",
                    "date": r.get("sortpubdate", "")[:10].replace("/", "-"), "source": r.get("fulljournalname", "")})
    return out


def rxiv(src, since):
    server = src["server"]
    out, cursor = [], 0
    while cursor < src.get("max_scan", 600):
        d = get_json(f"https://api.biorxiv.org/details/{server}/{since}/{TODAY}/{cursor}")
        coll = d.get("collection", [])
        if not coll:
            break
        for r in coll:
            if src.get("categories") and r.get("category", "").lower() not in src["categories"]:
                continue
            out.append({"title": r.get("title", ""), "summary": (r.get("abstract") or "")[:600],
                        "url": f"https://doi.org/{r.get('doi')}", "date": r.get("date", ""), "source": server})
        cursor += len(coll)
        if len(coll) < 100:
            break
    return out


def feed(src, since):
    items = C.parse_feed(C.fetch(src["url"]))
    for it in items:
        it["date"] = it["date"].date().isoformat() if it["date"] else None
    return items


HANDLERS = {"openalex": openalex, "pubmed": pubmed, "rxiv": rxiv, "rss": feed}


def score(it, kw, companies):
    s, hits = C.score(it, kw, companies)
    hay = (it["title"] + " " + it["summary"]).lower()
    novelty = [t for t in kw.get("research_novelty", []) if t in hay]
    if novelty:
        s += 1
        hits["novelty"] = novelty[:4]
    if any(t in hay for t in kw.get("research_penalty", [])):
        s -= 3
        hits["penalty"] = True
    if it.get("institutions") and any(c in " ".join(it["institutions"]) for c in kw.get("nordic_institutions", [])):
        s += 1
    return s, hits


def main():
    with open(os.path.join(C.CFG, "research.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    with open(os.path.join(C.CFG, "keywords.json"), encoding="utf-8") as f:
        kw = json.load(f)
    with open(os.path.join(C.CFG, "companies.json"), encoding="utf-8") as f:
        companies = json.load(f)
    kw["research_novelty"] = cfg.get("novelty_terms", [])
    kw["nordic_institutions"] = cfg.get("nordic_institutions", [])
    kw["research_penalty"] = cfg.get("penalty_terms", [])
    window = cfg.get("window_days", 14)
    since = (NOW - dt.timedelta(days=window)).date().isoformat()
    seen = C.load("research_seen.json", {})
    seen = {k: v for k, v in seen.items() if v >= (NOW - dt.timedelta(days=90)).date().isoformat()}
    prev = C.load("research.json", {}).get("items", [])
    status, new = [], []
    for src in cfg["sources"]:
        try:
            items = HANDLERS[src.get("type", "rss")](src, since)
            err = None
        except Exception as e:
            items, err = [], str(e)[:200]
        n_new = 0
        for it in items:
            if not it.get("title") or not it.get("url"):
                continue
            it["title"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", it["title"])).strip()
            it["id"] = C.item_id(it)
            if it["id"] in seen:
                continue
            it.update({"feed": src["name"], "kind": "research", "domain_hint": src.get("domain", ""),
                       "date": (it.get("date") or TODAY)[:10], "collected": TODAY})
            it["score"], it["hits"] = score(it, kw, companies)
            seen[it["id"]] = TODAY
            new.append(it)
            n_new += 1
        status.append({"source": src["name"], "type": src.get("type", "rss"), "ok": err is None, "items": len(items), "new": n_new, "error": err})
    keep = [x for x in prev if x.get("collected", "") >= since] + [x for x in new if x["score"] >= cfg.get("min_score", 3)]
    keep.sort(key=lambda x: x["date"], reverse=True)
    keep.sort(key=lambda x: -x["score"])
    keep = keep[: cfg.get("max_items", 300)]
    C.save("research.json", {"generated": NOW.isoformat(timespec="minutes"), "count": len(keep), "items": keep})
    C.save("research_status.json", {"run": NOW.isoformat(timespec="minutes"), "sources": status,
                                    "ok": sum(1 for s in status if s["ok"]), "failed": sum(1 for s in status if not s["ok"]),
                                    "new_items": len(new), "kept": len(keep)})
    C.save("research_seen.json", seen)
    print(f"research: {len(new)} new, {len(keep)} kept, {sum(1 for s in status if not s['ok'])} sources failed")


if __name__ == "__main__":
    main()
