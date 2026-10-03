# coding: utf-8
"""
StopMonitor 中断键两层闸门单测（桩模块加载，不依赖项目运行时）

钉住 2026-09-12 那次「回复凭空消失」的修复：中断键是全局热键，只有
「有请求在跑」且「已经产出正文」时才认作中断指令。用真线程跑一遍
mark_output_started，顺带验证它的幂等与跨线程可见性——`should_stop()`
实际是在 to_thread 的执行器线程里被调用的。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import types

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 桩：llm_stop_monitor 依赖包内 logger 与 config_client
for name in ('core', 'core.client', 'core.client.llm'):
    m = types.ModuleType(name)
    m.__path__ = []
    sys.modules[name] = m


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


sys.modules['core.client.llm'].logger = _Logger()

cfg = types.ModuleType('config_client')


class ClientConfig:
    llm_stop_key = 'esc'


cfg.ClientConfig = ClientConfig
sys.modules['config_client'] = cfg

spec = importlib.util.spec_from_file_location(
    'core.client.llm.llm_stop_monitor',
    os.path.join(REPO, 'core', 'client', 'llm', 'llm_stop_monitor.py'))
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

StopMonitor = mod.StopMonitor

PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print('  [×] %s' % label)


def section(title):
    print()
    print('[%s]' % title)


# ---------------------------------------------------------------- 1
section('作用域闸门：没有请求在跑时按下中断键不留任何状态')
m = StopMonitor()
m.on_stop_pressed()
check(not m.should_stop(), '请求外的按键被忽略')
m.begin_request()
check(not m.should_stop(), '进入请求作用域时是干净的')

# ---------------------------------------------------------------- 2
section('起效闸门：请求在跑但还没产出正文时不作中断')
m = StopMonitor()
m.begin_request()
m.on_stop_pressed()
check(not m.should_stop(), '零输出时的按键不中断（2026-09-12 故障原型）')
m.mark_output_started()
check(not m.should_stop(), '武装本身不会置位停止标志')
m.on_stop_pressed()
check(m.should_stop(), '产出正文之后按键恢复中断语义')

# ---------------------------------------------------------------- 3
section('作用域关闭后状态归零')
m.end_request()
m.on_stop_pressed()
check(not m.should_stop(), 'end_request 后按键不再生效')
m.begin_request()
check(not m.should_stop(), '下一次请求不继承上一次的停止标志')
m.on_stop_pressed()
check(not m.should_stop(), '下一次请求需要重新武装')

# ---------------------------------------------------------------- 4
section('重复武装')
# 「请求作用域之外武装无效」没有可观察的效果（begin_request 本来就会清零），不单独测
m = StopMonitor()
m.begin_request()
m.mark_output_started()
m.mark_output_started()
m.on_stop_pressed()
check(m.should_stop(), '重复武装幂等')

# ---------------------------------------------------------------- 5
section('跨线程可见性（should_stop 实际在执行器线程上被调用）')
m = StopMonitor()
m.begin_request()
seen = []


def worker():
    # 武装发生在主线程，工作线程必须立刻看得见
    seen.append(m.should_stop())


m.mark_output_started()
m.on_stop_pressed()
t = threading.Thread(target=worker)
t.start()
t.join(2)
check(seen == [True], '工作线程读到了主线程置位的停止标志')

# ---------------------------------------------------------------- 6
section('reset 只清停止标志，不动作用域')
m = StopMonitor()
m.begin_request()
m.mark_output_started()
m.on_stop_pressed()
m.reset()
check(not m.should_stop(), 'reset 清掉了停止标志')
m.on_stop_pressed()
check(m.should_stop(), 'reset 之后仍在请求作用域内且仍处于武装状态')

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
