# coding: utf-8
"""
热词管理模块

提供 HotwordManager 类用于管理热词的加载、替换和文件监视。
"""

from __future__ import annotations

import threading
import unicodedata
from pathlib import Path
from typing import Dict, Callable, Optional, TYPE_CHECKING, Any, Tuple

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from rich.console import Console

from core.hotword_sync import CLIENT_FILES, read_text, validate
from .hot_rule import RuleCorrector
from .hot_phoneme import PhonemeCorrector

# 尝试导入主项目的统一组件，失败则使用本地默认值（独立运行模式）
try:
    from config_client import ClientConfig
    HOT_THRESH = ClientConfig.hot_thresh
    HOT_SIMILAR = ClientConfig.hot_similar
except ImportError:
    HOT_THRESH = 0.8
    HOT_SIMILAR = 0.6

from . import logger

try:
    from core.client.state import console
except ImportError:
    try:
        from rich.console import Console
        console = Console(highlight=False)
    except ImportError:
        class MockConsole:
            def print(self, *args, **kwargs): 
                # rich 格式清洗略去，直接打印内容
                msg = str(args[0]) if args else ""
                print(msg.replace('[cyan]', '').replace('[/]', '').replace('[green4]', ''))
            def line(self): print()
        console = MockConsole()



class HotwordManager:
    """热词管理器：负责资源协调、热词文件加载与动态监控"""

    def __init__(self,
                 hotword_files: Optional[Dict[str, Path]] = None,
                 threshold: float = 0.7,
                 similar_threshold: Optional[float] = None):
        """
        初始化
        Args:
            hotword_files: 文件映射 {'hot': Path, 'rule': Path}
            threshold: 纠错阈值
            similar_threshold: 相似度阈值
        """
        self.files = hotword_files or {
            'hot': Path('hot.txt'),
            'rule': Path('hot-rule.txt'),
        }

        self.threshold = threshold
        self.similar_threshold = similar_threshold

        # 初始化各个组件
        self.phoneme_corrector = PhonemeCorrector(threshold=threshold, similar_threshold=similar_threshold)
        self.rule_corrector = RuleCorrector()
        
        self._observer: Optional[Observer] = None
        self._is_watcher_started = False
        self.synchronizer = None
        self._handler = None

        

    def _get_display_width(self, text: str) -> int:
        """计算字符串的显示宽度（考虑中文字符占2个单位）"""
        width = 0
        for char in text:
            if unicodedata.east_asian_width(char) in ('W', 'F', 'A'):
                width += 2
            else:
                width += 1
        return width

    def _format_msg(self, label: str, filename: str, count: int) -> str:
        """格式化对齐消息"""
        w = self._get_display_width(label)
        padding1 = " " * max(0, 8 - w)
        w2 = self._get_display_width(filename)
        padding2 = " " * max(0, 16 - w2)
        return f"[bold cyan]{label}{padding1}：[/][cyan]{filename}{padding2}[/] 已更新[green]{count:3d}[/]条"

    def load_all(self) -> None:
        """初次加载所有资源"""
        logger.info("正在加载热词资源...")
        self._load_hot()
        self._load_rule()
        logger.info("热词资源加载完成")

    def _read_file(self, key: str) -> Optional[str]:
        """读取文件的统一辅助函数"""
        path = self.files.get(key)
        if not path: return ""
        try:
            if not path.exists():
                # 缺失则创建空文件
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# 热词文件单行一个\n", encoding='utf-8')
                return ""
            content = read_text(path)
            validate(path.name, content)
            return content
        except Exception as e:
            logger.warning("热词文件加载失败，暂未同步，修正并保存后重试（%s）：%s", path, e)
            return None

    def _load_hot(self) -> bool:
        content = self._read_file('hot')
        if content is None: return False
        num = self.phoneme_corrector.update_hotwords(content)
        console.print(self._format_msg("热词库", "hot.txt", num))
        return True

    def _load_rule(self) -> bool:
        content = self._read_file('rule')
        if content is None: return False
        num = self.rule_corrector.update_rules(content)
        console.print(self._format_msg("规则库", "hot-rule.txt", num))
        return True

    def get_phoneme_corrector(self) -> PhonemeCorrector:
        return self.phoneme_corrector

    def get_rule_corrector(self) -> RuleCorrector:
        return self.rule_corrector

    def start(self) -> None:
        """开启热词服务：加载资源并启动文件监视"""
        self.load_all()
        self.start_file_watcher()

    def stop(self) -> None:
        """关闭热词服务：停止文件监视"""
        self.stop_file_watcher()

    def start_file_watcher(self) -> Any:
        """启动文件监视"""
        if self._observer: return
        
        self._observer = Observer()
        handler = self._handler = _HotwordFileHandler(self)
        
        # 监视每一个文件所在的目录 (去重后监听)
        watched_dirs = {p.parent.absolute() for p in self.files.values()}
        if self.synchronizer is not None:
            watched_dirs.add(self.synchronizer.directory.absolute())
        for d in watched_dirs:
            self._observer.schedule(handler, path=str(d), recursive=False)
            
        self._observer.start()
        self._is_watcher_started = True
        logger.debug(f"已启动热词文件监视: {watched_dirs}")
        return self._observer

    def stop_file_watcher(self) -> None:
        """停止文件监视"""
        if self._is_watcher_started and self._observer:
            if self._handler is not None:
                self._handler.stop()
            self._observer.stop()
            self._observer.join()
            self._is_watcher_started = False
            self._observer = None
            logger.debug("热词文件监视已停止")


class _HotwordFileHandler(FileSystemEventHandler):
    """按文件独立防抖；编辑器的创建、保存和原子替换均使用同一事件路径。"""

    _debounce_delay = 0.4

    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self._timers = {}
        self._lock = threading.Lock()
        self._stopped = False
        self._file_mapping = {
            manager.files['hot'].absolute(): manager._load_hot,
            manager.files['rule'].absolute(): manager._load_rule,
        }
        if manager.synchronizer is not None:
            for name in CLIENT_FILES:
                self._file_mapping[manager.synchronizer.conflict_path(name).absolute()] = None

    def on_modified(self, event):
        self._event(event.src_path, event.is_directory)

    def on_created(self, event):
        self._event(event.src_path, event.is_directory)

    def on_moved(self, event):
        self._event(event.dest_path, event.is_directory)

    def _event(self, filename, is_directory):
        path = Path(filename).absolute()
        if is_directory or path not in self._file_mapping:
            return
        with self._lock:
            if self._stopped:
                return
            previous = self._timers.pop(path, None)
            if previous:
                previous.cancel()
            timer = threading.Timer(self._debounce_delay, self._reload, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _reload(self, path):
        with self._lock:
            if self._stopped:
                return
            self._timers.pop(path, None)
        try:
            loader = self._file_mapping[path]
            # 加载失败已提示，本次保存不再触发同步侧的重复校验和报错。
            if loader and loader() is False:
                return
            if self.manager.synchronizer is not None:
                self.manager.synchronizer.changed(path)
        except Exception as error:
            logger.warning('热词保存事件处理失败：%s', error)

    def stop(self):
        with self._lock:
            self._stopped = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
