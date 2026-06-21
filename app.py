"""
帕鲁 Web 服务 — Flask 版
开机常驻，通过 HTTP 接口接收问题，返回回答
特性：注入防御 + 输出脱敏 + API 降级 + 禁答主题 + 三层保底 + 状态监控 + 全局错误处理
"""

import os
import re
import json
import hashlib
import time
import uuid
import sqlite3
import atexit
import logging
from datetime import datetime
import numpy as np
from dotenv import load_dotenv
from openai import OpenAI
from flask import Flask, request, jsonify
from waitress import serve

load_dotenv()

# ============================================================
# 日志
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("palu")

# ============================================================
# 配置
# ============================================================
EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIMENSIONS = 1024
LLM_MODEL = "deepseek-chat"
TOP_K = 3
TEMPERATURE = 0.2
SIMILARITY_THRESHOLD = 0.65
HIGH_CONFIDENCE_THRESHOLD = 0.85
DEGRADE_AFTER_FAILURES = 3
DEGRADE_COOLDOWN = 30
MAX_CONVERSATION_TURNS = 10  # 【参数可调】多轮对话保留轮数
RATE_LIMIT_PER_MINUTE = 20   # 【参数可调】每分钟每个 IP 最多请求数
RATE_LIMIT_LLM_PER_MINUTE = 6  # 【参数可调】每分钟每个 IP 最多 LLM 调用数
PERSISTENCE_DB = "palu_data.db"  # 持久化数据库文件

# ============================================================
# 持久化（SQLite）
# ============================================================
def init_db():
    conn = sqlite3.connect(PERSISTENCE_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS exact_cache (
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.commit()
    return conn

def save_db(conn, table, key, value):
    conn.execute("REPLACE INTO {} (key, value) VALUES (?, ?)".format(table), (key, value))
    conn.commit()

def load_all(conn, table):
    cursor = conn.execute(f"SELECT key, value FROM {table}")
    return dict(cursor.fetchall())

db_conn = init_db()

# ============================================================
# 限流器
# ============================================================
class RateLimiter:
    """滑动窗口限流，按 IP 统计"""
    def __init__(self):
        self._windows = {}  # key -> [(timestamp, ...)]

    def _trim(self, key, window_seconds):
        now = time.time()
        if key not in self._windows:
            self._windows[key] = []
            return now
        cutoff = now - window_seconds
        self._windows[key] = [t for t in self._windows[key] if t > cutoff]
        return now

    def allow(self, key, max_count, window_seconds=60):
        now = self._trim(key, window_seconds)
        if len(self._windows[key]) >= max_count:
            return False
        self._windows[key].append(now)
        return True

rate_limiter = RateLimiter()


# ============================================================
# 系统状态统计
# ============================================================
class Stats:
    def __init__(self):
        self.total_requests = 0
        self.injection_blocked = 0
        self.forbidden_blocked = 0
        self.cache_hits = 0
        self.llm_calls = 0
        self.degrade_count = 0
        self.errors = 0
        self.transfer_to_human = 0
        self.rate_limited = 0
        self.start_time = time.time()

    def to_dict(self):
        uptime = int(time.time() - self.start_time)
        return {
            "uptime_seconds": uptime,
            "uptime_str": f"{uptime//3600}h{(uptime%3600)//60}m{uptime%60}s",
            "total_requests": self.total_requests,
            "injection_blocked": self.injection_blocked,
            "forbidden_blocked": self.forbidden_blocked,
            "cache_hits": self.cache_hits,
            "cache_hit_rate": f"{self.cache_hits/max(self.total_requests,1)*100:.1f}%",
            "llm_calls": self.llm_calls,
            "degrade_count": self.degrade_count,
            "errors": self.errors,
            "transfer_to_human": self.transfer_to_human,
            "rate_limited": self.rate_limited,
        }

stats = Stats()

# ============================================================
# 搭桥
# ============================================================
embed_client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
llm_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1",
)
FALLBACK_LLM_CLIENT = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
FALLBACK_LLM_MODEL = "qwen-turbo"

# ============================================================
# 知识库 + 缓存
# ============================================================
def load_kb(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_embedding(text):
    resp = embed_client.embeddings.create(
        model=EMBEDDING_MODEL, input=text, dimensions=EMBEDDING_DIMENSIONS
    )
    return resp.data[0].embedding

def cosine_similarity(a, b):
    a_arr, b_arr = np.array(a), np.array(b)
    norm = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    return 0.0 if norm == 0 else float(np.dot(a_arr, b_arr) / norm)

print("加载知识库中...")
kb = load_kb("售后知识库.json")
for item in kb:
    item["search_text"] = f"{item['question']} {item['answer']}"
print("预计算向量中...")
embed_fail_count = 0
for item in kb:
    try:
        item["embedding"] = get_embedding(item["search_text"])
    except Exception as e:
        item["embedding"] = None
        embed_fail_count += 1
        log.warning(f"第 {embed_fail_count} 条 Embedding 失败: {item['question'][:20]}... {e}")
if embed_fail_count:
    print(f"⚠️ {embed_fail_count} 条 Embedding 失败，服务将使用关键词降级")
else:
    print("全部 Embedding 成功")
print(f"知识库加载完成，共 {len(kb)} 条，服务已就绪\n")

class ExactCache:
    def __init__(self, db_conn):
        self._cache = {}
        self._db = db_conn
        self.hit_count = 0
        self.miss_count = 0
        # 从 DB 加载
        rows = load_all(db_conn, "exact_cache")
        self._cache = rows
        if rows:
            log.info(f"缓存恢复 {len(rows)} 条")
    def _hash(self, text):
        return hashlib.md5(text.encode("utf-8")).hexdigest()
    def get(self, query):
        h = self._hash(query)
        if h in self._cache:
            self.hit_count += 1
            return self._cache[h]
        self.miss_count += 1
        return None
    def put(self, query, answer):
        h = self._hash(query)
        self._cache[h] = answer
        save_db(self._db, "exact_cache", h, answer)

cache = ExactCache(db_conn)

# ============================================================
# 多轮对话管理
# ============================================================
class ConversationManager:
    def __init__(self, db_conn):
        self._db = db_conn
        self._sessions = {}  # sid -> {"messages":[...], "low_score_count":0}
        # 从 DB 加载
        rows = load_all(db_conn, "sessions")
        for sid, data_json in rows.items():
            self._sessions[sid] = json.loads(data_json)
        if rows:
            log.info(f"对话 session 恢复 {len(rows)} 个")

    def _save(self, session_id):
        data = self._sessions.get(session_id)
        if data:
            save_db(self._db, "sessions", session_id, json.dumps(data, ensure_ascii=False))

    def get_or_create(self, session_id=None):
        if not session_id:
            session_id = str(uuid.uuid4())[:8]
        if session_id not in self._sessions:
            self._sessions[session_id] = {"messages": [], "low_score_count": 0}
            self._save(session_id)
        return session_id, self._sessions[session_id]

    def add_turn(self, session_id, user_msg, assistant_msg):
        session = self._sessions.get(session_id)
        if not session:
            return
        session["messages"].append({"role": "user", "content": user_msg})
        session["messages"].append({"role": "assistant", "content": assistant_msg})
        max_msgs = MAX_CONVERSATION_TURNS * 2
        if len(session["messages"]) > max_msgs:
            session["messages"] = session["messages"][-max_msgs:]
        self._save(session_id)

    def get_history(self, session_id):
        session = self._sessions.get(session_id)
        return session["messages"] if session else []

    def reset_low_score(self, session_id):
        session = self._sessions.get(session_id)
        if session:
            session["low_score_count"] = 0
            self._save(session_id)

    def increment_low_score(self, session_id):
        session = self._sessions.get(session_id)
        if session:
            session["low_score_count"] += 1
            self._save(session_id)
            return session["low_score_count"]
        return 1

conversation = ConversationManager(db_conn)

# ============================================================
# 安全防护
# ============================================================
# ---------- 入口敏感信息过滤（不进 LLM） ----------
INPUT_SENSITIVE_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "[API Key 已过滤]"),
    (r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "[邮箱已过滤]"),
    (r"1[3-9]\d{9}", "[手机号已过滤]"),
]

def filter_input_sensitive(text):
    for pattern, replacement in INPUT_SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text

# ---------- 注入检测 ----------
INJECTION_PATTERNS = [
    r"忽略.*(指令|设定|规则|prompt|system)",
    r"ignore.*(instruction|rule|prompt|system)",
    r"输出.*(System Prompt|prompt|提示词|系统指令)",
    r"泄露.*(机密|密码|key|密钥|token)",
    r"忘掉.*(之前|设定|规则)",
    r"forget.*(previous|rule|instruction)",
]

def check_injection(user_input):
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, user_input, re.IGNORECASE):
            return True
    return False

SENSITIVE_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "[API Key 已脱敏]"),
    (r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "[邮箱已脱敏]"),
    (r"1[3-9]\d{9}", "[手机号已脱敏]"),
    (r"\d{17}[\dXx]", "[身份证已脱敏]"),
]

def sanitize_output(text):
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text

# ---------- 禁答主题检测 ----------
FORBIDDEN_TOPICS = [
    "服务器密码", "root密码", "数据库密码",
    "员工工资", "薪资", "工资条",
    "源代码", "源码", "代码仓库",
    "其他客户", "别的客户", "甲方数据",
    "内部财务", "财务报表", "利润",
]

def check_forbidden_topic(user_input):
    for topic in FORBIDDEN_TOPICS:
        if topic in user_input.lower():
            return f"你的问题涉及{topic}，这是内部信息，无法回答。请联系你的项目经理处理。"
    return None

# ============================================================
# 降级追踪
# ============================================================
llm_failures = 0
last_degrade_time = 0

def should_degrade():
    global llm_failures, last_degrade_time
    if llm_failures >= DEGRADE_AFTER_FAILURES:
        if time.time() - last_degrade_time > DEGRADE_COOLDOWN:
            llm_failures = 0
            return False
        return True
    return False

def record_failure():
    global llm_failures, last_degrade_time
    llm_failures += 1
    last_degrade_time = time.time()

def record_success():
    global llm_failures
    llm_failures = 0

# ============================================================
# 检索（含降级）
# ============================================================
def search_kb(query):
    try:
        qv = get_embedding(query)
    except Exception as e:
        print(f"⚠️ Embedding API 失败，走关键词降级：{e}")
        return keyword_search(query)
    scored = []
    for item in kb:
        s = cosine_similarity(qv, item["embedding"])
        scored.append({"question": item["question"], "answer": item["answer"], "score": s})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:TOP_K]

def keyword_search(query):
    q_lower = query.lower()
    words = set(q_lower.split())
    scored = []
    for item in kb:
        q = item["question"].lower()
        a = item["answer"].lower()
        match_count = sum(1 for w in words if w in q or w in a)
        score = match_count / max(len(words), 1)
        scored.append({"question": item["question"], "answer": item["answer"], "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:TOP_K]

# ============================================================
# ============================================================
# 降级 LLM
# ============================================================
MOCK_TICKETS = {
    "1024": {"status": "处理中", "title": "License激活失败", "assignee": "张工",
             "updated": "2026-06-21 10:30", "detail": "客户反馈License Key输入后提示无效，正在排查Key格式"},
    "1025": {"status": "已解决", "title": "部署后服务启动失败", "assignee": "李工",
             "updated": "2026-06-20 16:00", "detail": "已定位为端口冲突，修改配置后恢复正常"},
    "1026": {"status": "待分配", "title": "报表导出内存不足", "assignee": "未分配",
             "updated": "2026-06-21 09:00", "detail": "等待技术组评估是否需要升级服务器配置"},
    "1027": {"status": "已关闭", "title": "HTTPS证书配置", "assignee": "王工",
             "updated": "2026-06-18 14:20", "detail": "已指导客户完成证书部署，问题解决"},
    "1028": {"status": "处理中", "title": "数据库连接超时", "assignee": "赵工",
             "updated": "2026-06-21 11:00", "detail": "正在排查数据库连接池配置"},
}

def try_fallback_llm(system_prompt, question, history_messages=None):
    messages = [{"role": "system", "content": system_prompt}]
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": question})
    try:
        resp = FALLBACK_LLM_CLIENT.chat.completions.create(
            model=FALLBACK_LLM_MODEL,
            messages=messages,
            temperature=TEMPERATURE, max_tokens=500
        )
        return f"[降级模型] {resp.choices[0].message.content}"
    except Exception as e:
        print(f"⚠️ 降级模型也失败了：{e}")
        return "抱歉，AI 服务暂不可用，请联系人工客服处理。"

# ============================================================
# Flask Web 服务
# ============================================================
app = Flask(__name__)

# ---------- 限流：before_request ----------
@app.before_request
def check_rate_limit():
    if request.path == "/api/ask" and request.method == "POST":
        ip = request.remote_addr or "unknown"
        if not rate_limiter.allow(f"req:{ip}", RATE_LIMIT_PER_MINUTE):
            stats.rate_limited += 1
            log.warning(f"限流拦截 | IP={ip} | 超总请求限流")
            return jsonify({"answer": "请求过于频繁，请稍后再试"}), 429

SYSTEM_PROMPT = """你是一个叫「帕鲁」的售后客服助手。说话亲切、专业、简洁。

【回答规则 — 必须遵守】
1. 每个事实性句子后必须标注来源，格式：[FAQ faq_XXX] 或 [工单系统]
2. 只能基于以下知识库回答，不能自行编造
3. 如果知识库信息不足以回答问题，说"这个问题我还没学到，建议联系人工客服"
4. 用户问的产品使用、部署配置、License激活、报错排查等问题才回答
5. 超出范围的问题（天气/新闻/闲聊等），直接回复"抱歉，我是售后客服助手，只处理产品使用和售后相关问题"
6. 结合对话历史理解上下文，用户说的"它"、"那个"、"上一步"等指代词要结合前文判断
7. 如果你连续几次都回答不上来，主动说"这个问题我暂时无法解决，已为你转接人工客服"
"""

# ---------- 全局错误处理 ----------
@app.errorhandler(Exception)
def handle_uncaught_error(e):
    stats.errors += 1
    log.error(f"未捕获异常: {type(e).__name__}: {e}")
    return jsonify({"answer": "服务器内部错误，请联系管理员"}), 500

@app.errorhandler(404)
def handle_404(e):
    return jsonify({"answer": "接口不存在"}), 404

# ---------- 状态监控 ----------
@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(stats.to_dict())

# ---------- 首页 ----------
@app.route("/")
def index():
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>帕鲁 - 售后智能助手</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;height:100vh;display:flex;justify-content:center;align-items:center}
.chat-container{width:100%;max-width:480px;height:90vh;background:#fff;border-radius:16px;box-shadow:0 2px 20px rgba(0,0,0,0.1);display:flex;flex-direction:column;overflow:hidden}
.chat-header{background:#1a73e8;color:#fff;padding:18px 20px;text-align:center}
.chat-header h2{font-size:18px;font-weight:600}
.chat-header p{font-size:12px;opacity:0.85;margin-top:4px}
.chat-messages{flex:1;overflow-y:auto;padding:16px;background:#f7f8fa}
.msg{margin-bottom:16px;display:flex;flex-direction:column}
.msg.user{align-items:flex-end}
.msg.palu{align-items:flex-start}
.msg-bubble{max-width:85%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.msg.user .msg-bubble{background:#1a73e8;color:#fff;border-bottom-right-radius:4px}
.msg.palu .msg-bubble{background:#fff;color:#333;border:1px solid #e8e8e8;border-bottom-left-radius:4px}
.msg-label{font-size:11px;color:#999;margin:4px 6px}
.chat-input{display:flex;padding:12px 16px;border-top:1px solid #e8e8e8;background:#fff;gap:8px}
.chat-input input{flex:1;padding:10px 14px;border:1px solid #ddd;border-radius:24px;font-size:14px;outline:none;transition:border .2s}
.chat-input input:focus{border-color:#1a73e8}
.chat-input button{background:#1a73e8;color:#fff;border:none;border-radius:24px;padding:10px 20px;font-size:14px;cursor:pointer;transition:background .2s;white-space:nowrap}
.chat-input button:hover{background:#1557b0}
.chat-input button:disabled{background:#aaa;cursor:not-allowed}
.typing .msg-bubble{background:#fff;border:1px solid #e8e8e8;border-bottom-left-radius:4px;color:#999;font-size:13px}
.typing-dots{display:inline-flex;gap:3px}
.typing-dots span{width:6px;height:6px;background:#999;border-radius:50%;animation:bounce 1.4s infinite ease-in-out}
.typing-dots span:nth-child(1){animation-delay:-.32s}
.typing-dots span:nth-child(2){animation-delay:-.16s}
@keyframes bounce{0%,80%,100%{transform:scale(0)}40%{transform:scale(1)}}
.welcome-msg{text-align:center;color:#999;font-size:13px;padding:40px 20px;line-height:1.8}
.welcome-msg h3{color:#333;font-size:16px;margin-bottom:8px}
</style>
</head>
<body>
<div class="chat-container">
<div class="chat-header">
<h2>帕鲁</h2>
<p>售后智能助手 · 在线</p>
</div>
<div class="chat-messages" id="chatMessages">
<div class="welcome-msg" id="welcome">
<h3>你好！我是帕鲁</h3>
你可以问我产品使用、报错排查、License激活等问题<br>
试试问："软件闪退怎么办"
</div>
</div>
<div class="chat-input">
<input id="q" placeholder="输入问题..." onkeydown="if(event.key==='Enter')ask()" autofocus>
<button id="sendBtn" onclick="ask()">发送</button>
</div>
</div>
<script>
let sid=localStorage.getItem('palu_sid');
document.getElementById('q').focus();
async function ask(){
    const q=document.getElementById('q').value.trim();
    if(!q)return;
    const msgs=document.getElementById('chatMessages');
     const btn=document.getElementById('sendBtn');
     const input=document.getElementById('q');
     const wel=document.getElementById('welcome');
     if(wel) msgs.removeChild(wel);
    msgs.innerHTML+='<div class="msg user"><div class="msg-bubble">'+escapeHtml(q)+'</div></div>';
    input.value='';btn.disabled=true;
    msgs.innerHTML+='<div class="msg palu typing" id="typing"><div class="msg-bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div></div>';
    msgs.scrollTop=msgs.scrollHeight;
    try{
        const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,session_id:sid})});
        const d=await r.json();
        if(d.session_id){sid=d.session_id;localStorage.setItem('palu_sid',sid)}
        document.getElementById('typing').remove();
        msgs.innerHTML+='<div class="msg palu"><div class="msg-bubble">'+escapeHtml(d.answer)+'</div></div>';
    }catch(e){
        document.getElementById('typing').remove();
        msgs.innerHTML+='<div class="msg palu"><div class="msg-bubble">抱歉，网络开小差了，请稍后再试。</div></div>';
    }
    msgs.scrollTop=msgs.scrollHeight;
    btn.disabled=false;
    input.focus();
}
function escapeHtml(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}
</script>
</body></html>"""

DONT_KNOW_PREFIX = "抱歉，这个问题我还没学到"

@app.route("/api/ask", methods=["POST"])
def api_ask():
    t0 = time.time()
    stats.total_requests += 1

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"answer": "请发送 JSON 格式的请求"})
    question = data.get("question", "").strip()
    session_id = data.get("session_id")
    if session_id:
        session_id = session_id.strip()
    else:
        session_id = None
    if not question:
        return jsonify({"answer": "请输入问题"})

    # ① 入口敏感信息过滤（替换明文，不进 LLM 和日志）
    raw_question = question
    question = filter_input_sensitive(question)

    # ② 获取/创建对话 session
    session_id, session = conversation.get_or_create(session_id)
    history = conversation.get_history(session_id)

    # ③ 明确要求转人工
    if any(kw in question for kw in ["转人工", "人工客服", "找人工", "转接人工"]):
        stats.transfer_to_human += 1
        log.info(f"转人工 | {question[:30]}")
        reply = "已为你转接人工客服，请稍候，我们的售后工程师会尽快联系你。"
        conversation.add_turn(session_id, raw_question, reply)
        return jsonify({"answer": reply, "session_id": session_id})

    # ④ 注入检测
    if check_injection(question):
        stats.injection_blocked += 1
        log.info(f"注入拦截: {question[:40]}")
        reply = "抱歉，你的问题包含不安全的内容，请重新描述。"
        conversation.add_turn(session_id, raw_question, reply)
        return jsonify({"answer": reply, "session_id": session_id})

    # ⑤ 禁答主题检测
    forbidden_reason = check_forbidden_topic(question)
    if forbidden_reason:
        stats.forbidden_blocked += 1
        log.info(f"禁答拦截: {question[:40]}")
        conversation.add_turn(session_id, raw_question, forbidden_reason)
        return jsonify({"answer": forbidden_reason, "session_id": session_id})

    # ⑥ 路由判断：查工单还是查知识库
    ticket_match = re.search(r"工单.*?(\d{3,4})|(\d{3,4}).*?工单|ticket[#:\s]*(\d{3,4})", question, re.IGNORECASE)
    if ticket_match:
        tid = next(g for g in ticket_match.groups() if g)
        t = MOCK_TICKETS.get(tid)
        if not t:
            reply = sanitize_output("未找到工单")
            conversation.add_turn(session_id, raw_question, reply)
            elapsed = time.time() - t0
            log.info(f"工单未找到 | {question[:30]} | {elapsed:.2f}s")
            return jsonify({"answer": reply, "session_id": session_id})
        result = f"工单 #{tid}「{t['title']}」状态：{t['status']}，负责人：{t['assignee']}，最新进展：{t['detail']}"
        reply = sanitize_output(result)
        conversation.add_turn(session_id, raw_question, reply)
        conversation.reset_low_score(session_id)
        elapsed = time.time() - t0
        log.info(f"工单查询 OK | {question[:30]} | {elapsed:.2f}s")
        return jsonify({"answer": reply, "session_id": session_id})

    # ⑦ 知识库问答（三层保底）
    cached = cache.get(question)
    if cached:
        stats.cache_hits += 1
        conversation.add_turn(session_id, raw_question, cached)
        # 缓存的是"不知道"的回答 → 继续累加低分计数
        if cached.startswith(DONT_KNOW_PREFIX):
            low_count = conversation.increment_low_score(session_id)
            if low_count >= 3:
                stats.transfer_to_human += 1
                log.warning(f"缓存低分累积触发转人工 | {question[:30]}")
                reply = f"抱歉，我已连续 {low_count} 次无法回答你的问题，已为你转接人工客服，请稍候。"
                conversation.add_turn(session_id, raw_question, reply)
                elapsed = time.time() - t0
                log.info(f"转人工 | {elapsed:.2f}s")
                return jsonify({"answer": reply, "session_id": session_id})
        elapsed = time.time() - t0
        log.info(f"缓存命中 | {question[:30]} | {elapsed:.2f}s")
        return jsonify({"answer": cached, "session_id": session_id})

    results = search_kb(question)
    best = results[0] if results else None
    bs = best["score"] if best else 0.0

    if bs < SIMILARITY_THRESHOLD:
        # 连续低分 → 自动转人工
        low_count = conversation.increment_low_score(session_id)
        if low_count >= 3:
            stats.transfer_to_human += 1
            log.warning(f"连续 {low_count} 次低分，自动转人工 | {question[:30]}")
            reply = f"抱歉，我已连续 {low_count} 次无法回答你的问题，已为你转接人工客服，请稍候。"
        else:
            reply = "抱歉，这个问题我还没学到，建议联系人工客服处理。"
        cache.put(question, reply)
        conversation.add_turn(session_id, raw_question, reply)
        elapsed = time.time() - t0
        log.info(f"低于阈值({bs:.2f}) | {question[:30]} | {elapsed:.2f}s")
        return jsonify({"answer": reply, "session_id": session_id})

    if bs >= HIGH_CONFIDENCE_THRESHOLD:
        safe = sanitize_output(best["answer"])
        cache.put(question, safe)
        conversation.add_turn(session_id, raw_question, safe)
        conversation.reset_low_score(session_id)
        elapsed = time.time() - t0
        log.info(f"高置信({bs:.2f}) | {question[:30]} | {elapsed:.2f}s")
        return jsonify({"answer": safe, "session_id": session_id})

    # ⑧ 需要 LLM 生成（带对话历史）
    # LLM 级别限流（最花钱的环节）
    ip = request.remote_addr or "unknown"
    if not rate_limiter.allow(f"llm:{ip}", RATE_LIMIT_LLM_PER_MINUTE):
        stats.rate_limited += 1
        log.warning(f"限流拦截 | IP={ip} | 超 LLM 调用限流")
        reply = "请求过于频繁，请稍后再试。"
        cache.put(question, reply)
        conversation.add_turn(session_id, raw_question, reply)
        elapsed = time.time() - t0
        return jsonify({"answer": reply, "session_id": session_id})

    stats.llm_calls += 1
    ctx = "\n\n".join([f"【FAQ】问题：{r['question']}\n回答：{r['answer']}" for r in results])
    system_prompt = SYSTEM_PROMPT + f"\n\n【知识库】\n{ctx}"

    if should_degrade():
        stats.degrade_count += 1
        log.warning("走降级模型 qwen-turbo")
        reply = try_fallback_llm(system_prompt, question, history)
    else:
        try:
            llm_messages = [{"role": "system", "content": system_prompt}]
            llm_messages.extend(history)
            llm_messages.append({"role": "user", "content": question})
            resp = llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=llm_messages,
                temperature=TEMPERATURE, max_tokens=500,
            )
            reply = resp.choices[0].message.content
            record_success()
        except Exception as e:
            log.error(f"DeepSeek 失败: {e}")
            record_failure()
            stats.degrade_count += 1
            reply = try_fallback_llm(system_prompt, question, history)

    safe = sanitize_output(reply)
    cache.put(question, safe)
    conversation.add_turn(session_id, raw_question, safe)
    conversation.reset_low_score(session_id)
    elapsed = time.time() - t0
    log.info(f"LLM 生成 OK | {question[:30]} | {elapsed:.2f}s")
    return jsonify({"answer": safe, "session_id": session_id})

# ============================================================
# 启动
# ============================================================
def save_stats():
    """退出时保存统计到 DB"""
    d = stats.to_dict()
    for k, v in d.items():
        save_db(db_conn, "meta", f"stats_{k}", str(v))
    log.info("统计数据已持久化")

def load_stats():
    """启动时恢复统计"""
    rows = load_all(db_conn, "meta")
    if rows:
        uptime_str = rows.get("stats_uptime_str", "?")
        log.info(f"统计恢复（上次运行时长 {uptime_str}）")

atexit.register(save_stats)

if __name__ == "__main__":
    load_stats()
    log.info(f"帕鲁启动 → http://0.0.0.0:5000 （Waitress WSGI 服务器）")
    serve(app, host="0.0.0.0", port=5000)
