# coding: utf-8
"""
录音悬浮指示器的声波条：音量换算（LevelNormalizer）与竖条目标高度（桩模块加载，不建窗口）

合成信号按两台机器实测的电平特征生成（停顿、音节、音节间低谷交替）：
  本机    说话约 −56～−41 dBFS，不说话时被降噪压到 −98
  小主机  说话约 −35～−21 dBFS，底噪约 −70
检查的是性质：两台换算后说话的高度相近、静音贴底、一次瞬态冲高不会把之后的说话压低、
持续底噪不会被放大成满格、第一次开口很快跟上；竖条轮廓中间高两边低、加载波浪从左往右走、
打断与出错时压平、颜色越高越饱和。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import random
import sys
import types

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for name in ('core', 'core.client', 'core.client.ui'):
    m = types.ModuleType(name)
    m.__path__ = []
    m.logger = logging.getLogger('level-meter-test')
    m.logger.addHandler(logging.NullHandler())
    m.logger.propagate = False
    sys.modules[name] = m

spec = importlib.util.spec_from_file_location(
    'core.client.ui.recording_indicator',
    os.path.join(REPO, 'core', 'client', 'ui', 'recording_indicator.py'))
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

BLOCK = 0.05            # 与客户端采集块长相同
PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print('  [×] %s' % label)


def median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else float('nan')


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))] if values else float('nan')


# 两台机器的电平特征：(不说话时, 不说话时的起伏, 说话最低, 说话最高)
LOCAL = (-98.0, 0.3, -56.0, -41.0)
MINI = (-70.0, 1.5, -35.0, -21.0)


def session(rng, profile, seconds, pauses=True):
    """[(dB, 类别)]，类别：'silence' 停顿、'speech' 音节、None 音节间低谷（不参与统计）"""
    floor, wobble, lo, hi = profile
    out = []
    while len(out) * BLOCK < seconds:
        if pauses:
            for _ in range(rng.randint(10, 30)):
                out.append((floor + rng.uniform(-wobble, wobble), 'silence'))
        sentence = rng.randint(40, 80)
        n = 0
        while n < sentence:
            for _ in range(rng.randint(3, 6)):
                out.append((rng.uniform(lo, hi), 'speech'))
                n += 1
            for _ in range(rng.randint(1, 2)):
                out.append((rng.uniform(lo - 25, lo - 12), None))
                n += 1
    return out


def feed(norm, seq):
    return [(norm.update(db, BLOCK), kind) for db, kind in seq]


def levels(pairs, kind):
    return [level for level, k in pairs if k == kind]


# ---------------------------------------------------------------------------
print('[1] 两台机器换算后说话的高度相近，静音贴底')
results = {}
for label, profile in (('本机', LOCAL), ('小主机', MINI)):
    norm = mod.LevelNormalizer()
    pairs = feed(norm, session(random.Random(7), profile, 40))
    steady = pairs[int(4 / BLOCK):]                     # 跳过开头几秒的适应
    results[label] = median(levels(steady, 'speech'))
    check(results[label] >= 0.55, '%s 说话中位高度 %.2f 应明显高于一半' % (label, results[label]))
    silent = quantile(levels(steady, 'silence'), 0.95)
    check(silent <= 0.05, '%s 静音 p95 高度 %.2f 应贴底' % (label, silent))
check(abs(results['本机'] - results['小主机']) <= 0.15,
      '两台说话中位高度应相近：本机 %.2f / 小主机 %.2f' % (results['本机'], results['小主机']))

# ---------------------------------------------------------------------------
print('[2] 一次瞬态冲高（咳嗽、敲桌子）不会把之后的说话压低')
for label, profile in (('本机', LOCAL), ('小主机', MINI)):
    norm = mod.LevelNormalizer()
    feed(norm, session(random.Random(11), profile, 20))
    same = session(random.Random(12), profile, 6)        # 冲高前后喂同一段话，差异只来自冲高
    before = median(levels(feed(norm, same), 'speech'))
    feed(norm, [(profile[3] + 15, None)] * 3)
    after = median(levels(feed(norm, same), 'speech'))
    check(after >= before - 0.1, '%s 冲高前 %.2f、冲高后 %.2f' % (label, before, after))

# ---------------------------------------------------------------------------
print('[3] 持续底噪不会被放大成满格')
norm = mod.LevelNormalizer()
rng = random.Random(3)
noise = feed(norm, [(-50 + rng.uniform(-2, 2), None) for _ in range(int(12 / BLOCK))])
tail = [level for level, _ in noise[-int(3 / BLOCK):]]
check(quantile(tail, 0.9) <= 0.2, '只有底噪时稳定后 p90 高度 %.2f 应接近贴底' % quantile(tail, 0.9))
norm = mod.LevelNormalizer()
muted = feed(norm, [(-120.0, None)] * int(10 / BLOCK))
check(max(level for level, _ in muted) == 0.0, '麦克风静音（数字零）时高度应为 0')
check(norm.peak_db >= mod.LEVEL_PEAK_MIN_DB, '满格参考不应低于下限')

# ---------------------------------------------------------------------------
print('[4] 第一次开口很快跟上，长停顿后不必重新适应')
for label, profile in (('本机', LOCAL), ('小主机', MINI)):
    norm = mod.LevelNormalizer()
    pairs = feed(norm, session(random.Random(5), profile, 3, pauses=False))
    voiced = levels(pairs, 'speech')
    second = voiced[10:30]                               # 开口后约 0.5～1.5 秒
    saturated = sum(1 for v in second if v >= 0.99) / max(1, len(second))
    check(saturated < 0.5, '%s 开口第一秒内满格比例 %.0f%% 应已回落' % (label, saturated * 100))

    norm = mod.LevelNormalizer()
    feed(norm, session(random.Random(21), profile, 20))
    same = session(random.Random(22), profile, 3, pauses=False)
    before = median(levels(feed(norm, same), 'speech'))
    feed(norm, [(profile[0], 'silence')] * int(15 / BLOCK))
    after = median(levels(feed(norm, same), 'speech'))
    check(abs(after - before) <= 0.1, '%s 停顿 15 秒前后说话高度 %.2f / %.2f 应相近' % (label, before, after))

# ---------------------------------------------------------------------------
print('[5] 竖条目标高度与颜色')
ind = mod.RecordingIndicator()           # 构造不建 Tk，只用纯计算的方法
centre = mod.BAR_COUNT // 2

ind.state = mod.IndicatorState(mode='recording')
ind._level_env = 0.0
samples = [ind._bar_targets(t * 0.05) for t in range(int(mod.IDLE_PERIOD_S / 0.05) + 1)]
flat = [h for frame in samples for h in frame]
check(min(flat) >= mod.BAR_MIN_H - 1e-6 and max(flat) <= mod.BAR_MIN_H + mod.IDLE_AMP_H + 1e-6,
      '静音时竖条只在呼吸幅度内起伏')
check(max(flat) - min(flat) >= mod.IDLE_AMP_H * 0.9, '静音时竖条应有呼吸起伏')

ind._level_env = 1.0
samples = [ind._bar_targets(t * 0.05) for t in range(200)]
means = [sum(frame[i] for frame in samples) / len(samples) for i in range(mod.BAR_COUNT)]
check(all(means[i] < means[i + 1] for i in range(centre))
      and all(means[i] > means[i + 1] for i in range(centre, mod.BAR_COUNT - 1)),
      '满音量时平均高度应中间高、两边低：%s' % ['%.0f' % m for m in means])
check(max(h for frame in samples for h in frame) <= mod.BAR_MAX_H + 1e-6, '竖条不超过最大高度')
check(len({round(frame[centre], 1) for frame in samples}) > 10, '满音量时各竖条仍有抖动，不是一块板')

ind.state = mod.IndicatorState(mode='waiting')
period = mod.WAVE_PERIOD_S
crest = [max(range(mod.BAR_COUNT), key=lambda i: ind._bar_targets(period * f)[i]) for f in (0.3, 0.5, 0.7)]
check(crest[0] < crest[1] < crest[2], '加载波浪的波峰应从左往右移动：%s' % crest)
wave_max = max(max(ind._bar_targets(period * f / 20)) for f in range(20))
check(wave_max < max(means), '加载波浪应低于说话时的高度')

for mode_name in ('cancelled', 'error'):
    ind.state = mod.IndicatorState(mode=mode_name)
    check(all(h == mod.BAR_MIN_H for h in ind._bar_targets(0.7)), '%s 时竖条压平' % mode_name)

check(ind._bar_color(0, mod.BAR_MIN_H) == mod.BAR_COLOR_LOW.upper()
      and ind._bar_color(0, mod.BAR_MAX_H) == mod.BAR_COLOR_HIGH.upper(),
      '按高度取色：最矮是灰蓝、最高是强调蓝')
mid = (mod.BAR_MIN_H + mod.BAR_MAX_H) / 2
check(ind._bar_color(0, mid) not in (mod.BAR_COLOR_LOW.upper(), mod.BAR_COLOR_HIGH.upper()), '中间高度取中间色')
original = mod.BAR_COLOR_MODE
mod.BAR_COLOR_MODE = 'gradient'
check(ind._bar_color(1, mod.BAR_MIN_H) == ind._bar_color(1, mod.BAR_MAX_H)
      and ind._bar_color(0, mid) != ind._bar_color(mod.BAR_COUNT - 1, mid),
      '渐变配色：颜色只随位置变、不随高度变')
mod.BAR_COLOR_MODE = original

# ---------------------------------------------------------------------------
print('[6] 音量包络：跟得上，音频中断时回落')
ind.state = mod.IndicatorState(mode='recording')
ind._level_env = 0.0
ind._phase_epoch = 0.0
now = 100.0
for _ in range(10):                                     # 0.33 秒持续满音量
    now += 0.033
    ind._level_sample = (1.0, now)
    ind._step_bars(now, 0.033)
check(ind._level_env > 0.9, '持续满音量 0.33 秒后包络 %.2f 应接近满' % ind._level_env)
for _ in range(30):                                     # 之后 1 秒没有新电平
    now += 0.033
    ind._step_bars(now, 0.033)
check(ind._level_env < 0.1, '电平中断 1 秒后包络 %.2f 应回落到静音' % ind._level_env)

print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
