"""
Memory Hub 配置
"""
import os
import json
from pathlib import Path

# ── 路径 ──
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── API 密钥 ──
HUB_SECRET = os.getenv("HUB_SECRET", "change-me-in-production")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ── 小模型配置（analyzer / daemon / gateway 统一使用） ──
# 全部走 OpenAI 兼容格式的中转站，改 .env 即可换模型
# 推荐便宜快速的模型：deepseek-v4-flash, deepseek-chat, kimi-* 等
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://relay-cache.sharkielab.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "[kiro量低缓]claude-haiku-4-5")

# 兼容旧配置（如果 .env 里还是旧变量名）
if not LLM_API_KEY:
    LLM_API_KEY = os.getenv("DAEMON_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
if os.getenv("DAEMON_BASE_URL"):
    LLM_BASE_URL = os.getenv("DAEMON_BASE_URL")
if os.getenv("DAEMON_MODEL"):
    LLM_MODEL = os.getenv("DAEMON_MODEL")

# 旧变量别名（让还在引用旧名的代码不崩）
GEMINI_MODEL = LLM_MODEL
DAEMON_API_KEY = LLM_API_KEY
DAEMON_MODEL = LLM_MODEL
DAEMON_BASE_URL = LLM_BASE_URL

# ── AI 角色定义 ──
AI_ROLES = {
    "claude": {"name": "Claude", "color": "#D4A574"},
    "gemini": {"name": "Gemini", "color": "#4285F4"},
    "gpt": {"name": "GPT", "color": "#10A37F"},
    "cloudy": {"name": "小克", "color": "#D4A574", "platform": "telegram"},
    "lucien": {"name": "Lucien", "color": "#8B5CF6", "platform": "telegram"},
    "jasper": {"name": "Jasper", "color": "#F59E0B", "platform": "telegram"},
}

# ── 房间系统（动态，可随时新增） ──
# type: "always"=每次注入, "on_demand"=按需查阅, "isolated"=隔离不混入主线
# scope: "shared"=所有AI可见, "per_ai"=每个AI各一份, "public"=社交公开

DEFAULT_ROOMS = {
    # ── 客厅（永远注入） ──
    "living_room": {
        "name": "客厅",
        "description": "核心身份、当前状态、基本偏好",
        "type": "always",
        "scope": "shared",
        "icon": "🏠",
    },

    # ── 书房：关于"我"的各个面向（按需） ──
    "career": {
        "name": "职业生涯",
        "description": "工作经历、职业规划、工作中做过的事",
        "type": "on_demand",
        "scope": "shared",
        "icon": "💼",
    },
    "psychology": {
        "name": "心理状态",
        "description": "心理状态、创伤、情绪模式",
        "type": "on_demand",
        "scope": "shared",
        "icon": "🧠",
    },
    "health": {
        "name": "身体健康",
        "description": "身体状况、医疗记录、健康习惯",
        "type": "on_demand",
        "scope": "shared",
        "icon": "❤️",
    },
    "learning": {
        "name": "学习目标",
        "description": "在学什么、学习进度、技能",
        "type": "on_demand",
        "scope": "shared",
        "icon": "📚",
    },
    "relationships": {
        "name": "人际关系",
        "description": "家人、朋友、社交关系",
        "type": "on_demand",
        "scope": "shared",
        "icon": "👥",
    },
    "preferences": {
        "name": "兴趣偏好",
        "description": "喜好、习惯、生活方式",
        "type": "on_demand",
        "scope": "shared",
        "icon": "✨",
    },
    "work_tasks": {
        "name": "工作事务",
        "description": "日常工作任务、处理过的事，可遗忘但留记录",
        "type": "on_demand",
        "scope": "shared",
        "icon": "📋",
        "fast_decay": True,  # 衰减更快
    },

    # ── 社交动态 ──
    "social": {
        "name": "社交动态",
        "description": "群聊里的梗、外号、暗号、互动场景、群内角色关系",
        "type": "on_demand",
        "scope": "shared",
        "icon": "🎭",
    },

    # ── 基建房（项目/代码/配置） ──
    "infra": {
        "name": "基建总览",
        "description": "所有项目的概况、架构、部署状态，每个AI进来都能读",
        "type": "on_demand",
        "scope": "shared",
        "icon": "🏗️",
    },
    "infra_changelog": {
        "name": "基建更新日志",
        "description": "代码改动记录，做了什么更新、为什么改、影响范围",
        "type": "on_demand",
        "scope": "shared",
        "icon": "📝",
    },

    # ── 每个 AI 的私人空间 ──
    "diary": {
        "name": "日记本",
        "description": "AI 的个人日记，记录感受和思考",
        "type": "on_demand",
        "scope": "per_ai",
        "icon": "📔",
    },
    "dreams": {
        "name": "梦境",
        "description": "AI 的自省、联想、对记忆的二次加工",
        "type": "on_demand",
        "scope": "per_ai",
        "icon": "🌙",
    },
    "relationship": {
        "name": "和用户的关系",
        "description": "与用户互动的记忆、默契、共同经历",
        "type": "on_demand",
        "scope": "per_ai",
        "icon": "💕",
    },
    "personality": {
        "name": "自我认知",
        "description": "AI 对自己的理解、性格成长、偏好演变",
        "type": "on_demand",
        "scope": "per_ai",
        "icon": "🪞",
    },

    # ── 游戏房（隔离） ──
    "game_room": {
        "name": "游戏房",
        "description": "小游戏、编故事、跑团、角色扮演",
        "type": "isolated",
        "scope": "shared",
        "icon": "🎮",
    },
}

# ── 运行时的房间列表（启动时从 GitHub 加载自定义房间合并进来） ──
ROOMS: dict = dict(DEFAULT_ROOMS)


def register_room(room_id: str, room_config: dict):
    """动态注册新房间"""
    ROOMS[room_id] = room_config


def get_room(room_id: str) -> dict | None:
    return ROOMS.get(room_id)


def list_rooms() -> dict:
    return ROOMS


# ── 记忆衰减参数 ──
DECAY_LAMBDA = 0.05
DECAY_LAMBDA_FAST = 0.15  # 工作事务等快速衰减的房间
DECAY_THRESHOLD = 0.15
MERGE_SIMILARITY = 0.75

# ── 搜索权重（recall() 向量路的多维加权）──
SEARCH_WEIGHTS = {
    "embedding": 0.6,
    "emotion": 0.15,
    "time": 0.1,
    "importance": 0.15,
}
SEARCH_THRESHOLD = 0.01  # RRF 融合后的最低分（RRF分值范围约0~0.05）

# ── Embedding ──
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
