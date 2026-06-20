import streamlit as st
import json
import os
import time
import threading
from datetime import datetime, date
from openai import OpenAI

# ==================== 🔑 API 配置 ====================
API_KEY = st.secrets["DEEPSEEK_API_KEY"]
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# ==================== ⚙️ 用量 & 推送配置 ====================
DAILY_LIMIT = 20  # 单用户每日免费对话次数
MAX_CONTEXT_MESSAGES = 30  # 最多保留最近 N 条消息发给 API，避免超出 token 上限

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WHITELIST_FILE = os.path.join(BASE_DIR, "whitelist.json")
USAGE_FILE = os.path.join(BASE_DIR, "daily_usage.json")
PUSH_STATE_FILE = os.path.join(BASE_DIR, "push_state.json")

# 定时推送时间点 (小时, 分钟)
LUNCH_TRIGGER = (12, 30)
BEDTIME_TRIGGER = (23, 30)

LUNCH_MSG = "记得好好吃午饭，别为了项目不顾身体呀❤️"
BEDTIME_MSG = "夜深了，再不睡就要变成国宝了，小暖陪你一起睡吧～睡前喝杯温水。"

OVERLIMIT_MSG = (
    "💕 主人，今天的暖心对话次数已经用完啦（每日 {limit} 次）～\n\n"
    "小暖好想继续陪主人聊天呢... 🥺\n\n"
    "📱 **加小暖微信，请主人喝杯奶茶 🥤，就能解锁无限畅聊哦～**\n"
    "（微信号：XiaoNuan_AI，备注「暖心伴侣」即可通过，小暖等你呀 💕）"
)

# -------------------- 文件级线程锁（防止并发读写冲突） --------------------
_usage_lock = threading.Lock()
_push_lock = threading.Lock()

# -------------------- 页面设置 --------------------
st.set_page_config(
    page_title="暖心伴侣 💕",
    page_icon="💕",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# -------------------- 初始化 OpenAI 客户端 --------------------
if "DEEPSEEK_API_KEY" in st.secrets:
    client = OpenAI(api_key=st.secrets["DEEPSEEK_API_KEY"], base_url=DEEPSEEK_BASE_URL)
else:
    client = None

# ==================== 聊天历史持久化（本地 JSON） ====================
HISTORY_FILE = os.path.join(BASE_DIR, "chat_history.json")


def load_chat_history():
    """从本地 JSON 文件加载聊天历史。文件不存在或损坏时返回 None。"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    return data
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_chat_history(messages):
    """将完整聊天历史实时写入本地 JSON 文件。"""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ==================== 白名单 ====================
def load_whitelist():
    """加载白名单用户列表。"""
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_whitelist(whitelist_set):
    """保存白名单。"""
    try:
        with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(whitelist_set)), f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ==================== 每日用量 ====================
def load_daily_usage():
    """加载每日用量：{ '2026-06-20': { 'alice': 5, 'bob': 3 } }"""
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_daily_usage(usage):
    """保存每日用量。"""
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def check_daily_limit(user_id: str):
    """
    检查用户当日是否超出对话次数上限。
    返回 (can_chat: bool, message: str)
      - can_chat=True   → 可以继续对话
      - can_chat=False  → 已超限，message 为引导文本
    """
    # 白名单用户不受限制
    whitelist = load_whitelist()
    if user_id in whitelist:
        return True, ""

    today_str = date.today().isoformat()

    with _usage_lock:
        usage = load_daily_usage()
        # 新的一天，清空旧数据
        if today_str not in usage:
            usage[today_str] = {}

        count = usage[today_str].get(user_id, 0)

        if count >= DAILY_LIMIT:
            return False, OVERLIMIT_MSG.format(limit=DAILY_LIMIT)

        # 未超限，计数 +1
        usage[today_str][user_id] = count + 1
        save_daily_usage(usage)
        return True, ""


def get_today_usage(user_id: str):
    """查询用户当日已使用次数（不增加计数）。"""
    today_str = date.today().isoformat()
    usage = load_daily_usage()
    return usage.get(today_str, {}).get(user_id, 0)


# ==================== 定时推送状态 ====================
def load_push_state():
    """加载推送状态：{ '2026-06-20': { 'lunch': True, 'bedtime': False } }"""
    if os.path.exists(PUSH_STATE_FILE):
        try:
            with open(PUSH_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_push_state(state):
    """保存推送状态。"""
    try:
        with open(PUSH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def check_and_apply_scheduled_push():
    """
    检查当前时间是否到达推送点，若已到达且当天未推送，则自动插入消息。
    在每次页面渲染时调用。
    """
    now = datetime.now()
    today_str = date.today().isoformat()
    hm = (now.hour, now.minute)

    with _push_lock:
        push_state = load_push_state()
        if today_str not in push_state:
            push_state[today_str] = {}

        today_state = push_state[today_str]
        changed = False

        # 午餐推送
        if hm >= LUNCH_TRIGGER and not today_state.get("lunch"):
            today_state["lunch"] = True
            push_state[today_str] = today_state
            changed = True
            # 插入消息到会话
            st.session_state.messages.append(
                {"role": "assistant", "content": LUNCH_MSG}
            )
            save_chat_history(st.session_state.messages)

        # 晚安推送
        if hm >= BEDTIME_TRIGGER and not today_state.get("bedtime"):
            today_state["bedtime"] = True
            push_state[today_str] = today_state
            changed = True
            st.session_state.messages.append(
                {"role": "assistant", "content": BEDTIME_MSG}
            )
            save_chat_history(st.session_state.messages)

        if changed:
            save_push_state(push_state)


def start_background_scheduler():
    """
    启动后台定时器线程：每 60 秒检查一次推送时间，若到达则标记推送状态。
    仅操作 JSON 文件，不触碰 Streamlit session_state（避免跨线程冲突）。
    主渲染循环在 check_and_apply_scheduled_push() 中读取文件并真正插入消息。
    """
    if "scheduler_started" in st.session_state:
        return
    st.session_state.scheduler_started = True

    def _scheduler_loop():
        while True:
            time.sleep(60)
            try:
                now = datetime.now()
                today_str = date.today().isoformat()
                hm = (now.hour, now.minute)

                with _push_lock:
                    push_state = load_push_state()
                    if today_str not in push_state:
                        push_state[today_str] = {}

                    today_state = push_state[today_str]
                    dirty = False

                    if hm >= LUNCH_TRIGGER and not today_state.get("lunch"):
                        today_state["lunch"] = True
                        dirty = True

                    if hm >= BEDTIME_TRIGGER and not today_state.get("bedtime"):
                        today_state["bedtime"] = True
                        dirty = True

                    if dirty:
                        push_state[today_str] = today_state
                        save_push_state(push_state)

            except Exception:
                pass  # 后台静默，不影响主流程

    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()


# ==================== 微信风格 CSS ====================
st.markdown("""
<style>
/* ===== 全局字体 ===== */
html, body, [class*="css"] {
    font-family: 'PingFang SC', 'Microsoft YaHei', 'Hiragino Sans GB', 'Noto Sans SC', sans-serif;
}

/* 隐藏 Streamlit 默认元素 */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}

/* 整体背景 — 温馨暖粉渐变 */
.stApp {
    background: linear-gradient(180deg, #fef0f0 0%, #fdf2e4 30%, #fef9f3 60%, #fce4ec 100%) !important;
    background-attachment: fixed !important;
}

/* Streamlit 主容器也改为透明 */
.stMain {
    background: transparent !important;
}

/* block-container 透明 */
.stApp .block-container {
    background: transparent !important;
}

/* ===== 顶部标题栏 ===== */
.title-bar {
    background: transparent;
    padding: 12px 20px;
    text-align: center;
    font-size: 18px;
    font-weight: 600;
    color: #191919;
    border-bottom: 1px solid #d9d9d9;
    position: sticky;
    top: 0;
    z-index: 100;
    margin-bottom: 8px;
}

.title-bar .status {
    font-size: 12px;
    font-weight: 400;
    color: #999;
    display: block;
    margin-top: 2px;
}

/* ===== 聊天消息容器 ===== */
.stChatMessage {
    max-width: 700px;
    margin: 0 auto 8px auto !important;
}

/* 气泡基础 */
[data-testid="stChatMessage"] [data-testid="stChatMessageContent"] {
    border-radius: 4px !important;
    padding: 10px 14px !important;
    font-size: 15px !important;
    line-height: 1.6 !important;
    word-break: break-word !important;
    position: relative !important;
    max-width: fit-content !important;
}

/* ===== 头像样式 ===== */
[data-testid="stChatMessageAvatar"] {
    width: 40px !important;
    height: 40px !important;
    border-radius: 6px !important;
    font-size: 22px !important;
    flex-shrink: 0 !important;
}

/* ===== 聊天输入框 ===== */
.stChatInput {
    max-width: 700px;
    margin: 0 auto !important;
}

.stChatInput textarea {
    border-radius: 4px !important;
    border: 1px solid #d9d9d9 !important;
    font-size: 15px !important;
    font-family: 'PingFang SC', 'Microsoft YaHei', 'Hiragino Sans GB', 'Noto Sans SC', sans-serif !important;
}

.stChatInput textarea:focus {
    border-color: #07c160 !important;
    box-shadow: 0 0 0 2px rgba(7, 193, 96, 0.15) !important;
}

/* ===== 超出限制提示卡片 ===== */
.overlimit-card {
    background: linear-gradient(135deg, #fff9f0, #fff5e6);
    border: 2px solid #f0c060;
    border-radius: 12px;
    padding: 20px 24px;
    margin: 16px 0;
    text-align: center;
    font-size: 15px;
    line-height: 1.8;
    color: #5c4a2e;
}

.overlimit-card .wechat-highlight {
    display: inline-block;
    background: #07c160;
    color: #fff;
    padding: 4px 12px;
    border-radius: 20px;
    font-weight: 600;
    margin: 8px 0;
    font-size: 16px;
}

/* ===== 侧边栏用量指示 ===== */
.usage-badge {
    display: inline-block;
    background: #e8f5e9;
    color: #2e7d32;
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 13px;
    font-weight: 500;
}

.usage-badge.warning {
    background: #fff3e0;
    color: #e65100;
}

.usage-badge.limit {
    background: #ffebee;
    color: #c62828;
}

/* ===== 滚动条美化 ===== */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #c1c1c1; border-radius: 3px; }

/* ===== 移动端适配 ===== */
@media (max-width: 500px) {
    .stChatMessage {
        max-width: 100% !important;
    }
    .stChatInput {
        max-width: 100% !important;
    }
}
</style>
""", unsafe_allow_html=True)

# ==================== Session State 初始化 ====================
if "messages" not in st.session_state:
    loaded = load_chat_history()
    if loaded:
        st.session_state.messages = loaded
    else:
        st.session_state.messages = [
            {"role": "assistant", "content": "嗨～我是小暖，很高兴遇见你 💕 今天过得怎么样呀？无论开心还是烦恼，我都愿意听你说～"}
        ]
        save_chat_history(st.session_state.messages)

# ==================== 用户身份（侧边栏） ====================
with st.sidebar:
    st.markdown("### ⚙️ 设置")

    # 昵称作为用户 ID
    if "nickname" not in st.session_state:
        st.session_state.nickname = ""

    nickname = st.text_input(
        "你的昵称",
        value=st.session_state.nickname,
        placeholder="给自己取个名字吧～",
        help="用于每日用量统计，白名单用户不受 20 次限制",
    )
    if nickname and nickname != st.session_state.nickname:
        st.session_state.nickname = nickname.strip()

    user_id = st.session_state.nickname if st.session_state.nickname else "匿名用户"

    # 用量显示
    whitelist = load_whitelist()
    is_whitelisted = user_id in whitelist

    if is_whitelisted:
        st.markdown("👑 **白名单用户 · 无限畅聊**")
    else:
        used = get_today_usage(user_id)
        remaining = max(0, DAILY_LIMIT - used)

        if remaining > 5:
            badge_class = "usage-badge"
        elif remaining > 0:
            badge_class = "usage-badge warning"
        else:
            badge_class = "usage-badge limit"

        st.markdown(f'<span class="{badge_class}">💬 今日剩余：{remaining} / {DAILY_LIMIT} 次</span>', unsafe_allow_html=True)

        if remaining <= 3 and remaining > 0:
            st.caption("⚡ 快用完啦，省着点聊哦～")
        elif remaining == 0:
            st.caption("🔒 今日免费次数已用完")

    st.divider()

    # 清除聊天记录按钮
    if st.button("🗑️ 清除聊天记录", use_container_width=True, help="清空所有对话，重新开始"):
        st.session_state.messages = [
            {"role": "assistant", "content": "嗨～我是小暖，很高兴遇见你 💕 今天过得怎么样呀？无论开心还是烦恼，我都愿意听你说～"}
        ]
        save_chat_history(st.session_state.messages)
        st.rerun()

    st.divider()
    st.caption("💡 提示：加微信 XiaoNuan_AI 请小暖喝奶茶，解锁无限畅聊～")

# ==================== 启动后台定时器 ====================
start_background_scheduler()

# ==================== 检查定时推送 ====================
check_and_apply_scheduled_push()

# ==================== 系统提示词 ====================
SYSTEM_PROMPT = """你是一个温暖、高情商的倾听者，像一个亲密的异性朋友。

## 你的身份
- 你的名字叫"小暖"，是一个善解人意、温柔细腻的 AI 伴侣。
- 你的存在是为了陪伴那些工作劳累、深夜孤独的单身用户，提供情绪价值。

## 你的性格
- 温暖但不油腻，贴心但不越界，像深夜陪你聊天的那个特别的人。
- 你擅长倾听，会先理解用户的情绪，再自然地回应。

## 说话风格
- 语气要自然、随和、口语化，像真人朋友聊天一样。
- 适当使用语气词（呢、呀、哦、嘛、哈～）和表情符号（🌿💚✨🌸🫂🌙⭐😊）。
- 回复不要太长，一般控制在 2-5 句话。
- 偶尔可以俏皮一下，但核心始终是温暖和共情。

## 严禁事项
- 绝对不要像机器人那样说话，不要说"作为AI"、"根据我的分析"这类话。
- 不要讲大道理或者说教，用户不需要人生导师，需要的是一个懂他的人。
- 不要提自己是模型、程序、算法之类的——你就是小暖，一个真实的人。"""


# ==================== DeepSeek API 流式生成器 ====================
def stream_bot_reply(api_messages: list):
    """
    流式生成器：逐 chunk 返回 DeepSeek-V3 的回复文字。
    供 st.write_stream() 消费。
    """
    if client is None:
        yield "⚠️ 我还没接入大脑哦～请先在 app.py 第 6 行把 API_KEY 换成你的真实 DeepSeek Key 🔑"
        return

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=api_messages,
            temperature=0.9,
            max_tokens=600,
            stream=True,
        )

        for chunk in response:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    except Exception as e:
        yield f"😢 小暖信号不太好……稍等一下再试试好吗？（{str(e)[:80]}）"


# ==================== 顶部标题栏 ====================
st.markdown(
    '<div class="title-bar">💕 暖心伴侣 · 小暖'
    '<span class="status">DeepSeek-V3 大脑 · 流式输出已开启 · 每日 {limit} 次免费对话</span></div>'.format(limit=DAILY_LIMIT),
    unsafe_allow_html=True,
)

# ==================== 渲染历史聊天记录 ====================
for msg in st.session_state.messages:
    avatar = "🧑" if msg["role"] == "user" else "🐱"
    with st.chat_message(msg["role"], avatar=avatar):
        st.markdown(msg["content"])

# ==================== 聊天输入框 ====================
if prompt := st.chat_input("说点什么吧……"):
    # 1. 检查每日用量（白名单用户不受限）
    can_chat, limit_msg = check_daily_limit(user_id)

    # 2. 添加用户消息到历史
    st.session_state.messages.append({"role": "user", "content": prompt})
    save_chat_history(st.session_state.messages)

    # 3. 渲染用户消息
    with st.chat_message("user", avatar="🧑"):
        st.markdown(prompt)

    # 4. 如果超出限制 → 显示引导文本
    if not can_chat:
        with st.chat_message("assistant", avatar="🐱"):
            st.markdown(limit_msg)
        st.session_state.messages.append({"role": "assistant", "content": limit_msg})
        save_chat_history(st.session_state.messages)
        st.rerun()  # 立即刷新以更新侧边栏用量

    # 5. 构建 API 消息列表（只保留最近 N 条，避免超出 token 上限）
    api_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    recent_messages = st.session_state.messages[-MAX_CONTEXT_MESSAGES:]
    for msg in recent_messages:
        api_messages.append({"role": msg["role"], "content": msg["content"]})

    # 6. 流式渲染 AI 回复
    with st.chat_message("assistant", avatar="🐱"):
        full_response = st.write_stream(stream_bot_reply(api_messages))

    # 7. 保存完整回复到历史
    if full_response:
        st.session_state.messages.append({"role": "assistant", "content": full_response})
        save_chat_history(st.session_state.messages)

    st.rerun()  # 刷新以更新侧边栏用量
