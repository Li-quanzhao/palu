"""
帕鲁 v1 — 让帕鲁开口说话
【概念演示】Day 1 最小可行版本
"""

# ============================================
# 第一步：拿工具箱
# ============================================
# 【逻辑说明】从环境变量文件 (.env) 读 API Key
import os
from dotenv import load_dotenv
load_dotenv()

# 【逻辑说明】DeepSeek 和 OpenAI 的接口是兼容的
# 所以可以用 OpenAI 的 Python 库来调 DeepSeek
from openai import OpenAI

# ============================================
# 第二步：搭桥 — 连上 DeepSeek 的服务器
# ============================================
# 【逻辑说明】创建一个"客户端"对象，用它来和 AI 服务器对话
# api_key：你的"通行证"，从 .env 文件里读出来
# base_url：DeepSeek 服务器的地址
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

# ============================================
# 第三步：写 Prompt — 告诉帕鲁它的身份
# ============================================
# 【逻辑说明】System = 给 AI 设定角色和规矩
# User = 你问的问题
# 帕鲁是公司售后群里的 AI 助手，所以用售后客服的口吻
system_prompt = "你是一个叫「帕鲁」的售后客服助手。说话亲切、专业、简洁。回答不超过 3 句话。"

# ============================================
# 第四步：问问题 & 拿回答
# ============================================
def ask_palu(user_question):
    """问帕鲁一个问题，拿到回答"""
    
    # 调 AI 接口发消息
    # 【参数可调】temperature=0.3 → 回答更稳定、更准确
    # 调高 (0.7+) → 回答更多样化但有编造风险
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_question}
        ],
        temperature=0.2,      # ← 调这个：0.0~0.3 稳定 / 0.7~0.9 有创意
        max_tokens=200        # ← 调这个：回答最长多少个字
    )

    # 【逻辑说明】从返回结果里把文字提取出来
    # response 是一个"快递箱"，一层层拆开才能拿到里面的文字
    reply = response.choices[0].message.content
    return reply


# ============================================
# 第五步：跑起来 — 在终端里对话
# ============================================
if __name__ == "__main__":
    print("🐤 帕鲁已上线！输入问题，帕鲁来答（输入 quit 退出）\n")
    
    while True:
        # input() 是 Python 自带功能 — 在终端等你打字
        user_input = input("你：")
        
        if user_input.lower() == "quit":
            print("帕鲁：拜拜~")
            break
        
        reply = ask_palu(user_input)
        print(f"帕鲁：{reply}\n")
