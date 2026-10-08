"""
LLM 角色的后端切换

角色文件里只写一行 `backend = 'deepseek'` 或 `'cpa'`，具体用哪个模型、连哪个地址、
用什么请求格式、超时多少，全部在本文件里定。

**热切换**：`llm_watcher` 监视整个 `LLM/` 目录，改本文件会触发全部角色重载
（`_do_reload` → `load_all_roles`）。`load_all_roles` 对 `sorted(glob('*.py'))` 里的
每个文件先 `del sys.modules` 再 import，而 `backends.py` 的首字母 `b` 排在
`default.py`、`secrets.py` 与四个中文角色文件之前，所以它一定先被重新执行、角色
随后 import 到的就是新值。**不要把本文件改成排序靠后的名字**（比如 `zz_backends.py`），
那样角色读到的会是上一轮的旧值，而且不报错。
"""
from LLM.secrets import CPA_API_KEY, DEEPSEEK_API_KEY


# ==================== CPA 网关（局域网小主机） ====================

# 改这一行即可换模型，存盘立即生效，不必重启客户端。
# 网关上可用的 Gemini 有四个，分属两条不同上游：
#   'gemini-3.8-flash'        / 'gemini-3.7-flash'          → antigravity
# 不用 vertex/ 前缀的模型（用户决定）
CPA_MODEL = 'gemini-3.8-flash(low)'

# 只有局域网内可达。人不在这个网里时，走 cpa 的角色一律请求失败，**不会自动回退**
# 到 DeepSeek——要切回去得自己改角色文件里的 backend
CPA_URL = 'http://192.168.5.8:8317/v1'


# ==================== DeepSeek 直连 ====================

DEEPSEEK_MODEL = 'deepseek-flash'

# 走 Anthropic 兼容接口（客户端在后面拼 /v1/messages）。DeepSeek 的 Responses API 对 web_search 一律忽略，flash 会把工具调用当正文吐出来；只有这个接口真的执行联网搜索，
# 且 thinking: disabled 在这里生效、短问答更快（见 core/client/llm/llm_anthropic.py）
DEEPSEEK_URL = 'https://api.deepseek.com/anthropic'


# 两套后端统一取同一个值。CPA 实测中位 3~7s、带图约 20s，DeepSeek 侧的联网搜索在
# 首个分片前也会静默数秒，provider 默认的 2s 两边都会发发超时（约定 15②）
REQUEST_TIMEOUT = 60


PROFILES = {
    'deepseek': {
        'provider': 'deepseek',
        'api_format': 'anthropic',
        'api_url': DEEPSEEK_URL,
        'api_key': DEEPSEEK_API_KEY,
        'model': DEEPSEEK_MODEL,
        'request_timeout': REQUEST_TIMEOUT,
    },
    'cpa': {
        # 刻意不写 'gemini'：网关同时代理 kimi / glm / grok 等，换模型时这个名字不用
        # 跟着改。provider 只影响日志与错误信息里的名字，以及 APIConfig 的兜底查表，
        # 而上面恒定给出 request_timeout > 0，那张兜底超时表永远走不到
        'provider': 'cpa',
        'api_format': 'openai',
        'api_url': CPA_URL,
        'api_key': CPA_API_KEY,
        'model': CPA_MODEL,
        'request_timeout': REQUEST_TIMEOUT,
    },
}

DEFAULT_BACKEND = 'deepseek'


def resolve(backend):
    """按名字取出一套后端配置，返回副本

    名字写错时回退到 DeepSeek 并打印告警，而**不是**抛异常：角色文件在 import 期
    抛异常会被 `RoleLoader.load_all_roles` 的 `except Exception` 接住，那个角色就
    整个从 `roles_registry` 里消失，而 errors 列表没有任何调用方在看
    （`llm_handler.reload_roles` 直接丢弃返回值）。届时唯一的现象是"这个角色不响应
    了"，排查成本远高于一行告警。
    """
    profile = PROFILES.get(backend)
    if profile is None:
        print('[LLM backends] 未知的 backend %r，已回退到 %r；可选：%s'
              % (backend, DEFAULT_BACKEND, ' / '.join(sorted(PROFILES))))
        profile = PROFILES[DEFAULT_BACKEND]
    return dict(profile)
