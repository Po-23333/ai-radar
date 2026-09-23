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
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"snapshots": {}, "posted": {}, "messages": [], "runs": []}


def save_state(path: Path, state: dict, now: datetime):
    cutoff = now - timedelta(days=C.STATE_RETENTION_DAYS)
    cut_date = cutoff.astimezone(TPE).date().isoformat()
    state["snapshots"] = {k: v for k, v in state["snapshots"].items()
                          if (sources.parse_time(v.get("t")) or now) >= cutoff}
    state["posted"] = {k: d for k, d in state["posted"].items() if d >= cut_date}
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
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows 終端機預設 cp950，印 emoji 會炸
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    now = datetime.now(timezone.utc)
    today = now.astimezone(TPE).date()
    state = load_state(args.state)
    get = make_getter(args.fixtures, args.save_raw)
    only = set(args.only.split(",")) if args.only else None

    fetchers = {
        "github": lambda: sources.fetch_github(C.GITHUB_QUERIES, now, os.getenv("GITHUB_TOKEN"),
                                               state["snapshots"], get=get,
                                               pause=0 if args.fixtures else None),
        "hf-model": lambda: sources.fetch_hf_models(C.HF_MODELS_LIMIT, get=get),
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
    msgs = discord.build_messages(picked, today, stats, errors, os.getenv("DISCORD_USER_ID") or None,
                                  separate_collision=bool(col_hook))

    if args.dry_run or not main_hook:
        if not args.dry_run:
            print("[warn] 沒有設定 DISCORD_WEBHOOK_URL，改為 dry-run", file=sys.stderr)
        args.preview.write_text(json.dumps([{"channel": c, "payload": p, "keys": k} for c, p, k in msgs],
                                           ensure_ascii=False, indent=2), encoding="utf-8")
        print_preview(msgs)
        return 0 if items else 1

    for channel, payload, keys in msgs:
        hook = col_hook if channel == "collision" else main_hook
        mid = discord.post(hook, payload)
        if keys:
            state["messages"].append({"id": mid, "channel": channel, "date": today.isoformat(), "keys": keys})
            for k in keys:
                state["posted"][k] = today.isoformat()
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
