"""
LLM 输出中断监控

监听用户按下中断键（默认 ESC），停止 LLM 流式输出

使用 pynput GlobalHotKeys 替代 keyboard 库
"""
import threading
from typing import Optional
from config_client import ClientConfig as Config
from . import logger


class StopMonitor:
    """LLM 输出中断监控类"""
    
    def __init__(self):
        # 任务独立的停止事件
        self._stop_event = threading.Event()
        # 线程局部存储：每个任务独立的停止标志
        self._thread_local = threading.local()
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
        # 优先检查线程局部停止标志
        local_stop = getattr(self._thread_local, 'stop_event', None)
        if local_stop and local_stop.is_set():
            return True

        # 检查全局停止标志（ESC 键触发）
        return self._stop_event.is_set()

    def reset(self):
        """重置停止标志（开始新的输出前调用）"""
        self._stop_event.clear()
        # 重置线程局部停止标志
        local_stop = getattr(self._thread_local, 'stop_event', None)
        if local_stop:
            local_stop.clear()

    def begin_request(self):
        """进入一次 LLM 请求的作用域（在角色检测之前调用）

        必须与 end_request 成对使用，调用方用 try/finally 保证配对——漏掉
        end_request 会让作用域闸门一直开着，等于这层防护不存在。
        """
        with self._state_lock:
            self._request_active = True
            self._output_started = False
        self.reset()

    def end_request(self):
        """离开一次 LLM 请求的作用域

        顺手清掉停止标志：所有输出路径的 should_stop() 判断都在这之前完成，
        留着一个已置位的标志跨出请求边界正是本次要消除的那类隐患。
        """
        with self._state_lock:
            self._request_active = False
            self._output_started = False
        self.reset()

    def mark_output_started(self):
        """本次请求已经产出第一段正文（或第一条工具状态占位）

        由 LLMHandler.process 包装回调后统一触发，三条流式路径与粘贴模式都覆盖到。
        在此之前按下的中断键一律不作数，见 on_stop_pressed。
        """
        with self._state_lock:
            if self._output_started or not self._request_active:
                return
            self._output_started = True
        logger.debug("本次 LLM 请求已开始产出正文，中断键从此刻起生效")

    def create_stop_callback(self) -> 'threading.Event':
        """
        为新的 LLM 任务创建独立的停止事件

        Returns:
            停止事件对象
        """
        stop_event = threading.Event()
        self._thread_local.stop_event = stop_event
        return stop_event

    def on_stop_pressed(self):
        """用户按下中断键时的回调

        两层闸门，缺一不可（2026-09-12 定）：

        ① 作用域闸门——没有请求在跑时直接忽略。中断键是全局热键，绝大多数按下
           都与本程序无关，不该在这里留下任何状态。
        ② 起效闸门——请求在跑、但一个正文字符都还没产出时不作中断。用户屏幕上
           此时空无一物（本地 Toast 还没弹、OpenMate 还没收到任何分片），这一下
           不可能是冲着「停下正在显示的回复」来的。放行的后果是整段回复被静默
           掐掉：`llm_processor` 的空回复告警带 `not stopped` 条件会被抑制，
           输出侧的中断分支又在此之前不打日志，最终只剩控制台一行
           「（空回复，详见日志）」而日志里什么都没有——2026-09-12 的实际故障。

        代价：某次请求彻底卡住、一个字都不出时不能再用中断键提前掐掉，只能等
        角色的 request_timeout（两套后端均为 60s）。那种状态下按键本来也没有
        任何反馈，换来的是「回复凭空消失」不再发生。同样被接受的：粘贴模式在首个
        分片到达前按中断键不起作用；提醒分析是非流式的，从不武装起效闸门，因此
        不响应中断键（见 reminder/service.py 里 should_stop 那处注释）。

        新增流式路径或输出模式时，stopped 分支要自己留一条 INFO 终局记录：
        llm_processor 的空回复告警带 not stopped 条件，中断时会被抑制。
        """
        with self._state_lock:
            active, started = self._request_active, self._output_started

        if not active:
            # 与本程序无关的按键占绝大多数，压到 DEBUG 免得刷满日志
            logger.debug("检测到中断键，但当前没有进行中的 LLM 请求，忽略")
            return
        if not started:
            logger.info("检测到中断键，但本次 LLM 请求尚未产出任何正文，不作中断")
            return

        logger.info("检测到中断键，停止 LLM 输出")
        self._stop_event.set()

        # 如果有 toast，关闭它
        try:
            from core.ui.toast import ToastMessageManager
            toast_manager = ToastMessageManager()
            # 注意：close_last_toast() 方法目前未实现，此处保留逻辑结构
        except Exception as e:
            logger.warning(f"关闭 toast 失败: {e}")

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
