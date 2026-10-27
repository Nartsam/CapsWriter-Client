# coding: utf-8
"""
CapsWriter Offline 客户端主程序门面类 (Facade)

采用外观模式统一管理音频流 (AudioStreamManager)、
识别结果处理 (ResultProcessor) 和快捷键管理 (ShortcutManager)。
"""

import os
import sys
import subprocess
import asyncio
from pathlib import Path

from .state import ClientState
from . import logger
from config_client import ClientConfig as Config, __version__
from core.tools.signal_handler import register_signal
from .state import console
from .connection import WebSocketManager
from typing import TYPE_CHECKING, Optional
from .manager import (
    TrayManager,
    MicRunner, FileRunner
)
from .audio.stream import AudioStreamManager
from .shortcut.shortcut_manager import ShortcutManager
from .shortcut.shortcut_config import Shortcut

from .udp.udp_control import UDPController
from .companion import CompanionServer

from .hotword.manager import HotwordManager
from .llm.llm_handler import LLMHandler
from .vision import VisionManager
from .reminder.service import ReminderService
from .output.text_output import TextOutput
from .diary.diary_writer import DiaryWriter
from core.tools.empty_working_set import empty_current_working_set
from platform import system



class CapsWriterClient:
    """
    CapsWriter 客户端门面类
    
    管理的外部接口简洁：start()。
    """
    def __init__(self):
        # 确保正确的工作目录
        self.base_dir = Path(__file__).parents[2]
        os.chdir(self.base_dir)
            
        # 初始化事件循环
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
            
        # 初始化状态容器
        self.state = ClientState(app=self)

        # 初始化热词管理器
        self.hotword = HotwordManager(
            hotword_files=None,
            threshold=Config.hot_thresh,
            similar_threshold=Config.hot_similar
        )

        # 4. 初始化 LLM 润色系统
        self.llm = LLMHandler(app=self)
        
        self.output = TextOutput()
        self.diary = DiaryWriter(base_path=self.base_dir)

        # 初始化各管理器
        self.ws = WebSocketManager(self)
        self.hotword_sync = None
        try:
            from .hotword.sync import HotwordSyncClient
            self.hotword_sync = HotwordSyncClient(self)
            self.hotword.synchronizer = self.hotword_sync
        except Exception as error:
            logger.warning('热词同步初始化失败，继续使用本机词库：%s', error)
        self.tray = TrayManager(self)

        # 实例化硬件资源管理组件
        self.stream = AudioStreamManager(self)
        self.shortcut = ShortcutManager(self, [Shortcut(**sc) for sc in Config.shortcuts])
        self.udp = UDPController(self.shortcut)
        self.companion = CompanionServer(self)
        self.vision = VisionManager(self)
        self.reminders = ReminderService(self)

        # 内存清理
        empty_current_working_set()

    def restart(self):
        if getattr(self, '_stopping', False):
            return
        self._restart_command = ([sys.executable] + sys.argv[1:] if getattr(sys, 'frozen', False)
                                 else [sys.executable, str(Path(sys.argv[0]).absolute())] + sys.argv[1:])
        self.stop()

    def stop(self):
        """
        统一释放所有资源（清理顺序：硬件 -> 托盘 -> WebSocket -> State）
        """
        if getattr(self, '_stopping', False):
            return
        self._stopping = True
        logger.info("正在执行 CapsWriterClient 资源释放...")
        processor = getattr(self, 'result_processor', None)
        if processor is not None:
            processor.request_exit()

        try:
            # 退出客户端时先隐藏悬浮提示，避免异常退出后窗口残留。
            from core.client.ui import hide_recording_indicator
            hide_recording_indicator()
        except Exception:
            pass

        # 1. 停止核心运行组件
        self.reminders.stop()
        self.companion.stop()
        self.udp.stop()
        self.shortcut.stop()
        self.stream.stop()

        # 2. 托盘资源
        self.tray.stop()

        # 3. 关闭监控
        self.hotword.stop()
        self.llm.stop()
        self.vision.stop()

        # 托盘/信号可能来自其他线程；退出的最后阶段留在事件循环里等待同步确认。
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self._finish_stop()))
        else:
            self.loop.run_until_complete(self._finish_stop())

    async def _finish_stop(self):
        try:
            if self.hotword_sync is not None:
                await self.hotword_sync.shutdown()
            await asyncio.wait_for(self.ws.close(), timeout=2)
        except Exception as error:
            logger.warning('退出连接时发生错误：%s', error)
        finally:
            try:
                self.state.reset()
                command = getattr(self, '_restart_command', None)
                if command:
                    # 旧实例的同步任务已完成/取消，再让新实例读取同一份状态。
                    subprocess.Popen(command)
            except Exception as error:
                logger.warning('退出或重启失败：%s', error)
            self.loop.stop()
        logger.info("资源释放完成")
        console.print('[green4]再见！')


    def start(self):
        """
        启动客户端 (唯一入口)
        
        自动根据命令行参数识别模式。内部管理异步循环。
        """

        # 注册退出函数
        register_signal(self.stop)

        files = [Path(f) for f in sys.argv[1:] if os.path.exists(f)]

        if files:
            # 文件转录模式
            runner = FileRunner(self, files)
        else:
            # 麦克风实时模式
            runner = MicRunner(self)
        
        try:
            self.loop.run_until_complete(runner.run())
        except RuntimeError:
            ...
