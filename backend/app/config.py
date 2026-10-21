"""运行配置：全部来自环境变量（可放在项目根目录 .env 中），带安全的默认值。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # combine/
FRONTEND_DIR = ROOT / "frontend"


def _load_dotenv() -> None:
    """极简 .env 解析（避免额外依赖）：KEY=VALUE，# 开头为注释，不覆盖已有环境变量。"""
    if os.environ.get("COMBINE_IGNORE_DOTENV") == "1":
        return
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] not in ('"', "'"):
            v = v.split(" #", 1)[0].split("	#", 1)[0].strip()       # 去掉行内注释
        os.environ.setdefault(k.strip(), v.strip('"').strip("'"))


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(env("DATA_DIR", str(ROOT / "data")))
if not DATA_DIR.is_absolute():                      # 相对路径一律相对项目根目录，避免随启动目录变化
    DATA_DIR = ROOT / DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(env("DB_PATH", str(DATA_DIR / "combine.db")))

# cloud：服务器部署，禁止访问私网地址、不开放本地模型；self_hosted：用户本机运行
DEPLOY_MODE = env("DEPLOY_MODE", "self_hosted")
if DEPLOY_MODE not in ("cloud", "self_hosted"):
    DEPLOY_MODE = "self_hosted"

REGISTRATION_MODE = env("REGISTRATION_MODE", "open")      # open | closed
SESSION_DAYS = env_int("SESSION_DAYS", 14)
COOKIE_SECURE = env("COOKIE_SECURE", "0") == "1"

# 密钥主密钥（INV-18）。未配置时在数据目录生成并持久化一次，生产环境务必显式配置。
SECRETS_MASTER_KEY = env("SECRETS_MASTER_KEY", "")

# 平台内置模型（OpenAI 兼容接口）。未配置 Key 时自动启用 FakeLLM（离线确定性模型）。
PLATFORM_BASE_URL = env("PLATFORM_BASE_URL", "")
PLATFORM_API_KEY = env("PLATFORM_API_KEY", "")
PLATFORM_MODELS = {
    "fast": env("PLATFORM_MODEL_FAST", ""),
    "standard": env("PLATFORM_MODEL_STANDARD", ""),
    "strong": env("PLATFORM_MODEL_STRONG", ""),
}
# 微信开放平台「网站应用」扫码登录（F30）。三项齐备且模式不为 off 时才启用；缺省自动判定，
# 不必额外配置 WECHAT_MODE。fake = 本地模拟授权页（无需资质即可端到端自证与自动化测试）。
WECHAT_APPID = env("WECHAT_APPID", "")
WECHAT_SECRET = env("WECHAT_SECRET", "")
WECHAT_REDIRECT_URI = env("WECHAT_REDIRECT_URI", "")     # 必须与开放平台后台「授权回调域」完全一致
_WM = env("WECHAT_MODE", "")
WECHAT_MODE = _WM if _WM in ("live", "fake", "off") else (
    "live" if (WECHAT_APPID and WECHAT_SECRET and WECHAT_REDIRECT_URI) else "off")
WECHAT_TICKET_TTL_S = env_int("WECHAT_TICKET_TTL_S", 300)   # 二维码 / state 有效期（秒）

FAKE_LLM = env("FAKE_LLM", "") == "1" or not (PLATFORM_BASE_URL and PLATFORM_API_KEY)
FAKE_LATENCY_SCALE = env_float("FAKE_LATENCY_SCALE", 1.0)   # 测试时调小，例如 0.02

# 外网检索（v2.1）：auto = 真实多引擎检索；fake = 离线夹具（无网演示与全部测试）；
# off = 彻底关闭（不给模型提供 web.* 工具）。未显式配置时：配了真实模型则联网，否则离线夹具。
WEB_SEARCH_MODE_EXPLICIT = bool(env("WEB_SEARCH_MODE", "").strip())
WEB_SEARCH_MODE = env("WEB_SEARCH_MODE", "") or ("fake" if FAKE_LLM else "auto")
WEB_SEARCH_MODE = WEB_SEARCH_MODE if WEB_SEARCH_MODE in ("auto", "fake", "off") else "auto"

GLOBAL_MAX_CONCURRENCY = env_int("GLOBAL_MAX_CONCURRENCY", 16)
DEFAULT_PROVIDER_CONCURRENCY = env_int("DEFAULT_PROVIDER_CONCURRENCY", 4)
DEFAULT_TASK_BUDGET_CNY = env_float("DEFAULT_TASK_BUDGET_CNY", 2.0)
DEFAULT_MONTHLY_QUOTA_CNY = env_float("DEFAULT_MONTHLY_QUOTA_CNY", 50.0)
LLM_TIMEOUT_S = env_int("LLM_TIMEOUT_S", 90)
UPLOAD_MAX_BYTES = env_int("UPLOAD_MAX_BYTES", 2 * 1024 * 1024)
RATE_LIMIT = env("RATE_LIMIT", "1") == "1"                  # 进程内接口限流（hardening.RULES）
TRUST_PROXY = env("TRUST_PROXY", "0") == "1"                # 位于反向代理之后时，按 X-Forwarded-For 识别客户端
SSE_QUEUE_MAX = env_int("SSE_QUEUE_MAX", 1000)              # 单个实时连接最多积压的事件数，超出后改为从数据库补发
VERSION = "1.2.0"
