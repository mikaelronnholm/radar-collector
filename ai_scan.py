"""AI scan for the Deeptech Radar (standard library only).

Asks Gemini (Google Search grounding) and ChatGPT (OpenAI web search) the same fixed questions
once a day and saves the answers as CANDIDATES - not signals. The scheduled Claude runs open
each source, drop duplicates and decide what becomes a signal.

Reads config/ai_scan.json and the secrets GEMINI_API_KEY / OPENAI_API_KEY (GitHub Actions secrets).
A provider without a key is skipped. Writes:
  data/ai_scan.json         candidates from the last keep_days days, highest score first
  data/ai_scan_status.json  per provider and group: ok / error, item count
"""
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request

import collect as C

NOW = C.NOW
TODAY = C.TODAY


class ApiError(Exception):
    def __init__(self, code, text):
        super().__init__(f"HTTP {code}: {text}")
        self.code = code


def call(url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace"))
            msg = (msg.get("error") or {}).get("message") or str(msg)
        except Exception:
            msg = e.reason
        raise ApiError(e.code, str(msg)[:180])


def post(url, body, headers):
    return call(url, body, headers)


MODEL = {}


def ver(name):
    return [int(x) for x in re.findall(r"\d+", name)[:3]] or [0]


def pick_gemini(key):
    r = call("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", None, {"x-goog-api-key": key})
    names = [m["name"].split("/")[-1] for m in r.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
    ok = [n for n in names if "flash" in n and not re.search(r"lite|image|tts|live|audio|thinking|exp|preview|8b", n)]
    ok = ok or [n for n in names if "flash" in n and not re.search(r"image|tts|live|audio", n)]
    return sorted(ok, key=ver)[-1] if ok else None


def pick_openai(key):
    r = call("https://api.openai.com/v1/models", None, {"Authorization": "Bearer " + key})
    names = [m["id"] for m in r.get("data", [])]
    ok = [n for n in names if re.match(r"gpt-\d", n) and n.endswith("-mini")] or [n for n in names if re.match(r"gpt-\d", n) and "mini" in n and not re.search(r"audio|realtime|transcribe|tts|search|image", n)]
    return sorted(ok, key=ver)[-1] if ok else None


def prompt(cfg, g):
    since = (NOW - dt.timedelta(hours=cfg.get("window_hours", 48))).strftime("%Y-%m-%d %H:%M UTC")
    return f"""You scan the news for a technology-intelligence radar. Search the web for concrete events published after {since} about:
{g['focus']}
{cfg.get('extra', '')}
Only real, dated events with a source page: funding rounds, acquisitions, contracts, deployments, product launches, pilots, research results, regulation, export controls, factory openings or closures, bankruptcies, delays and setbacks. No opinion pieces, no market-size reports, no general explainers.
Return at most {cfg.get('max_items_per_call', 10)} items, most important first. Reply with ONLY a JSON array, no other text:
[{{"title":"factual headline in English","summary":"one or two sentences: who did what, where, how much","url":"link to the source article or press release","source":"publisher","date":"YYYY-MM-DD","why":"one sentence on why it matters"}}]
If you find nothing new, reply []."""


def parse_items(text):
    text = (text or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return [x for x in arr if isinstance(x, dict)]


def gemini(cfg, g, key):
    p = cfg["providers"]["gemini"]
    model = MODEL.get("gemini") or p.get("model", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = post(url, {"contents": [{"parts": [{"text": prompt(cfg, g)}]}], "tools": [{"google_search": {}}]},
             {"x-goog-api-key": key})
    cand = (r.get("candidates") or [{}])[0]
    text = "".join(pt.get("text", "") for pt in ((cand.get("content") or {}).get("parts") or []))
    return parse_items(text)


def openai(cfg, g, key):
    p = cfg["providers"]["openai"]
    r = post("https://api.openai.com/v1/responses",
             {"model": MODEL.get("openai") or p.get("model", "gpt-5-mini"), "tools": [{"type": p.get("tool", "web_search")}], "input": prompt(cfg, g)},
             {"Authorization": "Bearer " + key})
    text = r.get("output_text") or ""
    if not text:
        for o in r.get("output", []):
            if o.get("type") == "message":
                for c in o.get("content", []):
                    if c.get("type") in ("output_text", "text"):
                        text += c.get("text", "")
    return parse_items(text)


CALLS = {"gemini": (gemini, "GEMINI_API_KEY"), "openai": (openai, "OPENAI_API_KEY")}


def main():
    with open(os.path.join(C.CFG, "ai_scan.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    with open(os.path.join(C.CFG, "keywords.json"), encoding="utf-8") as f:
        kw = json.load(f)
    with open(os.path.join(C.CFG, "companies.json"), encoding="utf-8") as f:
        companies = json.load(f)
    keep = cfg.get("keep_days", 7)
    cutoff = (NOW - dt.timedelta(days=keep)).date().isoformat()
    prev = [x for x in C.load("ai_scan.json", {}).get("items", []) if x.get("collected", "") >= cutoff]
    by_id = {x["id"]: x for x in prev}
    by_url = {x["url"]: x for x in prev if x.get("url")}
    status, n_new = [], 0
    for name, pcfg in cfg.get("providers", {}).items():
        fn, env = CALLS.get(name, (None, None))
        key = os.environ.get(env or "", "").strip()
        if not fn or not pcfg.get("enabled", True):
            continue
        if not key:
            status.append({"provider": name, "group": "", "ok": False, "items": 0, "new": 0, "error": f"no key ({env} secret missing)"})
            continue
        for g in cfg.get("groups", []):
            try:
                try:
                    items, err = fn(cfg, g, key), None
                except ApiError as e:
                    if e.code not in (400, 404) or name in MODEL:
                        raise
                    MODEL[name] = (pick_gemini if name == "gemini" else pick_openai)(key) or ""
                    if not MODEL[name]:
                        raise
                    items, err = fn(cfg, g, key), None
            except Exception as e:
                items, err = [], str(e)[:220]
            new = 0
            for it in items:
                title = re.sub(r"\s+", " ", str(it.get("title", ""))).strip()
                url = str(it.get("url", "")).strip()
                if not title or not url.startswith("http"):
                    continue
                cand = {"title": title, "summary": str(it.get("summary", ""))[:600], "url": url}
                cand["id"] = C.item_id(cand)
                old = by_id.get(cand["id"]) or by_url.get(url)
                if old:
                    if name not in old["found_by"]:
                        old["found_by"].append(name)
                        old["score"] += 1
                    continue
                d = str(it.get("date", ""))[:10]
                cand.update({"source": str(it.get("source", "")), "date": d if re.match(r"\d{4}-\d{2}-\d{2}$", d) else TODAY,
                             "why": str(it.get("why", "")), "group": g["name"], "domain_hint": g.get("domain", ""),
                             "found_by": [name], "kind": "news", "collected": TODAY, "status": "candidate"})
                cand["score"], cand["hits"] = C.score(cand, kw, companies)
                by_id[cand["id"]] = cand
                by_url[url] = cand
                new += 1
            n_new += new
            status.append({"provider": name, "group": g["name"], "model": MODEL.get(name) or pcfg.get("model"), "ok": err is None, "items": len(items), "new": new, "error": err})
    items = sorted(by_id.values(), key=lambda x: (-x["score"], x["date"]), reverse=False)
    items.sort(key=lambda x: x["date"], reverse=True)
    items.sort(key=lambda x: -x["score"])
    C.save("ai_scan.json", {"generated": NOW.isoformat(timespec="minutes"), "count": len(items), "items": items})
    C.save("ai_scan_status.json", {"run": NOW.isoformat(timespec="minutes"), "calls": status,
                                   "ok": sum(1 for s in status if s["ok"]), "failed": sum(1 for s in status if not s["ok"]), "new_items": n_new})
    print(f"ai scan: {n_new} new candidates, {len(items)} kept, {sum(1 for s in status if not s['ok'])} calls failed")


if __name__ == "__main__":
    main()
