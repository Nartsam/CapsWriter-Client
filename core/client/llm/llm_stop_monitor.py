"""
LLM 输出中断监控

监听用户按下中断键（默认 ESC），停止 LLM 流式输出

使用 pynput GlobalHotKeys 替代 keyboard 库
"""
import threading
from typing import Optional
from config_client import ClientConfig as Config
from . import logger


class ToastRequest:
    """单个本地 Toast 的请求状态；完成与取消只有一个能先取得结果。"""

    def __init__(self, on_cancel):
        self.cancelled = threading.Event()
        self._finished = False
        self._lock = threading.Lock()
        self._on_cancel = on_cancel

    def cancel(self):
        with self._lock:
            if self._finished or self.cancelled.is_set():
                return
            self.cancelled.set()
        self._on_cancel()

    def finish(self) -> bool:
        """生成结束时认领结果；取消先发生则不允许提交历史。锁内不做磁盘写入。"""
        with self._lock:
            if self.cancelled.is_set():
                return False
            self._finished = True
            return True


class StopMonitor:
    """LLM 输出中断监控类"""
    
    def __init__(self):
        # OpenMate / typing 的全局快捷键停止事件。
        self._stop_event = threading.Event()
        self._toast_request = None
        # 快捷键管理器引用
        self._hotkey_manager = None
        self._is_running = False
        # 中断键的两层闸门（见 on_stop_pressed）。中断键是 pynput 全局热键，
        # 在任何前台程序里按下都会打到这里，因此必须自己判断「这一下是不是给我的」
        self._state_lock = threading.Lock()
        self._request_active = False   # 作用域闸门：有 LLM 请求正在跑
        self._output_started = False   # 起效闸门：本次请求已经开始产出正文

    def should_stop(self) -> bool:
        """
        检查是否应该停止输出

        Returns:
            True 表示应该停止
        """
        return self._stop_event.is_set()

    def reset(self):
        """重置停止标志（开始新的输出前调用）"""
        self._stop_event.clear()

    def begin_request(self):
        """进入一次 LLM 请求的作用域（在角色检测之前调用）

        必须与 end_request 成对使用，调用方用 try/finally 保证配对——漏掉
        end_request 会让作用域闸门一直开着，等于这层防护不存在。
        """
        with self._state_lock:
            self._request_active = True
            self._output_started = False
            self._toast_request = None
        self.reset()

    def end_request(self):
        """离开一次 LLM 请求的作用域

        顺手清掉停止标志：所有输出路径的 should_stop() 判断都在这之前完成，
        留着一个已置位的标志跨出请求边界正是本次要消除的那类隐患。
        """
        with self._state_lock:
            self._request_active = False
            self._output_started = False
            self._toast_request = None
        self.reset()

    def mark_output_started(self):
        """本次请求已经产出第一段正文

        由 LLMHandler.process 为 OpenMate / typing 包装回调后触发，粘贴模式也覆盖到。
        本地 Toast 使用自己的取消状态，不武装全局中断键。
        """
        with self._state_lock:
            if self._output_started or not self._request_active:
                return
            self._output_started = True
        logger.debug("本次 LLM 请求已开始产出正文，中断键从此刻起生效")

    def create_toast_request(self, on_cancel) -> ToastRequest:
        """本地 Toast 接管取消：窗口与工作线程持有同一对象，不借用后续轮次的状态。"""
        request = ToastRequest(on_cancel)
        with self._state_lock:
            self._toast_request = request
        return request

    def toast_cancelled(self) -> bool:
        request = self._toast_request
        return request is not None and request.cancelled.is_set()

    def on_stop_pressed(self):
        """用户按下中断键时的回调

        本地 Toast 只接受已点击且仍有焦点的窗口内 Esc，全局热键在此直接忽略。
        OpenMate / typing 保留 2026-09-12 的两层闸门：有请求且已产出正文
        才允许中断，避免在其他程序里按 Esc 让尚未显示的回答无声消失。提醒分析不武装。
        """
        with self._state_lock:
            active, started = self._request_active, self._output_started
            local_toast = self._toast_request is not None

        if not active or local_toast:
            # 与本程序无关的按键占绝大多数，压到 DEBUG 免得刷满日志
            logger.debug("检测到全局中断键，无适用请求或由本地 Toast 接管，忽略")
            return
        if not started:
            logger.info("检测到中断键，但本次 LLM 请求尚未产出任何正文，不作中断")
            return

        logger.info("检测到中断键，停止 LLM 输出")
        self._stop_event.set()

    def start(self):
        """启动中断键监控"""
        if self._is_running:
            return
            
        try:
            from core.client.global_hotkey import get_global_hotkey_manager
            
            self._hotkey_manager = get_global_hotkey_manager()
            
            # 将 Config.llm_stop_key 转换为 pynput 格式
            stop_key = Config.llm_stop_key.lower().strip()
            if not stop_key.startswith('<'):
                stop_key = f'<{stop_key}>'
            
            self._hotkey_manager.register(stop_key, self.on_stop_pressed)
            self._hotkey_manager.start()
            self._is_running = True
            logger.debug(f"LLM 中断键监控已启动: {stop_key}")
        except Exception as e:
            logger.warning(f"启动 LLM 中断键监控失败: {e}")

    def stop(self):
        """停止中断键监控"""
        if self._hotkey_manager and self._is_running:
            try:
                # 注意：目前 GlobalHotkeyManager 是单例，stop() 可能会影响全局
                # 但这里是按照实例化的逻辑进行封装
                self._hotkey_manager.stop()
                self._is_running = False
                logger.debug("LLM 中断键监控已停止")
            except Exception as e:
                logger.warning(f"停止 LLM 中断键监控失败: {e}")
