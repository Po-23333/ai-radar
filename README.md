# AI Radar

每天早上 07:07（台灣時間）掃描 GitHub 與 Hugging Face，把「有潛力、跟我有關、可能跟我撞題」的 AI 專案推到 Discord。

```
GitHub Actions (cron)
  fetch  → GitHub Search API（新 repo）/ HF trending models / HF daily papers
  score  → 同來源百分位熱度 + 跨來源加分 + 刷星懲罰 + 興趣相關度 + 撞題規則
  post   → Discord webhook（#daily-radar，撞題另推 #collision-alert 並 @我）
  state  → data/state.json（star 快照、已推送紀錄、message id）commit 回 repo
```

## 設定（一次性）

1. **Discord**：私人伺服器 → `#daily-radar` 與 `#collision-alert` 各建一個 webhook。
2. **GitHub**：建一個 repo（建議 Private），把這個資料夾 push 上去：
   ```bash
   git init -b main && git add . && git commit -m "init ai-radar"
   git remote add origin https://github.com/<你的帳號>/ai-radar.git
   git push -u origin main
   ```
3. **Secrets**：repo → Settings → Secrets and variables → Actions → New repository secret

   | 名稱 | 必填 | 內容 |
   |---|---|---|
   | `DISCORD_WEBHOOK_URL` | ✅ | `#daily-radar` 的 webhook 網址 |
   | `DISCORD_COLLISION_WEBHOOK_URL` | | `#collision-alert` 的 webhook；沒設就併在主頻道 |
   | `DISCORD_USER_ID` | | 你的 Discord 使用者 ID（撞題時 @你，手機會跳通知）|

   `GITHUB_TOKEN` 由 Actions 自動提供，不用設。
4. **試跑**：Actions → ai-radar → Run workflow（預設勾選 dry_run，只產生預覽，可在 run 頁面下載 `preview`）。
   確認沒問題後，取消勾選 dry_run 再跑一次，Discord 就會收到第一則推播。

> 同一天手動重跑會推送「下一批」：推過的東西不會再推（除非「再度升溫」，見下）。只想看效果請用 dry_run。

## 調整口味

全部在 `radar/config.py`：
- `INTERESTS`：興趣標籤與權重 → 決定「跟你有關」
- `COLLISION_RULES`：撞題規則（每組至少命中一個關鍵字、全部組都命中才觸發）→ 你開新專案時就加一條
- `NOISE_KEYWORDS` / `LEARNING_KEYWORDS`：丟掉 / 打折
- `RESURGE_FACTOR` / `RESURGE_MIN_DAYS`：推過的東西只有在速度達推送當時的 N 倍、且隔了幾天以上，才以 🔁 重推（論文不重推）
- `FAMILY_RELATIONS` / `DERIVATIVE_PENALTY`：HF 同一模型的轉檔、adapter 視為同家族，一天只推一個、推過整族不再推；量化轉檔的相關度打折
- `GITHUB_QUERIES`、`BIG_N`、`RELEVANT_N`、`RELEVANT_MAX_PER_SOURCE`、`HF_PAPERS_MAX_AGE_DAYS`

## 本地開發

```bash
python -m unittest -v                                # 離線測試（tests/fixtures）
python -m radar --dry-run --fixtures tests/fixtures --now 2026-09-23T00:00:00Z  # 離線預覽（fixture 日期固定，需搭配 --now）
python -m radar --dry-run --only github              # 真的打 GitHub API 預覽
```

## state.json 格式

- `posted`：`{key: {"d": 推送日期, "v": 推送時速度}}`；`fam:<模型名>` 是 HF 家族紀錄。舊版的日期字串仍可讀，視為「永不重推」。
- `messages[].items`：每則推送當下的 title / section / tags / collision / heat / relevance / velocity，週報與回饋分析直接讀這裡。

## 已知限制（Phase 1）

- 只抓「近期新建」的 repo；老 repo 突然爆紅要等 Phase 2 的快照追蹤。
- 第一天的 star 速度是「總 star / 年齡」估計值；第二天起才有 `實測` 的日增量。
- 描述是原文，還沒有 LLM 中文摘要（Phase 2）。
- 撞題與相關度是關鍵字比對，會有誤報；Phase 3 用 emoji 回饋調權重。
- GitHub 速度混用「實測日增量」與「總星 / 年齡」估計值做同一個百分位排序，剛出現的 repo 會偏高。
- 同一事件的周邊 repo（例如某模型發布後的一堆 wrapper）尚未合併，Phase 2 做事件聚類。

## Roadmap

- **Phase 2**：LLM 一句話中文摘要、週報、追蹤既有熱門 repo 的日增量、HN / r/LocalLLaMA / ModelScope 來源
- **Phase 3**：Bot 讀 👀⭐❌ 回饋自動調權重；👀 的項目自動在 `#deep-read` 開 thread
- **Phase 4**：`/check <點子>` 先行調查（Cloudflare Worker + repository_dispatch）
