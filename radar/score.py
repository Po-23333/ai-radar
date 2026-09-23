"""標註（相關度、撞題、雜訊）→ 熱度 → 挑選三個區塊。"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

from . import config as C


def _compile(kw: str):
    kw = kw.lower()
    pat = r"(?<![a-z0-9])" + re.escape(kw)
    if len(kw) <= 3 and kw.isascii():
        pat += r"(?![a-z])"
    return re.compile(pat)


_INTERESTS = {tag: (w, [_compile(k) for k in kws]) for tag, (w, kws) in C.INTERESTS.items()}
_COLLISION = {name: [[_compile(k) for k in g] for g in groups] for name, groups in C.COLLISION_RULES.items()}
_AI = [_compile(k) for k in C.AI_KEYWORDS]
_NOISE = [_compile(k) for k in C.NOISE_KEYWORDS]
_LEARN = [_compile(k) for k in C.LEARNING_KEYWORDS]


def _hit(pats, text):
    return any(p.search(text) for p in pats)


def annotate(items):
    for it in items:
        text = it["text"].lower()
        tags = [t for t, (_, pats) in _INTERESTS.items() if _hit(pats, text)]
        it["tags"] = tags
        # 最強的標籤算滿分，其他標籤只加 30%，避免「關鍵字塞滿」的 repo 霸榜
        ws = sorted((_INTERESTS[t][0] for t in tags), reverse=True)
        it["relevance"] = (ws[0] + 0.3 * sum(ws[1:])) if ws else 0.0
        it["collision"] = [n for n, groups in _COLLISION.items() if all(_hit(g, text) for g in groups)]
        it["noise"] = _hit(_NOISE, text)
        it["ai"] = it["source"] != "github" or _hit(_AI, text)
        it["sources"] = [it["source"]]
        it["flags"] = []
        if _hit(_LEARN, text):
            it["relevance"] *= C.LEARNING_PENALTY
            it["flags"].append("📚 教材類")
    return items


def compute_heat(items):
    """熱度 = 同來源內的百分位（0~1）+ 調整。不同來源的原始數字不可比，所以用排名。"""
    by_src = {}
    for it in items:
        by_src.setdefault(it["source"], []).append(it)
    for group in by_src.values():
        group.sort(key=lambda i: i["velocity"])
        n = len(group)
        for rank, it in enumerate(group):
            it["heat"] = rank / (n - 1) if n > 1 else 1.0

    # 跨來源：論文附的 code repo 同時出現在 GitHub 熱門清單
    gh_items = {i["gh_link"]: i for i in items if i["source"] == "github"}
    for it in items:
        if it["source"] == "hf-paper" and it["gh_link"] in gh_items:
            g = gh_items[it["gh_link"]]
            for a, b in ((it, g), (g, it)):
                a["heat"] += 0.15
                if b["source"] not in a["sources"]:
                    a["sources"].append(b["source"])
            it["flags"].append(f"🔗 程式碼也在 GitHub 熱門：{g['title']}")
            g["flags"].append(f"🔗 有對應論文：{it['title'][:80]}")

    for it in items:
        if it["source"] != "github":
            continue
        m = it["meta"]
        # 刷星啟發式：很新、星很多、幾乎沒人 fork
        if m["age_days"] <= 3 and it["metric"] >= 300 and m["forks"] < max(3, it["metric"] * 0.01):
            it["heat"] *= 0.5
            it["flags"].append("⚠ star/fork 比例異常，可能刷星")
        if m.get("owner_type") == "Organization":
            it["heat"] += 0.05
    return items


def select(items, posted: dict, today: date):
    cutoff = (today - timedelta(days=C.REPOST_COOLDOWN_DAYS)).isoformat()
    pool = [i for i in items
            if i["ai"] and not i["noise"] and posted.get(i["key"], "0000") < cutoff]
    pool.sort(key=lambda i: i["heat"], reverse=True)

    big, per_src = [], Counter()
    for it in pool:
        if per_src[it["source"]] >= C.BIG_MAX_PER_SOURCE:
            continue
        big.append(it)
        per_src[it["source"]] += 1
        if len(big) >= C.BIG_N:
            break
    shown = {i["key"] for i in big}

    relevant = sorted((i for i in pool if i["relevance"] > 0 and i["key"] not in shown),
                      key=lambda i: i["relevance"] * (0.4 + i["heat"]), reverse=True)[:C.RELEVANT_N]
    shown |= {i["key"] for i in relevant}

    collision = [i for i in pool if i["collision"]][:C.COLLISION_N]
    return {"big": big, "relevant": relevant, "collision": collision, "pool_size": len(pool)}
