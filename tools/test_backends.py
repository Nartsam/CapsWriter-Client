# coding: utf-8
"""
后端切换单测（桩掉 secrets，不联网）

覆盖 2026-09-05 新增的 LLM/backends.py：

1. resolve() 两套 profile 的取值、未知名字的回退、返回副本
2. **加载顺序不变式**：backends.py 必须排在所有角色文件之前，否则热切换会静默失效
3. **回归保险**：backend='deepseek' 时四个角色解析出的 RoleConfig 与改造前逐字相同
   （唯一已知偏差是翻译的 request_timeout，由 0 变 60）
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
CPA_SENTINEL = '<cpa-key-sentinel>'

import LLM  # noqa: E402  （先拿到包对象，再把桩挂上去）

_secrets = types.ModuleType('LLM.secrets')
_secrets.DEEPSEEK_API_KEY = DEEPSEEK_SENTINEL
_secrets.CPA_API_KEY = CPA_SENTINEL
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


# llm_role_config.py 只依赖 dataclasses/typing，可以直接按路径加载，不必桩 core
RoleConfig = load_by_path(
    '_rc_for_backends',
    os.path.join('core', 'client', 'llm', 'llm_role_config.py')).RoleConfig

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
    """复刻 RoleLoader.register_role：只读 RoleConfig 声明过的字段"""
    config = {'module_name': module.__name__}
    for f in fields(RoleConfig):
        if f.name == 'module_name':
            continue
        if hasattr(module, f.name):
            config[f.name] = getattr(module, f.name)
    return RoleConfig(**config)


ROLES = ['助理', '雾切响子', '新岛真', '翻译']

# 改造前的取值，逐字抄自改造前的四个角色文件
BASELINE = {
    '助理': dict(temperature=0.3, max_tokens=4096, enable_vision=True,
                 enable_web_search=True, enable_datetime=True, enable_history=True,
                 enable_read_selection=True, output_mode='toast'),
    '雾切响子': dict(temperature=0.7, max_tokens=4096, enable_vision=True,
                     enable_web_search=True, enable_datetime=True, enable_history=True,
                     enable_read_selection=True, output_mode='toast'),
    '新岛真': dict(temperature=0.7, max_tokens=4096, enable_vision=True,
                   enable_web_search=True, enable_datetime=True, enable_history=True,
                   enable_read_selection=True, output_mode='toast'),
    '翻译': dict(temperature=0.2, max_tokens=4096, enable_vision=True,
                 enable_web_search=False, enable_datetime=False, enable_history=False,
                 enable_read_selection=True, output_mode='toast'),
}

# ---------------------------------------------------------------- 1
section('resolve() 的两套 profile')

ds = backends.resolve('deepseek')
check(ds['provider'] == 'deepseek', 'deepseek provider: %r' % ds['provider'])
check(ds['api_url'] == 'https://api.deepseek.com',
      'deepseek api_url 必须不带 /v1（Responses API 官方示例），实为 %r' % ds['api_url'])
check(ds['api_key'] == DEEPSEEK_SENTINEL, 'deepseek api_key 没有原样传下去')
# 与 cpa 侧对称，跟着变量走而不是钉死名字：DeepSeek 会把旧模型名路由到新模型，
# 名字早已不代表能力，钉住它既证明不了「还能看图」，又会在 backends.py 鼓励的日常
# 换模型操作里必然失败。模型是否真支持视觉只有 probe_llm_live.py 能验。
# 这里保留的是接线性质：PROFILES 必须把 DEEPSEEK_MODEL 传出来，不许另写死一个
# （角色循环里的 cfg.model == want['model'] 是 resolve 跟 resolve 比，守不到）
check(ds['model'] == backends.DEEPSEEK_MODEL, 'deepseek model 没有跟随 DEEPSEEK_MODEL')
check(ds['request_timeout'] == 60, 'deepseek request_timeout: %r' % ds['request_timeout'])

cpa = backends.resolve('cpa')
check(cpa['provider'] == 'cpa',
      'cpa provider 应为 cpa（网关也代理 kimi/glm/grok，不写 gemini），实为 %r' % cpa['provider'])
check(cpa['api_url'] == backends.CPA_URL, 'cpa api_url: %r' % cpa['api_url'])
check(cpa['api_url'].endswith('/v1'),
      'cpa base_url 必须带 /v1：chat/completions 与 responses 两条路径都拼在它后面')
check(cpa['api_key'] == CPA_SENTINEL, 'cpa api_key 没有原样传下去')
check(cpa['model'] == backends.CPA_MODEL, 'cpa model 没有跟随 CPA_MODEL')
check(cpa['request_timeout'] == 60,
      'cpa request_timeout 必须大于 0：provider 兜底的 2s 对 3~7s 的实测延迟发发超时')

check(set(ds) == set(cpa) == {'provider', 'api_url', 'api_key', 'model', 'request_timeout'},
      '两套 profile 的键必须一致，否则角色文件里的取用会 KeyError')

# ---------------------------------------------------------------- 2
section('未知 backend 回退而不是抛异常')

fallback = backends.resolve('gemini')          # 容易手滑写成模型名
check(fallback == ds, '未知名字应回退到 DeepSeek 那一套')
check(backends.resolve(None) == ds, 'None 也要回退，不能抛异常')
check(backends.resolve('') == ds, '空串也要回退，不能抛异常')

# ---------------------------------------------------------------- 3
section('resolve() 返回副本，调用方改不脏全局')

got = backends.resolve('cpa')
got['model'] = '被角色文件改掉了'
check(backends.PROFILES['cpa']['model'] == backends.CPA_MODEL, 'PROFILES 被调用方污染了')
check(backends.resolve('cpa')['model'] == backends.CPA_MODEL, '下一次 resolve 拿到了被污染的值')

# ---------------------------------------------------------------- 4
section('加载顺序不变式（热切换的前提）')

names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(REPO, 'LLM', '*.py')))
names = [n for n in names if n != '__init__.py']
check(names[0] == 'backends.py',
      'backends.py 必须是 sorted(glob) 里的第一个，否则角色会 import 到上一轮的旧值；实为 %r' % names[:2])

role_files = [n for n in names if os.path.splitext(n)[0] in ROLES]
check(len(role_files) == 4, '角色文件应有 4 个，实为 %r' % role_files)
check(all(names.index('backends.py') < names.index(n) for n in role_files),
      'backends.py 排在了某个角色文件之后')

# ---------------------------------------------------------------- 5
section('四个角色的接线与回归基线')

for role in ROLES:
    mod = importlib.import_module('LLM.%s' % role)
    cfg = build_role_config(mod)
    want = backends.resolve(mod.backend)

    # 接线不变式：无论 backend 选的是哪一套，五个字段都必须来自同一套 profile
    check(cfg.provider == want['provider'], '%s provider 未跟随 backend' % role)
    check(cfg.api_url == want['api_url'], '%s api_url 未跟随 backend' % role)
    check(cfg.api_key == want['api_key'], '%s api_key 未跟随 backend' % role)
    check(cfg.model == want['model'], '%s model 未跟随 backend' % role)
    check(cfg.request_timeout == want['request_timeout'], '%s request_timeout 未跟随 backend' % role)

    # 回归基线：其余字段一个都不许动
    for k, v in BASELINE[role].items():
        check(getattr(cfg, k) == v, '%s.%s 被改动了：%r != %r' % (role, k, getattr(cfg, k), v))

    # 具名角色是连续对话与角色前缀检测的前提
    check(bool(cfg.display_name) and cfg.display_name != RoleConfig.DEFAULT_ROLE_NAME,
          '%s 的 display_name 异常：%r' % (role, cfg.display_name))
    check(cfg.enabled is True, '%s 被意外禁用' % role)

# ---------------------------------------------------------------- 6
section('中间变量不会被当成配置项')

field_names = {f.name for f in fields(RoleConfig)}
check('backend' not in field_names, 'backend 撞上了 RoleConfig 字段名，会被 RoleLoader 吃进配置')
check('backend_config' not in field_names, 'backend_config 撞上了 RoleConfig 字段名')
check('resolve' not in field_names, 'resolve 撞上了 RoleConfig 字段名')

mod = importlib.import_module('LLM.助理')
check(hasattr(mod, 'backend') and hasattr(mod, 'backend_config'),
      '角色文件里应保留这两个中间变量，供人阅读')

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
