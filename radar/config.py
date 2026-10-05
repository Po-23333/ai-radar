"""個人化設定：調整雷達口味只需要改這個檔案。

關鍵字比對規則（見 score.py）：
- 不分大小寫，對 name / description / topics / tags 做比對
- 左邊界是「單字開頭」，右邊不限 → "quantiz" 可命中 quantization / quantized
- 長度 <= 3 的關鍵字（ai, rag, mcp...）兩邊都要是單字邊界，避免 "ai" 命中 "mail"
"""

# ---- 興趣輪廓：標籤 → (權重, 關鍵字) -------------------------------------------
INTERESTS = {
    "推論/量化": (1.0, ["inference", "quantiz", "gguf", "llama.cpp", "vllm", "sglang",
                     "kv cache", "kv-cache", "speculative", "awq", "gptq", "exllama",
                     "flash attention", "flash-attn", "serving", "throughput", "ttft",
                     "int4", "int8", "fp8", "4-bit", "8-bit"]),
    "評測/幻覺": (1.0, ["benchmark", "eval", "hallucin", "factual", "leaderboard", "grounding"]),
    "中文/國產模型": (0.8, ["qwen", "deepseek", "kimi", "moonshot", "glm", "chinese", "minimax",
                      "繁體", "中文", "taiwan", "modelscope"]),
    "金融 NLP": (1.0, ["financ", "stock", "earnings", "annual report", "fintech", "trading",
                     "10-k", "財報", "股"]),
    "訓練/對齊": (0.6, ["fine-tun", "finetun", "lora", "rlhf", "grpo", "dpo", "distill",
                     "pretrain", "reasoning", "reinforcement"]),
    "Agent/RAG": (0.5, ["agent", "rag", "retrieval", "mcp", "tool use", "tool-use"]),
    "CV/偵測": (0.6, ["yolo", "object detection", "segmentation", "crack", "defect",
                    "inspection", "detr"]),
}

# ---- 撞題規則：每個「群組」至少命中一個關鍵字，所有群組都命中才觸發 -----------------
COLLISION_RULES = {
    "MOPS 財報數值幻覺 benchmark": [
        ["hallucin", "numer", "factual"],
        ["financ", "財報", "earnings", "annual report"],
    ],
    "中文金融 LLM 評測": [
        ["benchmark", "eval"],
        ["financ", "財報"],
        ["chinese", "中文", "繁體", "taiwan"],
    ],
    "量化 × 推論效能實驗": [
        ["quantiz", "4-bit", "int4", "gguf", "awq", "gptq"],
        ["benchmark", "throughput", "latency", "ttft", "kv cache"],
    ],
    "YOLO 裂縫偵測": [
        ["crack", "pavement", "concrete"],
        ["detect", "yolo", "segment"],
    ],
}

# ---- GitHub 專案必須看起來跟 AI 有關才收（HF 來源本來就是 AI，不套用）-------------
AI_KEYWORDS = [
    "llm", "ai", "gpt", "agent", "model", "transformer", "diffusion", "neural",
    "machine learning", "deep learning", "ml", "inference", "embedding", "rag", "lora",
    "vision", "speech", "tts", "asr", "ocr", "multimodal", "vlm", "claude", "openai",
    "gemini", "qwen", "deepseek", "llama", "mistral", "ollama", "mcp", "chatbot",
    "copilot", "reinforcement", "token", "prompt", "yolo", "pytorch", "cuda",
]

# ---- 雜訊：命中就丟掉 --------------------------------------------------------------
NOISE_KEYWORDS = [
    "awesome", "jailbreak", "airdrop", "crypto", "memecoin", "cracked", "keygen",
    "v2ray", "leaked", "system prompts", "free api key", "free-api",
    "uncensored", "abliterat", "nsfw",
]

# ---- 教材類（教程、學習路線、面試題）：不丟掉，但相關度打折 ---------------------------
LEARNING_KEYWORDS = [
    "tutorial", "course", "roadmap", "cookbook", "interview", "handbook", "learning path",
    "教程", "学习路线", "學習路線", "面试", "面試", "入门", "入門",
]
LEARNING_PENALTY = 0.5

# ---- HF 衍生模型 ---------------------------------------------------------------------
# 量化轉檔（GGUF/AWQ/...）：熱度照算，但「跟你有關」打折 —— 轉檔不等於推論研究
DERIVATIVE_PENALTY = 0.4
# 哪些 base_model 關係算「同一家族」（同家族一天只推一個、推過就整族不再推）
# finetune 不算：微調 / 蒸餾是新工作，不是換包裝
FAMILY_RELATIONS = ("quantized", "adapter", "merge")

# ---- GitHub 搜尋：{since} 會被換成 now - days --------------------------------------
GITHUB_QUERIES = [
    # 一週內新建、已經有一定熱度 → 「大事」候選
    {"q": "created:>{since} stars:>=40", "days": 7, "pages": 2},
    # 三週內新建、跟你的方向有關，門檻較低 → 「跟你有關」候選
    {"q": "created:>{since} stars:>=10 llm OR inference OR quantization OR benchmark OR hallucination",
     "days": 21, "pages": 1},
]

HF_MODELS_LIMIT = 50
HF_PAPERS_MAX_AGE_DAYS = 5  # HF 週末不上新論文；3 天會讓週日、週一固定 0 篇

# ---- 版面 --------------------------------------------------------------------------
BIG_N = 3               # 今日大事
BIG_MAX_PER_SOURCE = 2  # 大事裡同一來源最多幾則（避免被 GitHub 洗版）
RELEVANT_N = 5          # 跟你有關
RELEVANT_MAX_PER_SOURCE = 3  # 跟你有關裡同一來源最多幾則（論文摘要長、命中多，會洗版）
COLLISION_N = 5         # 撞題警報

# ---- 重推規則：推過的東西預設不再推，除非「再度升溫」 ------------------------------
RESURGE_SOURCES = ("github", "hf-model")  # 論文的 upvotes 是累積值，不是速度，不適用
RESURGE_FACTOR = 2.0      # 目前速度 >= 推送當時的幾倍
RESURGE_MIN_DAYS = 3      # 距離上次推送至少幾天

STATE_RETENTION_DAYS = 30   # star 快照、訊息紀錄、run 紀錄
POSTED_RETENTION_DAYS = 90  # 已推送紀錄（決定「推過」能記多久）
