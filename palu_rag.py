"""
帕鲁 v3 — RAG + 语义缓存 + 三层保底
【概念演示】Day 2 增强版：缓存命中→直接返回 / 高置信→返回原文 / 低阈值→说不知道
"""

import os
import json
import hashlib
import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ============================================================
# 第一步：配置文件（参数可调）
# ============================================================
EMBEDDING_MODEL = "text-embedding-v3"       # Embedding 模型
EMBEDDING_DIMENSIONS = 1024                  # 向量维度
LLM_MODEL = "deepseek-chat"                 # 回答用的模型
TOP_K = 3                                    # ← 调这个：检索取前几条
TEMPERATURE = 0.2                            # 低温保准确

# 【参数可调】三层保底阈值
SIMILARITY_THRESHOLD = 0.5                   # ← 调这个：Level 3 — 低于此值不调 AI
HIGH_CONFIDENCE_THRESHOLD = 0.85             # ← 调这个：Level 2 — 高于此值直接返回原文
# 精确缓存就够了，不需要语义缓存（省一次 Embedding 调用）

# ============================================================
# 第二步：搭两座桥 — Embedding 桥 + LLM 桥
# ============================================================
embed_client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

llm_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1",
)

# ============================================================
# 第三步：加载知识库 + 预计算向量
# ============================================================
def load_knowledge_base(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_embedding(text: str) -> list[float]:
    resp = embed_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    return resp.data[0].embedding

def cosine_similarity(a: list[float], b: list[float]) -> float:
    a_arr, b_arr = np.array(a), np.array(b)
    dot = np.dot(a_arr, b_arr)
    norm_a, norm_b = np.linalg.norm(a_arr), np.linalg.norm(b_arr)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))

print("加载知识库中...")
kb = load_knowledge_base("售后知识库.json")
print(f"已加载 {len(kb)} 条 FAQ")

# search_text = question + answer，问题答案一起搜
# 同时存 question 原文——Level 1 精确命中要拿它做哈希匹配
for item in kb:
    item["search_text"] = f"{item['question']} {item['answer']}"

print("预计算向量中...")
for item in kb:
    item["embedding"] = get_embedding(item["search_text"])
    print(f"  ✅ {item['id']} {item['question']}")

print(f"知识库加载完成，共 {len(kb)} 条\n")

# ============================================================
# 第四步：精确缓存 — 相同问题不重复调 API
# ============================================================
# 【逻辑说明】用问题原文的 MD5 hash 做 key
# 问题一模一样 → hash 一样 → 命中
# 只做精确匹配，不做语义匹配（省一次 Embedding 调用）
class ExactCache:
    """精确缓存：用 hash 做 key，同样问题第二次秒回"""
    def __init__(self):
        self._cache: dict[str, str] = {}
        self.hit_count = 0
        self.miss_count = 0

    def _hash(self, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def get(self, query: str) -> str | None:
        h = self._hash(query)
        if h in self._cache:
            self.hit_count += 1
            return self._cache[h]
        self.miss_count += 1
        return None

    def put(self, query: str, answer: str):
        h = self._hash(query)
        self._cache[h] = answer

    def stats(self) -> str:
        total = self.hit_count + self.miss_count
        rate = self.hit_count / total * 100 if total > 0 else 0
        return f"缓存命中：{self.hit_count}/{total}（{rate:.0f}%）"

# 创建全局缓存实例
cache = ExactCache()

# ============================================================
# 第五步：向量检索
# ============================================================
def search_knowledge(query: str) -> list[dict]:
    """检索：问题 → 向量 → 找最相关的知识"""
    query_vec = get_embedding(query)
    
    scored = []
    for item in kb:
        score = cosine_similarity(query_vec, item["embedding"])
        scored.append({
            "id": item["id"], "question": item["question"],
            "answer": item["answer"], "score": score,
            "category": item["category"]
        })
    
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:TOP_K]

# ============================================================
# 第六步：三层保底 + RAG 生成
# ============================================================
# 【逻辑说明】三层保底架构：
#   Level 1 精确命中 → FAQ 原文直接返回（0 Token 消耗）
#   Level 2 高置信度 → 最佳匹配 > 0.85，直接返回原文（0 Token 消耗）
#   Level 3 RAG 带阈值 → 0.5~0.85 之间，调 LLM 基于知识库回答
#   Level 3 拒绝 → 低于 0.5，说"不知道"
def ask_palu_with_guardrails(user_question: str) -> str:
    """帕鲁 v3：缓存优先 → 三层保底 → RAG 生成"""

    # ========== 第零步：查语义缓存 ==========
    cached = cache.get(user_question)
    if cached is not None:
        print(f"\n📎 缓存命中 ✅（共 {cache.hit_count + cache.miss_count} 次）")
        return cached

    # ========== 第一步：向量检索 ==========
    results = search_knowledge(user_question)
    best = results[0] if results else None
    best_score = best["score"] if best else 0.0

    print(f"\n📎 检索结果（最佳匹配：{best_score:.3f}）")
    for r in results:
        print(f"   [{r['score']:.3f}] {r['question']}")

    # ========== 第二步：Level 1 — 精确命中 ==========
    # 【逻辑说明】如果最佳匹配的分数接近 1.0 且问题文字高度重合
    # 说明用户问的就是这个 FAQ 本身，直接返回原文
    if best_score > 0.95:
        print("   → Level 1 精确命中，直接返回原文")
        cache.put(user_question, best["answer"])
        return best["answer"]

    # ========== 第三步：Level 2 — 高置信度 ==========
    # 【逻辑说明】分数 > 0.85，说明检索结果非常相关
    # 直接返回原文，省掉一次 LLM 调用（省时间省钱）
    if best_score >= HIGH_CONFIDENCE_THRESHOLD:
        print("   → Level 2 高置信度，直接返回原文")
        cache.put(user_question, best["answer"])
        return best["answer"]

    # ========== 第四步：Level 3 — 低于阈值，拒绝回答 ==========
    if best_score < SIMILARITY_THRESHOLD:
        print("   → Level 3 低于阈值，拒绝回答")
        reply = "抱歉，这个问题我还没学到，建议联系人工客服处理。"
        cache.put(user_question, reply)
        return reply

    # ========== 第五步：Level 3 — 正常 RAG ==========
    # 分数在 0.5~0.85 之间，调 LLM 基于知识库生成回答
    print("   → Level 3 RAG 生成")
    context = "\n\n".join([
        f"【FAQ {r['id']}】问题：{r['question']}\n回答：{r['answer']}"
        for r in results
    ])

    system_prompt = f"""你是一个叫「帕鲁」的售后客服助手。说话亲切、专业、简洁。

请基于以下知识库内容回答问题。如果知识库足够回答，就直接回答。
如果知识库内容不全，就结合知识库给出你能给的最佳建议。

知识库内容：
{context}"""

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_question}
        ],
        temperature=TEMPERATURE,
        max_tokens=500
    )

    reply = response.choices[0].message.content
    # 记住这次回答，下次同样问题直接命中缓存
    cache.put(user_question, reply)
    return reply

# ============================================================
# 第七步：跑起来
# ============================================================
if __name__ == "__main__":
    print("🐤 帕鲁 v3（缓存+三层保底）已上线！输入问题，输入 stats 看缓存统计，quit 退出\n")
    
    while True:
        user_input = input("你：")
        
        if user_input.lower() == "quit":
            print(f"帕鲁：拜拜~（{cache.stats()}）")
            break
        
        if user_input.lower() == "stats":
            print(f"📊 {cache.stats()}")
            continue
        
        reply = ask_palu_with_guardrails(user_input)
        print(f"\n帕鲁：{reply}\n")
