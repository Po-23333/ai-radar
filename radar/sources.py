"""資料來源。每個 fetch_* 回傳統一格式的 item（dict）清單。

item 欄位：
  key            全域唯一 id，例如 "gh:owner/repo"、"hfm:org/model"、"hfp:2509.12345"
  source         "github" | "hf-model" | "hf-paper"
  title, url, desc
  text           用來做關鍵字比對的文字
  velocity       熱度原始值（GitHub=stars/天、HF model=trendingScore、HF paper=upvotes）
  velocity_label 顯示用
  gh_link        正規化後的 GitHub repo（"owner/repo"），用來做跨來源比對
  meta           其他顯示資訊
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "ai-radar/0.1"


def http_json(url: str, headers: dict | None = None, retries: int = 3, timeout: int = 30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def parse_time(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def norm_gh(url_or_name):
    """'https://github.com/Foo/Bar.git' -> 'foo/bar'"""
    if not url_or_name:
        return None
    s = str(url_or_name).strip().lower().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    if "github.com/" in s:
        s = s.split("github.com/", 1)[1]
    parts = s.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else None


# ---------------------------------------------------------------- GitHub
def fetch_github(queries, now, token=None, snapshots=None, get=http_json, pause=None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if pause is None:
        pause = 2 if token else 7  # 未驗證的 search API 只有 10 次/分鐘
    snapshots = snapshots or {}
    seen = {}
    for spec in queries:
        since = (now - timedelta(days=spec["days"])).strftime("%Y-%m-%d")
        q = spec["q"].format(since=since)
        for page in range(1, spec.get("pages", 1) + 1):
            url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
                {"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page})
            data = get(url, headers)
            repos = data.get("items", [])
            for r in repos:
                key = "gh:" + r["full_name"].lower()
                if key in seen or r.get("fork") or r.get("archived"):
                    continue
                seen[key] = _github_item(r, key, now, snapshots)
            if len(repos) < 100:
                break
            time.sleep(pause)
        time.sleep(pause)
    return list(seen.values())


def _github_item(r, key, now, snapshots):
    created = parse_time(r.get("created_at"))
    age = max((now - created).total_seconds() / 86400, 0.5) if created else 30.0
    stars = int(r.get("stargazers_count") or 0)

    per_day, label = None, None
    prev = snapshots.get(key)
    if prev:
        pt = parse_time(prev.get("t"))
        dt = (now - pt).total_seconds() / 86400 if pt else 0
        if dt >= 0.5:  # 同一天重跑時沿用舊基準，不算差值
            per_day = max(stars - int(prev.get("stars", 0)), 0) / dt
            label = f"+{per_day:,.0f} ⭐/天（實測）│ 共 {stars:,}"
    if per_day is None:
        per_day = stars / age
        label = f"⭐ {stars:,} │ 建立 {age:.0f} 天 │ ≈{per_day:,.0f}/天"

    topics = r.get("topics") or []
    name_words = r["name"].replace("-", " ").replace("_", " ")
    return {
        "key": key,
        "source": "github",
        "title": r["full_name"],
        "url": r["html_url"],
        "desc": r.get("description") or "",
        "text": " ".join([name_words, r.get("description") or "", " ".join(topics)]),
        "metric": stars,
        "velocity": per_day,
        "velocity_label": label,
        "gh_link": r["full_name"].lower(),
        "meta": {
            "language": r.get("language"),
            "forks": int(r.get("forks_count") or 0),
            "owner_type": (r.get("owner") or {}).get("type"),
            "age_days": age,
            "topics": topics,
        },
    }


# ---------------------------------------------------------------- Hugging Face models
_BORING_TAGS = {"transformers", "safetensors", "pytorch", "endpoints_compatible",
                "autotrain_compatible", "text-generation-inference", "conversational",
                "region:us", "custom_code", "gguf"}


def fetch_hf_models(limit=50, get=http_json):
    url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
        {"sort": "trendingScore", "direction": -1, "limit": limit})
    items = []
    for m in get(url) or []:
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        tags = m.get("tags") or []
        base = None
        for t in tags:  # base_model:quantized:Qwen/Qwen3-8B / base_model:Qwen/Qwen3-8B
            if t.startswith("base_model:"):
                parts = t.split(":")
                rel, name = (parts[1], parts[-1]) if len(parts) >= 3 else ("base", parts[-1])
                if base is None or rel != "base":
                    base = (rel, name)
        likes = int(m.get("likes") or 0)
        downloads = m.get("downloads")
        trend = float(m.get("trendingScore") or 0)
        desc_bits = [m.get("pipeline_tag"), m.get("library_name")]
        if base:
            desc_bits.append(f"{base[0]} of {base[1]}")
        extra = [t for t in tags if ":" not in t and t not in _BORING_TAGS][:6]
        label = f"trending {trend:,.0f} │ ♥ {likes:,}"
        if downloads is not None:
            label += f" │ ⬇ {int(downloads):,}"
        items.append({
            "key": "hfm:" + mid.lower(),
            "source": "hf-model",
            "title": mid,
            "url": f"https://huggingface.co/{mid}",
            "desc": " · ".join(b for b in desc_bits if b) + (f"\ntags: {', '.join(extra)}" if extra else ""),
            "text": " ".join([mid.replace("-", " ").replace("_", " "), " ".join(tags),
                              m.get("pipeline_tag") or ""]),
            "metric": likes,
            "velocity": trend,
            "velocity_label": label,
            "gh_link": None,
            "meta": {"created": m.get("createdAt")},
        })
    return items


# ---------------------------------------------------------------- Hugging Face daily papers
def fetch_hf_papers(now, max_age_days=3, limit=100, get=http_json):
    url = "https://huggingface.co/api/daily_papers?" + urllib.parse.urlencode({"limit": limit})
    raw = get(url) or []
    items = []
    for d in raw:
        p = d.get("paper") or d
        pid = p.get("id")
        if not pid:
            continue
        pub = parse_time(d.get("publishedAt") or p.get("publishedAt"))
        if pub and (now - pub).total_seconds() > max_age_days * 86400:
            continue
        title = (d.get("title") or p.get("title") or pid).replace("\n", " ")
        summary = p.get("ai_summary") or p.get("summary") or d.get("summary") or ""
        upvotes = int(p.get("upvotes") or 0)
        gh = p.get("githubRepo")
        label = f"▲ {upvotes} upvotes"
        if gh and p.get("githubStars") is not None:
            label += f" │ code ⭐ {int(p['githubStars']):,}"
        items.append({
            "key": "hfp:" + pid,
            "source": "hf-paper",
            "title": title,
            "url": f"https://huggingface.co/papers/{pid}",
            "desc": " ".join(summary.split()),
            "text": " ".join([title, p.get("summary") or summary, " ".join(p.get("ai_keywords") or [])]),
            "metric": upvotes,
            "velocity": float(upvotes),
            "velocity_label": label,
            "gh_link": norm_gh(gh),
            "meta": {"arxiv": f"https://arxiv.org/abs/{pid}", "github": gh},
        })
    return items
