"""進入點：python -m radar [--dry-run] [--fixtures DIR] [--save-raw DIR]"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from . import discord, score, sources

TPE = timezone(timedelta(hours=8))  # 台灣沒有日光節約時間，固定 +8 即可


# ---------------------------------------------------------------- state
def load_state(path: Path) -> dict:
    if not path.exists():
        return {"snapshots": {}, "posted": {}, "messages": [], "runs": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    # 舊版 posted 沒有家族紀錄：推過的 HF 模型補上自己的家族 key，避免它的轉檔版之後又被推
    for k, v in list(state["posted"].items()):
        if k.startswith("hfm:"):
            state["posted"].setdefault(sources.model_family(k[4:]), v)
    return state


def save_state(path: Path, state: dict, now: datetime):
    cutoff = now - timedelta(days=C.STATE_RETENTION_DAYS)
    cut_date = cutoff.astimezone(TPE).date().isoformat()
    posted_cut = (now - timedelta(days=C.POSTED_RETENTION_DAYS)).astimezone(TPE).date().isoformat()
    state["snapshots"] = {k: v for k, v in state["snapshots"].items()
                          if (sources.parse_time(v.get("t")) or now) >= cutoff}
    state["posted"] = {k: v for k, v in state["posted"].items()
                       if score.posted_entry(v)["d"] >= posted_cut}
    state["messages"] = [m for m in state["messages"] if m["date"] >= cut_date]
    state["runs"] = state["runs"][-C.STATE_RETENTION_DAYS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- fetch helpers
def make_getter(fixtures: Path | None, save_raw: Path | None):
    def name_for(url):
        if "api.github.com" in url:
            return "github.json"
        if "daily_papers" in url:
            return "hf_papers.json"
        return "hf_models.json"

    def get(url, headers=None):
        if fixtures:
            return json.loads((fixtures / name_for(url)).read_text(encoding="utf-8"))
        data = sources.http_json(url, headers)
        if save_raw:
            save_raw.mkdir(parents=True, exist_ok=True)
            (save_raw / name_for(url)).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    return get


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="radar")
    ap.add_argument("--dry-run", action="store_true", help="不發送、不寫 state，只印出預覽")
    ap.add_argument("--fixtures", type=Path, help="用本地 JSON 取代網路請求（離線測試）")
    ap.add_argument("--save-raw", type=Path, help="把 API 原始回應存起來（做 fixture 用）")
    ap.add_argument("--state", type=Path, default=Path("data/state.json"))
    ap.add_argument("--preview", type=Path, default=Path("preview.json"))
    ap.add_argument("--only", help="只跑部分來源，逗號分隔：github,hf-model,hf-paper")
    ap.add_argument("--now", help="假裝現在是這個時間（ISO 格式），讓 fixture 預覽不會隨日期失效")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows 終端機預設 cp950，印 emoji 會炸
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    now = sources.parse_time(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        ap.error(f"--now 無法解析：{args.now}")
    today = now.astimezone(TPE).date()
    state = load_state(args.state)
    get = make_getter(args.fixtures, args.save_raw)
    only = set(args.only.split(",")) if args.only else None

    fetchers = {
        "github": lambda: sources.fetch_github(C.GITHUB_QUERIES, now, os.getenv("GITHUB_TOKEN"),
                                               state["snapshots"], get=get,
                                               pause=0 if args.fixtures else None),
        "hf-model": lambda: sources.fetch_hf_models(C.HF_MODELS_LIMIT, get=get,
                                                    family_relations=C.FAMILY_RELATIONS),
        "hf-paper": lambda: sources.fetch_hf_papers(now, C.HF_PAPERS_MAX_AGE_DAYS, get=get),
    }
    items, errors, stats = [], [], {}
    for name, fn in fetchers.items():
        if only and name not in only:
            continue
        try:
            got = fn()
            stats[name] = len(got)
            items += got
            print(f"[fetch] {name}: {len(got)}", file=sys.stderr)
        except Exception as e:  # 單一來源失敗不影響其他來源
            msg = f"{discord.SRC.get(name, name)}（{type(e).__name__}: {str(e)[:80]}）"
            errors.append(msg)
            print(f"[fetch] {name} FAILED: {e}", file=sys.stderr)

    score.annotate(items)
    score.compute_heat(items)
    picked = score.select(items, state["posted"], today)

    main_hook = os.getenv("DISCORD_WEBHOOK_URL")
    col_hook = os.getenv("DISCORD_COLLISION_WEBHOOK_URL")
    preview_only = args.dry_run or not main_hook
    # 預覽檔會被上傳成 Actions artifact；公開 repo 的 artifact 任何登入者都能下載
    # → 預覽一律用占位字串，真實 user ID 只出現在送往 Discord 的請求裡
    user_id = "DISCORD_USER_ID" if preview_only else (os.getenv("DISCORD_USER_ID") or None)
    msgs = discord.build_messages(picked, today, stats, errors, user_id,
                                  separate_collision=bool(col_hook))

    if preview_only:
        if not args.dry_run:
            print("[warn] 沒有設定 DISCORD_WEBHOOK_URL，改為 dry-run", file=sys.stderr)
        args.preview.write_text(json.dumps([{"channel": c, "payload": p, "keys": k} for c, p, k in msgs],
                                           ensure_ascii=False, indent=2), encoding="utf-8")
        print_preview(msgs)
        return 0 if items else 1

    by_key = {i["key"]: i for i in items}
    section_of = {}
    for sec in ("big", "relevant", "collision"):
        for i in picked[sec]:
            section_of.setdefault(i["key"], sec)  # 主頻道裡一個 key 只會出現在一個區塊

    for channel, payload, keys in msgs:
        hook = col_hook if channel == "collision" else main_hook
        mid = discord.post(hook, payload)
        if keys:
            records = []
            for k in keys:
                it = by_key[k]
                sec = "collision" if channel == "collision" else section_of[k]
                records.append(record_of(it, sec))
                entry = {"d": today.isoformat(), "v": round(float(it["velocity"]), 2)}
                state["posted"][k] = entry
                if it.get("family"):
                    state["posted"][it["family"]] = entry
            state["messages"].append({"id": mid, "channel": channel, "date": today.isoformat(),
                                      "keys": keys, "items": records})
        time.sleep(1.0)

    # 更新 GitHub star 快照（同一天重跑不覆蓋，保留前一天的基準）
    for it in items:
        if it["source"] != "github":
            continue
        prev = state["snapshots"].get(it["key"])
        pt = sources.parse_time(prev.get("t")) if prev else None
        if not pt or (now - pt) >= timedelta(hours=20):
            state["snapshots"][it["key"]] = {"stars": it["metric"], "t": now.isoformat(timespec="seconds")}

    state["runs"].append({"date": today.isoformat(), "stats": stats, "errors": errors,
                          "picked": {s: len(picked[s]) for s in ("big", "relevant", "collision")}})
    save_state(args.state, state, now)
    print(f"[done] posted {len(msgs)} messages", file=sys.stderr)
    return 0


def record_of(it: dict, section: str) -> dict:
    """推送當下的快照：之後做週報、回查撞題、Phase 3 回饋調權重都靠這個，不用重抓 API。"""
    return {
        "key": it["key"], "title": it["title"], "url": it["url"], "source": it["source"],
        "section": section, "tags": it["tags"], "collision": it["collision"],
        "heat": round(it["heat"], 3), "relevance": round(it["relevance"], 3),
        "velocity": round(float(it["velocity"]), 2), "metric": it["metric"],
        "flags": it["flags"],
    }


def print_preview(msgs):
    for channel, payload, _ in msgs:
        print(f"\n===== #{channel} =====")
        if payload.get("content"):
            print(payload["content"])
        for e in payload.get("embeds", []):
            print(f"\n  ▌{e['title']}\n  ▌{e['url']}")
            for line in e["description"].splitlines():
                print(f"  ▌  {line}")
            print(f"  ▌  — {e['footer']['text']}")
