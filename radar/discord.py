"""把挑選結果轉成 Discord webhook payload，並負責發送。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

# Discord 限制：https://discord.com/developers/docs/resources/message#embed-object-embed-limits
MAX_EMBEDS = 10
MAX_TOTAL = 6000
MAX_TITLE = 256
MAX_DESC = 4096
MAX_FOOTER = 2048
MAX_CONTENT = 2000

COLORS = {"big": 0xF2A33A, "relevant": 0x3B82F6, "collision": 0xE5484D}
HEADINGS = {"big": "### 🔥 今日大事", "relevant": "### 🎯 跟你有關", "collision": "### 🚨 撞題警報"}
SRC = {"github": "GitHub", "hf-model": "HF Model", "hf-paper": "HF Paper"}
WEEKDAY = "一二三四五六日"


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def to_embed(it: dict, section: str) -> dict:
    lines = [_trunc(it["desc"] or "（沒有描述）", 350), ""]
    lines.append(f"`{it['velocity_label']}`")
    if it["tags"]:
        lines.append("相關：" + "、".join(it["tags"]))
    if it["collision"]:
        lines.append("**可能撞題：" + "、".join(it["collision"]) + "**")
    lines += it["flags"]
    footer = " + ".join(SRC.get(s, s) for s in it["sources"])
    lang = it.get("meta", {}).get("language")
    if lang:
        footer += f" · {lang}"
    return {
        "title": _trunc(it["title"], MAX_TITLE),
        "url": it["url"],
        "description": _trunc("\n".join(lines), MAX_DESC),
        "color": COLORS[section],
        "footer": {"text": _trunc(footer, MAX_FOOTER)},
    }


def _embed_len(e):
    return len(e.get("title", "")) + len(e.get("description", "")) + len(e.get("footer", {}).get("text", ""))


def _chunk(pairs):
    """pairs=[(key, embed)]；依 10 個 / 6000 字的上限切成多則訊息。"""
    out, cur, size = [], [], 0
    for pair in pairs:
        n = _embed_len(pair[1])
        if cur and (len(cur) >= MAX_EMBEDS or size + n > MAX_TOTAL):
            out.append(cur)
            cur, size = [], 0
        cur.append(pair)
        size += n
    if cur:
        out.append(cur)
    return out


def build_messages(picked: dict, today, stats: dict, errors: list, user_id: str | None,
                   separate_collision: bool):
    """回傳 [(channel, payload, keys)]，channel ∈ {"main", "collision"}。"""
    head = f"## 📡 AI Radar · {today.isoformat()}（週{WEEKDAY[today.weekday()]}）\n"
    head += (f"掃描 GitHub {stats.get('github', 0)} · HF 模型 {stats.get('hf-model', 0)} · "
             f"HF 論文 {stats.get('hf-paper', 0)}，過濾後候選 {picked['pool_size']} 則")
    if errors:
        head += "\n⚠ 來源失敗：" + "；".join(errors)
    if not any(picked[s] for s in ("big", "relevant", "collision")):
        head += "\n今天沒有新東西。"
    none = {"parse": []}
    msgs = [("main", {"content": _trunc(head, MAX_CONTENT), "allowed_mentions": none}, [])]

    shown_main = set()
    for section in ("big", "relevant", "collision"):
        items = picked[section]
        channel = "main"
        if section == "collision":
            if separate_collision:
                channel = "collision"
            else:
                items = [i for i in items if i["key"] not in shown_main]
        if not items:
            continue
        content = HEADINGS[section]
        mentions = none
        if section == "collision" and user_id:
            content += f" <@{user_id}>"
            mentions = {"parse": [], "users": [user_id]}
        pairs = [(i["key"], to_embed(i, section)) for i in items]
        for k, chunk in enumerate(_chunk(pairs)):
            payload = {"content": content if k == 0 else "", "embeds": [e for _, e in chunk],
                       "allowed_mentions": mentions}
            msgs.append((channel, payload, [key for key, _ in chunk]))
        if channel == "main":
            shown_main |= {i["key"] for i in items}
    return msgs


def post(webhook_url: str, payload: dict, retries: int = 4) -> str | None:
    """發送並回傳 message id（?wait=true 才會回傳訊息內容）。"""
    url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
    body = json.dumps({"username": "AI Radar", **payload}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "ai-radar/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return (json.load(r) or {}).get("id")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                try:
                    wait = float(json.load(e).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Discord HTTP {e.code}: {detail}") from None
    return None
