# coding: utf-8
"""
录音悬浮指示器预览：用真实录音的音量驱动声波条（真 Tk，会弹窗、会出声）

用真实的 RecordingIndicator 弹出指示器（固定在屏幕中下方），把已保存录音的电平按 50ms
一块实时喂进去（与客户端麦克风回调的块长相同），同时播放这段声音；每条录音放完后依次
演示 等待识别 → 识别中 → 处理结果 → 打断 / 出错。用来调声波条的观感（尺寸、配色、动画
参数都在 recording_indicator.py 文件头），以及拿不同机器的录音核对自适应换算。

每条录音结束时打印换算结果：有高度的块的高度分布、顶满的比例、底噪线与峰值线。换算
状态在各条录音之间保留，与客户端运行期间一样；--fresh 让每条都从头适应（模拟刚启动）。

用法（在仓库根目录执行）：
  python tools/preview_indicator.py                     # 本机最近 3 条录音
  python tools/preview_indicator.py --color gradient    # 蓝→青渐变配色
  python tools/preview_indicator.py --root S:/Software/LittleTools/CapsWriter-Client
  python tools/preview_indicator.py --file a.mp3 --file b.mp3 --chat --image --mute

需要 FFmpeg（解码录音）和桌面会话，不进 run_tests.py 的流水线。播放用的临时 WAV 放在
tools/ 下的临时目录，结束时删除。
"""
from __future__ import annotations

import argparse
import array
import ctypes
import glob
import importlib.util
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))

SAMPLE_RATE = 48000
BLOCK = 0.05            # 与客户端 AudioStreamManager 的采集块长相同


def load_indicator():
    """按路径加载真实的 recording_indicator.py，包级 logger 用桩代替（系统 Python 没有 rich）"""
    for name in ('core', 'core.client', 'core.client.ui'):
        m = types.ModuleType(name)
        m.__path__ = []
        m.logger = logging.getLogger('preview-indicator')
        sys.modules[name] = m
    spec = importlib.util.spec_from_file_location(
        'core.client.ui.recording_indicator',
        os.path.join(REPO, 'core', 'client', 'ui', 'recording_indicator.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def block_levels(path):
    """录音每 50ms 一块的电平（dBFS），算法与客户端音频回调相同"""
    raw = subprocess.run(
        ['ffmpeg', '-v', 'quiet', '-i', path, '-ac', '1', '-ar', str(SAMPLE_RATE), '-f', 'f32le', '-'],
        capture_output=True, check=True).stdout
    samples = array.array('f')
    samples.frombytes(raw[:len(raw) // 4 * 4])
    n = int(SAMPLE_RATE * BLOCK)
    levels = []
    for i in range(0, len(samples) - n + 1, n):
        power = sum(x * x for x in samples[i:i + n]) / n
        levels.append(10 * math.log10(power) if power > 1e-12 else -120.0)
    return levels


def pick_clips(args):
    """[(路径, 电平列表)]：--file 指定的，或 --root 下最近的录音（跳过太短的）"""
    if args.file:
        missing = [path for path in args.file if not os.path.isfile(path)]
        if missing:
            print('找不到录音文件：' + '、'.join(missing))
            return []
        return [(path, block_levels(path)) for path in args.file]
    pattern = os.path.join(args.root, '20[0-9][0-9]', '*', 'assets', '*.mp3')
    clips = []
    for path in sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True):
        levels = block_levels(path)
        if len(levels) * BLOCK >= args.min_seconds:
            clips.append((path, levels))
        if len(clips) >= args.count:
            break
    return clips


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))] if values else float('nan')


def play_clip(ind, index, path, levels, args, wav_dir):
    import winsound

    duration = len(levels) * BLOCK
    print('[%d] %s（%.1fs）' % (index + 1, os.path.basename(path), duration))
    if not args.mute:
        wav = os.path.join(wav_dir, 'clip%d.wav' % index)
        subprocess.run(['ffmpeg', '-v', 'quiet', '-y', '-i', path, '-ac', '1', '-ar', str(SAMPLE_RATE),
                        '-c:a', 'pcm_s16le', wav], check=True)

    ind.show_recording(time.time(), 'ctrl_r',
                       'active' if args.chat else '', '雾切响子' if args.chat else '',
                       has_image=args.image, streaming=True)
    if not args.mute:
        winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)

    heights = []
    start = time.perf_counter()
    for i, db in enumerate(levels):
        delay = start + i * BLOCK - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        ind.update_level(db, BLOCK)
        heights.append(ind._level_sample[0])

    shown = [h for h in heights if h > 0.05]
    norm = ind._normalizer
    print('    有高度的块 %d%%，其中高度 p50 %.2f / p90 %.2f，顶满 %d%%；底噪线 %.1f dBFS，峰值线 %.1f dBFS'
          % (100 * len(shown) / max(1, len(heights)), quantile(shown, 0.5), quantile(shown, 0.9),
             100 * sum(1 for h in shown if h >= 0.99) / max(1, len(shown)), norm.floor_db, norm.peak_db))

    ind.show_waiting(duration)
    time.sleep(1.6)
    ind.update_progress(duration, '这里是服务端返回的识别文字预览，用来看第二行的截断效果')
    time.sleep(1.4)
    ind.show_processing()
    time.sleep(1.0)
    if index % 2 == 0:
        ind.show_cancelled('esc', duration_ms=1500)
        time.sleep(2.0)
    else:
        ind.show_error('服务端未连接，无法发送音频', duration_ms=2000)
        time.sleep(2.5)


def main():
    parser = argparse.ArgumentParser(description='用真实录音的音量预览录音悬浮指示器的声波条（会弹窗、会出声）')
    parser.add_argument('--root', default=REPO, help='在这个目录的 年/月/assets 下找录音（默认本仓库）')
    parser.add_argument('--file', action='append', help='指定录音文件，可重复；给了就不按 --root 找')
    parser.add_argument('--count', type=int, default=3, help='按 --root 找时取最近几条（默认 3）')
    parser.add_argument('--min-seconds', type=float, default=2.0, help='跳过短于此时长的录音（默认 2 秒）')
    parser.add_argument('--color', choices=('height', 'gradient'), help='覆盖 BAR_COLOR_MODE')
    parser.add_argument('--chat', action='store_true', help='连续对话中的浅绿底色')
    parser.add_argument('--image', action='store_true', help='挂图时的内嵌描边')
    parser.add_argument('--fresh', action='store_true', help='每条录音都从头适应（模拟刚启动客户端）')
    parser.add_argument('--mute', action='store_true', help='不播放声音')
    args = parser.parse_args()

    if os.name != 'nt':
        print('仅适用于 Windows')
        return 0
    if not shutil.which('ffmpeg'):
        print('找不到 FFmpeg，无法解码录音')
        return 1

    clips = pick_clips(args)
    if not clips:
        print('没有找到可用的录音')
        return 1

    # 与客户端相同的 DPI 感知级别（core/ui/dpi.py），否则窗口被系统按 96 DPI 拉伸
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass
    mod = load_indicator()
    if args.color:
        mod.BAR_COLOR_MODE = args.color
    # 固定在屏幕中下方：预览时输入光标在终端里，贴着它显示会挡住输出
    mod.RecordingIndicator._get_caret_position = lambda self: None
    ind = mod.RecordingIndicator()

    with tempfile.TemporaryDirectory(dir=HERE, prefix='_preview_') as wav_dir:
        try:
            for index, (path, levels) in enumerate(clips):
                if args.fresh:
                    ind._normalizer = mod.LevelNormalizer()
                play_clip(ind, index, path, levels, args, wav_dir)
                time.sleep(0.5)
        except KeyboardInterrupt:
            print('已中止')
        finally:
            import winsound
            winsound.PlaySound(None, 0)
            ind.hide()
            time.sleep(0.3)
    sys.stdout.flush()
    # Tk 对象属于指示器线程，交给解释器退出时在主线程回收会报 Tcl_AsyncDelete，直接退出
    os._exit(0)


if __name__ == '__main__':
    sys.exit(main())
