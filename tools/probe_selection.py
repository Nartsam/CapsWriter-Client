# coding: utf-8
"""只读检查真实前台 UIA 路径，不发送按键、不读写剪贴板、不打印选区正文。"""
import argparse
import ctypes
import importlib.util
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--depth', type=int, help='临时覆盖父链上限，默认使用生产设置')
    parser.add_argument('--wait', type=float, default=0, help='启动后留给手动选择文本的秒数')
    parser.add_argument('--all-patterns', action='store_true', help='检查父链各 TextPattern，仅输出选区长度')
    parser.add_argument('--samples', type=int, default=1, help='连续采样次数，最多 60 次')
    parser.add_argument('--interval', type=float, default=1, help='连续采样间隔秒数')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Windows only')
    spec = importlib.util.spec_from_file_location('selection_probe', ROOT / 'core/tools/windows_selection.py')
    native = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = native
    spec.loader.exec_module(native)
    if args.depth is not None:
        native.MAX_PARENT_DEPTH = max(1, min(args.depth, 256))
    native.READ_TIMEOUT_S = 5
    original_same = native._same_element
    original_interface = native._ComPointer.interface
    nodes = []
    oleaut32 = ctypes.WinDLL('oleaut32')
    oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
    oleaut32.SysFreeString.restype = None
    oleaut32.SysStringLen.argtypes = [ctypes.c_void_p]
    oleaut32.SysStringLen.restype = ctypes.c_uint32

    def string_property(element, slot):
        value = ctypes.c_void_p()
        try:
            element.call(slot, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(value))
            return ctypes.wstring_at(value, oleaut32.SysStringLen(value)) if value.value else ''
        finally:
            if value.value:
                oleaut32.SysFreeString(value)

    def same(automation, first, second):
        result = original_same(automation, first, second)
        if first not in nodes:
            nodes.append(first)
            try:
                print('ancestor={} control_type={} process={} matches_window={}'.format(
                    len(nodes) - 1, first.integer(21), first.integer(20), result), flush=True)
                if len(nodes) == 1 and not native._password_or_unknown(first):
                    # Name 可能包含文件名或用户内容，仅输出是否命中编辑器/终端标签。
                    name = string_property(first, 23).lower()
                    print('focus class={} framework={} editor_label={} terminal_label={}'.format(
                        string_property(first, 30), string_property(first, 40),
                        'editor' in name or '编辑器' in name,
                        'terminal' in name or '终端' in name), flush=True)
            except Exception as e:
                print('ancestor={} properties_error={}'.format(len(nodes) - 1, e), flush=True)
        return result

    def interface(pointer, slot, argtypes=(), *values):
        try:
            result = original_interface(pointer, slot, argtypes, *values)
        except Exception as e:
            if slot == 14:
                print('pattern ancestor={} error={}'.format(nodes.index(pointer), e), flush=True)
            raise
        if slot == 14:
            print('pattern ancestor={} supported={}'.format(nodes.index(pointer), result is not None), flush=True)
            if result is not None and args.all_patterns:
                with result:
                    text = native._selected_text(result, 1000, oleaut32)
                print('selection ancestor={} chars={}'.format(nodes.index(pointer), len(text)), flush=True)
                return None  # 探针继续检查父链，最终 status 不作为选区结论。
        return result

    native._same_element = same
    native._ComPointer.interface = interface
    if args.wait:
        time.sleep(max(0, min(args.wait, 30)))
    for sample in range(max(1, min(args.samples, 60))):
        if sample:
            time.sleep(max(0.1, min(args.interval, 10)))
        nodes.clear()
        print('sample={} time={} target={}'.format(
            sample + 1, time.strftime('%H:%M:%S'), native.get_input_target()), flush=True)
        started = time.monotonic()
        result = native.read_selection(1000)
        print('status={} chars={} detail={} seconds={:.3f}'.format(
            result.status, len(result.text), result.detail, time.monotonic() - started), flush=True)
        if native._read_lock.locked():
            break  # 超时工作线程仍可能在输出，不复用它的诊断节点列表。


if __name__ == '__main__':
    main()
