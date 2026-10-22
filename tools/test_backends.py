# coding: utf-8
"""
后端切换单测（桩掉 secrets，不联网）

覆盖 LLM/backends.py 的接线，不检查各角色当前的参数取值（温度、开关、模型名都是
日常会改的配置，钉住它们只会让测试在正常使用中失败）：

1. resolve() 两套 profile 的接线、协议约束、未知名字的回退、返回副本
2. **加载顺序不变式**：backends.py 必须排在所有引用它的角色文件之前，否则热切换会静默失效
3. 写了 backend 的角色文件，六个后端字段都取自同一套 profile
4. backend / backend_config 两个中间变量不会被 RoleLoader 当成配置项吃进去
"""
from __future__ import annotations

import glob
import importlib
import importlib.util
import os
import sys
import types
from dataclasses import fields

sys.dont_write_bytecode = True

# 仓库根目录按本文件位置推导，脚本从任何工作目录跑都成立
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# 真实的 LLM/secrets.py 未被 git 跟踪且存放密钥，一律桩掉：测试只关心「这个值有没有
# 被原样传下去」，用哨兵字符串比真密钥更容易断言
DEEPSEEK_SENTINEL = '<deepseek-key-sentinel>'
CUSTOM_SENTINEL = '<custom-key-sentinel>'

import LLM  # noqa: E402  （先拿到包对象，再把桩挂上去）

_secrets = types.ModuleType('LLM.secrets')
_secrets.DEEPSEEK_API_KEY = DEEPSEEK_SENTINEL
_secrets.CUSTOM_API_KEY = CUSTOM_SENTINEL
sys.modules['LLM.secrets'] = _secrets
LLM.secrets = _secrets

backends = importlib.import_module('LLM.backends')


def load_by_path(mod, relpath):
    p = os.path.join(REPO, relpath)
    spec = importlib.util.spec_from_file_location(mod, p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod] = m
    spec.loader.exec_module(m)
    return m


# 角色加载器要用包内相对导入（logger、llm_role_config），桩出 core 包后按路径加载真代码
for _name in ('core', 'core.client', 'core.client.llm'):
    _pkg = types.ModuleType(_name)
    _pkg.__path__ = []
    sys.modules[_name] = _pkg
sys.modules['core.client.llm'].logger = types.SimpleNamespace(
    debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None)
RoleConfig = load_by_path('core.client.llm.llm_role_config',
                          os.path.join('core', 'client', 'llm', 'llm_role_config.py')).RoleConfig
RoleLoader = load_by_path('core.client.llm.llm_role_loader',
                          os.path.join('core', 'client', 'llm', 'llm_role_loader.py')).RoleLoader

PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print('  [x] %s' % label)


def section(t):
    print()
    print('[%s]' % t)


def build_role_config(module):
    """用真实的 RoleLoader.register_role 把角色模块转成 RoleConfig"""
    loader = RoleLoader.__new__(RoleLoader)
    loader.roles_registry = {}
    name = loader.register_role(module.__name__, module)
    return loader.roles_registry[name]


# 引用了 backends 的角色文件：按文件内容判断，新增或删除角色都不用改这里
LLM_DIR = os.path.join(REPO, 'LLM')
NOT_ROLES = {'__init__.py', 'backends.py', 'secrets.py'}
ALL_FILES = sorted(os.path.basename(p) for p in glob.glob(os.path.join(LLM_DIR, '*.py')))
BACKEND_ROLES = [
    n for n in ALL_FILES
    if n not in NOT_ROLES
    and 'LLM.backends' in open(os.path.join(LLM_DIR, n), encoding='utf-8').read()
]
USED_KEYS = {'provider', 'api_format', 'api_url', 'api_key', 'model', 'request_timeout'}

# ---------------------------------------------------------------- 1
section('resolve() 的两套 profile')

ds = backends.resolve('deepseek')
# DeepSeek 的 Responses API 忽略 web_search，只有 Anthropic 兼容接口真的执行联网搜索
check(ds['api_format'] == 'anthropic',
      'deepseek 必须走 Anthropic 接口，否则联网搜索静默失效，实为 %r' % ds['api_format'])
check(ds['api_url'].rstrip('/').endswith('/anthropic'),
      'deepseek api_url 必须是 Anthropic 接口的根（客户端在后面拼 /v1/messages），实为 %r' % ds['api_url'])
check(ds['api_key'] == DEEPSEEK_SENTINEL, 'deepseek api_key 没有原样传下去')
# 跟着变量走而不是钉死名字：模型名是 backends.py 鼓励日常改动的配置。这里保留的是
# 接线性质：PROFILES 必须把 DEEPSEEK_MODEL 传出来，不许另写死一个
check(ds['model'] == backends.DEEPSEEK_MODEL, 'deepseek model 没有跟随 DEEPSEEK_MODEL')

custom = backends.resolve('custom')
check(custom['api_url'] == backends.CUSTOM_URL, 'custom api_url 没有跟随 CUSTOM_URL：%r' % custom['api_url'])
check(custom['api_format'] == 'responses',
      'custom 后端必须固定走 Responses，实为 %r' % custom['api_format'])
check(custom['provider'] == 'custom', 'custom 后端的 provider 应使用通用名称')
check(custom['api_key'] == CUSTOM_SENTINEL, 'custom api_key 没有原样传下去')
check(custom['model'] == backends.CUSTOM_MODEL, 'custom model 没有跟随 CUSTOM_MODEL')

for name, profile in (('deepseek', ds), ('custom', custom)):
    # 0 表示用 provider 兜底的 2 秒，对联网检索与网关的实测延迟必然超时
    check(profile['request_timeout'] > 0, '%s request_timeout 必须大于 0' % name)
    check(profile['provider'] != 'ollama', '%s 是 OpenAI 兼容接口，不能走 Ollama 分支' % name)

check(set(ds) == set(custom), '两套 profile 的键必须一致，否则角色文件里的取用会 KeyError')
check(USED_KEYS <= set(ds), '角色文件取用的键缺了：%r' % (USED_KEYS - set(ds)))

# ---------------------------------------------------------------- 2
section('未知 backend 回退而不是抛异常')

fallback = backends.resolve('gemini')          # 容易手滑写成模型名
check(fallback == ds, '未知名字应回退到 DeepSeek 那一套')
check(backends.resolve(None) == ds, 'None 也要回退，不能抛异常')
check(backends.resolve('') == ds, '空串也要回退，不能抛异常')

# ---------------------------------------------------------------- 3
section('resolve() 返回副本，调用方改不脏全局')

got = backends.resolve('custom')
got['model'] = '被角色文件改掉了'
check(backends.PROFILES['custom']['model'] == backends.CUSTOM_MODEL, 'PROFILES 被调用方污染了')
check(backends.resolve('custom')['model'] == backends.CUSTOM_MODEL, '下一次 resolve 拿到了被污染的值')

# ---------------------------------------------------------------- 4
section('加载顺序不变式（热切换的前提）')

check(bool(BACKEND_ROLES), '没有找到引用 backends 的角色文件，下面的检查都落空了')
late = [n for n in BACKEND_ROLES if ALL_FILES.index(n) < ALL_FILES.index('backends.py')]
check(not late, 'backends.py 必须排在引用它的角色文件之前，否则角色会 import 到上一轮的旧值：%r' % late)

# ---------------------------------------------------------------- 5
section('角色文件的接线')

for file_name in BACKEND_ROLES:
    role = os.path.splitext(file_name)[0]
    mod = importlib.import_module('LLM.%s' % role)
    if not hasattr(mod, 'backend'):
        continue
    cfg = build_role_config(mod)
    want = backends.resolve(mod.backend)
    # 无论 backend 选的是哪一套，六个字段都必须来自同一套 profile
    for key in sorted(USED_KEYS):
        check(getattr(cfg, key) == want[key], '%s.%s 未跟随 backend' % (role, key))

# ---------------------------------------------------------------- 6
section('中间变量不会被当成配置项')

field_names = {f.name for f in fields(RoleConfig)}
check('thinking_level' in field_names and 'enable_thinking' not in field_names,
      '角色字段应以 thinking_level 替换旧思考开关')
sample = types.SimpleNamespace(name='档位测试', provider='custom', model='m', thinking_level='high',
                               __name__='LLM.档位测试')
check(build_role_config(sample).thinking_level == 'high', '角色加载器应保留 thinking_level 的显式档位')
for name in ('backend', 'backend_config', 'resolve'):
    check(name not in field_names, '%s 撞上了 RoleConfig 字段名，会被 RoleLoader 吃进配置' % name)

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
