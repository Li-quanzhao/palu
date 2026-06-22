"""
钉钉机器人回调模块 — 让帕鲁接入钉钉群聊
用户在群里 @帕鲁 提问 → 帕鲁自动回复

架构：
  DingTalk Server → POST /dingtalk/callback → 验证签名 → 调帕鲁核心逻辑 → 返回回复

使用方式：
  1. 在 app.py 中 import 并注册：
       from dingtalk_bot import dingtalk_bp, init_dingtalk_bot
       init_dingtalk_bot(process_question)  # process_question 是帕鲁核心函数
       app.register_blueprint(dingtalk_bp)
  2. 在 .env 中配置：
       DINGTALK_APP_KEY=xxx
       DINGTALK_APP_SECRET=xxx
       DINGTALK_CALLBACK_KEY=xxx  # 钉钉后台配置的加签 Key
"""

import os
import re
import json
import time
import hashlib
import hmac
import base64
import logging
from flask import Blueprint, request, jsonify

log = logging.getLogger("werkzeug")

# ============================================================
# 蓝图定义
# ============================================================
dingtalk_bp = Blueprint("dingtalk", __name__)

# 核心处理函数（由 init_dingtalk_bot 注入）
_ask_func = None

def init_dingtalk_bot(ask_func):
    """注入帕鲁核心问答函数"""
    global _ask_func
    _ask_func = ask_func
    log.info("钉钉机器人模块已初始化")

# ============================================================
# 钉钉回调签名验证
# ============================================================
def verify_signature(timestamp, signature):
    """
    验证钉钉回调签名
    钉钉用 AppSecret 做 HMAC-SHA256 加签
    """
    # 【参数可调】从环境变量读取
    app_secret = os.getenv("DINGTALK_APP_SECRET", "")
    if not app_secret:
        log.warning("未配置 DINGTALK_APP_SECRET，跳过签名验证（不安全！）")
        return True

    # 钉钉签名算法：base64(hmac-sha256(app_secret, timestamp + "\n" + app_secret))
    string_to_sign = f"{timestamp}\n{app_secret}"
    expected = base64.b64encode(
        hmac.new(
            app_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()
    ).decode("utf-8")
    return signature == expected


# ============================================================
# 钉钉消息回调入口
# ============================================================
@dingtalk_bp.route("/dingtalk/callback", methods=["POST"])
def dingtalk_callback():
    """
    钉钉消息回调入口
    文档：https://open.dingtalk.com/document/orgapp/robot-message-receiving-protocol
    
    钉钉在群聊 @机器人 或私聊发消息时，会 POST 到这个地址。
    需要在 5 秒内返回响应。
    """
    # ① 获取请求头和参数
    timestamp = request.headers.get("timestamp", "")
    signature = request.headers.get("sign", "")

    # ①.5 签名验证
    if not verify_signature(timestamp, signature):
        log.warning(f"钉钉签名验证失败 | timestamp={timestamp}")
        return jsonify({"msg": "signature verification failed"}), 403

    # ② 解析消息
    data = request.get_json(silent=True) or {}
    log.info(f"钉钉消息 | {json.dumps(data, ensure_ascii=False)[:300]}")

    # ②.5 钉钉地址验证回调（必处理，否则注册失败）
    # 【逻辑说明】钉钉保存回调地址时会发一个验证请求，需要原样返回 challenge
    if "challenge" in data or data.get("msgtype") == "url_verification":
        challenge = data.get("challenge", "ok")
        log.info(f"钉钉地址验证通过")
        return jsonify({"challenge": challenge})

    # ③ 只处理文本消息
    msgtype = data.get("msgtype", "")
    if msgtype != "text":
        return jsonify({
            "msgtype": "text",
            "text": {"content": "帕鲁目前只支持文字消息"}
        })

    # ④ 提取消息内容 + 去掉 @机器人的部分
    text_content = data.get("text", {}).get("content", "").strip()

    # 【逻辑说明】钉钉群聊中 @机器人 时，文本内容会带上 "@机器人名 "
    # 例如 "@帕鲁 软件闪退怎么办" → 去掉 "@帕鲁 " → "软件闪退怎么办"
    text_content = re.sub(r'@[\u4e00-\u9fa5a-zA-Z0-9_]+\s*', '', text_content, count=1).strip()
    if not text_content:
        return jsonify({
            "msgtype": "text",
            "text": {"content": "请输入问题，例如：软件闪退怎么办"}
        })

    # ⑤ 构造 session_id（基于钉钉会话 ID，保持多轮对话上下文）
    # 【逻辑说明】群聊里所有人在同一个 conversationId，所以同一个群的上下文是共享的
    conversation_id = data.get("conversationId", "")
    sender_nick = data.get("senderNick", "未知用户")
    session_id = f"dingtalk_{conversation_id}" if conversation_id else None

    # ⑥ 调帕鲁核心逻辑
    if _ask_func is None:
        log.error("钉钉机器人未初始化：未调用 init_dingtalk_bot 注入核心函数")
        return jsonify({
            "msgtype": "text",
            "text": {"content": "帕鲁暂时无法回答问题，请联系管理员"}
        })

    try:
        # 调帕鲁的问答函数，传 question 和 session_id
        # 【参数可调】client_ip 按 conversationId 隔离限流
        # 不同群聊有不同 conversationId，一个群刷屏不影响另一个群
        answer, _ = _ask_func(text_content, session_id, client_ip=f"dingtalk_{conversation_id}" if conversation_id else "dingtalk")
    except Exception as e:
        log.error(f"钉钉回调处理异常: {e}")
        answer = "帕鲁暂时无法回答，请稍后再试或联系人工客服。"

    # ⑦ 追加反馈提示文字
    # 【逻辑说明】钉钉没有交互按钮，用文字提示用户回复赞/踩
    feedback_prompt = "\n\n---\n觉得有帮助回复：赞 👍\n没帮助回复：踩 👎"
    answer_with_prompt = answer + feedback_prompt

    # ⑧ 返回回复（钉钉会投递到群里）
    # 【逻辑说明】钉钉回调的返回值就是机器人的回复内容
    # 消息长度限制 2000 字符（钉钉限制）
    return jsonify({
        "msgtype": "text",
        "text": {
            "content": answer_with_prompt[:2000]
        }
    })
