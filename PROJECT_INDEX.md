# CapsWriter Offline Client — 项目索引

> 本文档面向 AI Agent，作为快速理解项目结构的入口。修改代码前先读此文档，再按需读取相关源文件。

## 项目概览

CapsWriter-Offline 是一个 Windows 离线语音输入工具（GitHub: HaujetZhao/CapsWriter-Offline）。本仓库是 Client 端的 Release 目录，附带 Server 端源码（`core/server/`）以供参考——但 `config_server.py` 和 `start_server.exe` **不在本目录**，服务端配置无法在此修改。整体由 Python 编写，使用 asyncio 事件循环、pynput 键盘/鼠标钩子、WebSocket 通信、sounddevice 音频采集。

**核心流程**：用户按住快捷键 → 客户端录音 → 音频通过 WebSocket 发送到 Server → Server 用 ASR 引擎识别 → 结果返回客户端 → 客户端执行热词替换/LLM 润色 → 模拟键盘打字输出到当前窗口。

---

## 目录结构总览

```
├── config_client.py          # 客户端配置（快捷键、服务端地址、功能开关等）
├── core/
│   ├── __init__.py            # 初始化 colorama，导出 logger
│   ├── constants.py           # 音频格式常量（采样率 16kHz、float32）
│   ├── logger.py              # 日志系统（TruncatingFileHandler + RichHandler）
│   ├── protocol.py            # C/S 通信协议数据类（AudioMessage, RecognitionMessage）
│   ├── client/                # ======= 客户端模块 =======
│   │   ├── app.py             # 门面类 CapsWriterClient（唯一入口 start()）
│   │   ├── state.py           # ClientState dataclass（全局状态容器）
│   │   ├── audio/             # 音频子系统
│   │   ├── shortcut/          # 快捷键子系统（核心交互逻辑）
│   │   ├── connection/        # WebSocket 连接管理
│   │   ├── output/            # 识别结果处理与文本输出
│   │   ├── hotword/           # 热词系统（音素 RAG + 正则规则）
│   │   ├── llm/               # LLM 润色系统（多角色、流式输出）
│   │   ├── clipboard/         # 剪贴板工具
│   │   ├── companion/          # Companion WebSocket 服务器（Electron 伴侣应用通信）
│   │   ├── diary/             # 日记归档
│   │   ├── transcribe/        # 文件转录
│   │   ├── udp/               # UDP 控制与广播
│   │   ├── global_hotkey/     # 全局快捷键（pynput GlobalHotKeys）
│   │   ├── manager/           # 运行器（MicRunner, FileRunner, TrayManager）
│   │   └── ui/                # 客户端 UI（录音指示器、启动提示）
│   ├── server/                # ======= 服务端模块 =======
│   │   ├── app.py             # 门面类 CapsWriterServer
│   │   ├── state.py           # ServerState / WorkerState
│   │   ├── schema.py          # Task / Result / RecognitionSession 数据类
│   │   ├── connection/        # WebSocket 服务端（ws_recv, ws_send）
│   │   ├── engines/           # ASR 引擎（SenseVoice, Paraformer, FunASR, Qwen）
│   │   ├── worker/            # 识别子进程（Worker, Pipeline, TaskHandler）
│   │   ├── formatter/         # 文本格式化（标点、空格）
│   │   ├── merger/            # 分段文本拼接算法
│   │   └── ui/                # 服务端托盘
│   ├── tools/                 # 通用工具
│   │   ├── chinese_itn/       # 中文逆文本正则化
│   │   ├── zhconv/            # 繁简转换
│   │   ├── window_detector.py # 前台窗口检测
│   │   ├── my_status.py       # Rich Status 增强（控制台动画）
│   │   ├── signal_handler.py  # 退出信号处理
│   │   └── ...
│   └── ui/                    # 通用 UI 组件（Toast 弹窗、系统托盘）
├── LLM/                       # LLM 角色定义（.py 文件，每个文件=一个角色）
├── hot.txt                    # 热词列表
├── hot-rule.txt               # 正则替换规则
├── hot-server.txt             # 服务端热词（部分引擎支持）
├── internal/                  # 内嵌的 Python 3.8 运行时与第三方库（供 start_client.exe 使用）
├── assets/                    # 图标等静态资源
├── logs/                      # 运行日志（自动生成）
├── docs/                      # 用户文档 + 开发笔记（DEV_NOTES.md）
├── start_client.exe           # 客户端启动器
└── 2026/                      # 日记归档目录（自动生成）
```

---

## 核心模块详解

### 1. 入口与生命周期

| 文件 | 职责 |
|---|---|
| `config_client.py` | 所有客户端配置项。快捷键列表 `shortcuts`、服务端地址、LLM 开关、热词阈值、粘贴模式等 |
| `core/client/app.py` | **CapsWriterClient** 门面类。`__init__` 实例化所有子系统，`start()` 根据命令行参数选择 MicRunner 或 FileRunner |
| `core/client/state.py` | **ClientState** dataclass。共享状态：`queue_in`（音频队列）、`websocket`、`recording`、`audio_files` 等 |
| `core/client/manager/mic_runner.py` | 麦克风模式运行器。启动顺序：托盘→提示→音频流→快捷键→UDP→热词→LLM→Companion→ResultProcessor |
| `core/client/manager/file_runner.py` | 文件转录模式运行器。遍历文件，对媒体文件调用 FileTranscriber，对文本文件调用 SrtAdjuster |

### 2. 快捷键子系统 (`core/client/shortcut/`)

这是用户交互的核心，使用 pynput 的 `win32_event_filter` 低层钩子。

| 文件 | 职责 |
|---|---|
| `shortcut_config.py` | **Shortcut** dataclass。字段：`key`、`type`(keyboard/mouse)、`suppress`(阻塞)、`hold_mode`(长按/单击)、`enabled` |
| `key_mapper.py` | VK 码 ↔ 按键名称转换。`vk_to_name()` 用于钩子回调识别按键，`name_to_key()` 用于补发时获取 pynput Key 对象。包含 Windows 消息常量定义 |
| `shortcut_manager.py` | **ShortcutManager**。创建 keyboard/mouse Listener，在 `win32_event_filter` 中处理按键事件。包含防自捕获逻辑（`_emulating_keys`、`_restoring_keys`） |
| `event_handler.py` | **ShortcutEventHandler**。处理 keydown/keyup 逻辑。hold_mode：按下→launch()，松开→判断时长→finish()/cancel()+补发。click_mode：单击切换录音 |
| `emulator.py` | **ShortcutEmulator**。使用 pynput Controller 补发被阻塞的按键/鼠标点击。`_emulating_keys` 标志防止补发的按键被自身钩子再次捕获 |
| `task.py` | **ShortcutTask**。单个快捷键的录音任务状态。`launch()`：记录时间→设状态→显示 UI→通知 Companion→启动 AudioRecorder。`finish()`：通知 Companion 结束录音→停止录音。`cancel()`：取消录音 |

**数据流**：`win32_event_filter` → `ShortcutEventHandler.handle_keydown/up` → `ShortcutTask.launch/cancel/finish` → `AudioRecorder.record_and_send`

### 3. 音频子系统 (`core/client/audio/`)

| 文件 | 职责 |
|---|---|
| `stream.py` | **AudioStreamManager**。使用 sounddevice 创建 48kHz InputStream，回调中将音频数据放入 `state.queue_in` |
| `recorder.py` | **AudioRecorder**。从 `queue_in` 读取数据，通过 WebSocket 发送到服务端。支持本地保存（MP3/WAV）。协议使用 `AudioMessage` |
| `file_manager.py` | **AudioFileManager**。管理录音文件的创建（MP3 via FFmpeg / WAV）、写入、关闭、按识别文本重命名 |

### 4. 连接与通信 (`core/client/connection/`)

| 文件 | 职责 |
|---|---|
| `websocket_manager.py` | **WebSocketManager**。管理 WebSocket 连接的建立、重连、消息收发。使用 `core/protocol.py` 定义的 `AudioMessage`/`RecognitionMessage` |

**协议**：
- 客户端 → 服务端：`AudioMessage`（task_id, source, base64 音频数据, is_final, 分段参数）
- 服务端 → 客户端：`RecognitionMessage`（task_id, is_final, text, text_accu, tokens, timestamps）

### 5. 结果处理与输出 (`core/client/output/`)

| 文件 | 职责 |
|---|---|
| `result_processor.py` | **ResultProcessor**。主工作循环：连接 WebSocket → 接收消息 → 热词替换 → 去标点 → 正则替换 → LLM 润色 → 文本输出 → 日记/音频保存 |
| `text_output.py` | **TextOutput**。两种输出方式：`_type_text()`（keyboard.write 模拟打字）和 `_paste_text()`（Ctrl+V 粘贴）。`strip_punc()` 去除末尾标点 |

### 6. 热词系统 (`core/client/hotword/`)

| 文件 | 职责 |
|---|---|
| `manager.py` | **HotwordManager**。加载 hot.txt/hot-rule.txt，启动文件监视（watchdog），热更新 |
| `hot_phoneme.py` | **PhonemeCorrector**。基于音素编辑距离的模糊匹配。两阶段：FastRAG 粗筛 → AccuRAG 精确计算 |
| `hot_rule.py` | **RuleCorrector**。基于正则表达式的强制替换规则 |
| `algo_phoneme.py` | 拼音/音素提取工具 |
| `algo_calc.py` | 模糊匹配算法（编辑距离、子串匹配） |
| `rag_fast.py` / `rag_fast_rf.py` / `rag_accu.py` | RAG 检索实现（倒排索引 + Numba JIT 加速） |

### 7. LLM 润色系统 (`core/client/llm/`)

| 文件 | 职责 |
|---|---|
| `llm_handler.py` | **LLMHandler** 协调器。`process_and_output()`：角色检测 → Companion 转发（若已连接则跳过 LLM）→ LLM 调用 → 输出。管理角色加载、上下文、客户端池 |
| `llm_role_config.py` | **RoleConfig** dataclass。角色配置字段：provider、model、system_prompt、output_mode(typing/toast)、Toast 样式（字体、颜色、透明度、圆角）等 |
| `llm_role_loader.py` | **RoleLoader**。扫描 `LLM/` 目录下的 .py 文件，每个文件作为一个角色模块加载 |
| `llm_role_detector.py` | **RoleDetector**。根据输入文本前缀匹配角色（如 "翻译 xxx" → 翻译角色） |
| `llm_processor.py` | **LLMProcessor**。执行 LLM API 调用（支持 OpenAI/Ollama/DeepSeek 等），流式输出 |
| `llm_client_pool.py` | **ClientPool**。按 provider+url 缓存 OpenAI 客户端实例 |
| `llm_message_builder.py` | 构建发送给 LLM 的消息列表（system prompt + 热词 + 选中文字 + 用户输入） |
| `llm_context.py` | **ContextManager**。管理对话历史记录 |
| `llm_output_typing.py` | typing 模式输出（直接模拟打字/粘贴） |
| `llm_output_toast.py` | toast 模式输出（弹窗显示，支持 Markdown 渲染，并按角色配置传入字体、颜色、透明度、圆角等样式） |
| `llm_output_openmate.py` | OpenMate 桌面看板输出。toast 角色在探测到 OpenMate 以 external 模式运行（`GET /health`）时改由其展示：未配置目标角色时按 `GET /v1/presentation` 解析当前生效角色（每次都查），自动获取该角色情绪状态列表（`GET /v1/characters/{id}/states`，带缓存）并注入 `<state:情绪ID>` 标签指令、后台线程聚合分片发送（流/队列固定到已注入状态的角色）、ESC 中断映射为取消、OpenMate 中途失效时剥离标签回退本地 Toast。角色开启 `enable_result_splitter` 时走 open 分段队列（`POST /v1/messages` + `PATCH` 追加）：回复按 `result_split_delimiter` 切成多条消息逐条展示，边生成边追加；否则走流式正文。接管确认后本地「正在处理结果」悬浮提示移交为 OpenMate 的 typing 指示（`PUT /v1/presentation/typing`，开启失败则保留本地提示）。API 文档见 OpenMate/docs/api/external-http-api.md |
| `llm_stop_monitor.py` | **StopMonitor**。监听 ESC 键中断 LLM 输出 |
| `llm_watcher.py` | 监视 LLM/ 目录变化，自动重载角色 |
| `llm_get_selection.py` | 通过 Ctrl+C 获取选中文字 |

**角色文件格式**（`LLM/*.py`）：每个文件定义一组变量（name、provider、model、system_prompt 等），由 RoleLoader 自动加载。当前内置角色文件为 `default.py`、`翻译.py`、`助理.py`。

### 8. UI 子系统

| 文件 | 职责 |
|---|---|
| `core/client/ui/__init__.py` | 客户端 UI 门面。注入 logger，重新导出 toast/tray/recording_indicator |
| `core/client/ui/recording_indicator.py` | **RecordingIndicator**。录音悬浮指示器（Tk 实现，单例，线程安全）。显示录音时长、等待状态、处理进度、错误提示。通过 Windows API 获取输入光标位置定位。状态优先级：录音进行中时服务端分段进度只记录不切换模式（见约定 11） |
| `core/client/ui/tips.py` | **TipsDisplay**。启动时的 Rich 控制台提示信息 |
| `core/ui/toast.py` | Toast 浮动消息通知（Tk 实现，支持 Markdown 渲染） |
| `core/ui/toast_base.py` | Toast 窗口基类。负责置顶无边框窗口、透明度、Canvas 透明色圆角背景、拖动、滚轮、复制、Markdown 最终视图字体/tag 样式同步，以及文字区域选择和空白/边缘区域拖动的事件隔离 |
| `core/ui/toast_text.py` | 基于 Text 的 Toast 窗口。用于 LLM 流式输出，支持增量更新、自动增高、选中文字和复制 |
| `core/ui/toast_label.py` | 基于 Label 的 Toast 窗口。用于普通提示，支持自动换行、自动增高和 Markdown 转换 |
| `core/ui/tray.py` | 系统托盘图标（pystray） |
| `core/ui/toast_manager.py` | Toast 窗口管理器。`ToastMessage` 支持字体、颜色、持续时间、Markdown、可编辑、透明度和圆角配置 |

### 9. 其他客户端模块

| 文件 | 职责 |
|---|---|
| `core/client/clipboard/clipboard.py` | 剪贴板操作工具（safe_paste, safe_copy, paste_text） |
| `core/client/diary/diary_writer.py` | **DiaryWriter**。按日期归档识别结果到 Markdown 文件（YYYY/MM/DD.md） |
| `core/client/transcribe/file_transcriber.py` | **FileTranscriber**。文件转录：FFmpeg 提取音频 → 分块发送 → 接收结果 → 热词替换 → 保存 |
| `core/client/transcribe/srt_adjuster.py` | **SrtAdjuster**。调整 SRT 字幕时间轴 |
| `core/client/udp/udp_control.py` | **UDPController**。监听 UDP 端口，接收 START/STOP 命令控制录音 |
| `core/client/udp/udp_broadcaster.py` | 将识别结果通过 UDP 广播到配置的目标 |
| `core/client/companion/companion_server.py` | **CompanionServer**。Companion WebSocket 服务器，管理与 Electron 伴侣应用的连接。当有客户端连接时，CapsWriter 跳过本地 LLM，将去前缀后的文本直接转发给 Electron |
| `core/client/companion/companion_protocol.py` | Companion 通信协议。定义出站事件类型（`EventType`）和 JSON 消息构造函数（`make_event`） |
| `core/client/global_hotkey/global_hotkey.py` | **GlobalHotkeyManager**。使用 pynput GlobalHotKeys 实现全局快捷键（单例） |

---

## 服务端模块概要

服务端运行在独立进程中，通过多进程 Queue 与 WebSocket 服务通信。

| 模块/文件 | 职责 |
|---|---|
| `core/server/app.py` | **CapsWriterServer** 门面类。管理 ProcessManager + SocketManager + TrayManager |
| `core/server/state.py` | **ServerState**（主进程：sockets 字典、Queue）、**WorkerState**（子进程：sessions、GPU 加速状态） |
| `core/server/schema.py` | **Task**（音频任务）、**Result**（识别结果）、**RecognitionSession** 数据类 |
| `core/server/connection/` | WebSocket 服务端。`ws_recv` 处理客户端连接和消息接收，`ws_send` 从 `queue_out` 取结果发回客户端 |
| `core/server/worker/worker.py` | **RecognizerWorker**。识别子进程入口，加载模型 → 进入任务循环 |
| `core/server/worker/pipeline.py` | **TaskPipeline**。ASR 推理流水线：音频预处理 → 引擎推理 → 文本拼接 → 对齐增强 → 格式化 |
| `core/server/worker/task_handler.py` | **TaskHandler**。任务调度器，支持跨 socket 公平轮转 |
| `core/server/engines/` | ASR 引擎抽象与实现 |
| `core/server/engines/base.py` | 基类：**BaseASREngine**、**BasePuncEngine**、**BaseAlignEngine** |
| `core/server/engines/factory.py` | **EngineFactory**。延迟加载，支持 sensevoice / paraformer / fun_asr_nano / qwen_asr |
| `core/server/engines/manager.py` | **ManagedAlignerProxy**。对齐引擎懒加载与闲置卸载 |
| `core/server/engines/sensevoice_onnx/` | SenseVoice ONNX 引擎 |
| `core/server/engines/paraformer_onnx/` | Paraformer ONNX 引擎 |
| `core/server/engines/fun_asr_gguf/` | FunASR GGUF 引擎（llama.cpp 推理） |
| `core/server/engines/qwen_asr_gguf/` | Qwen ASR GGUF 引擎 |
| `core/server/engines/force_aligner_gguf/` | 强制对齐引擎（字级时间戳补齐） |
| `core/server/formatter/text_formatter.py` | 文本格式化（标点注入、空格调整） |
| `core/server/merger/` | 分段文本拼接算法（简单拼接 + Token 级精确拼接） |

---

## 通用工具 (`core/tools/`)

| 文件 | 职责 |
|---|---|
| `my_status.py` | Rich Status 增强，支持 start()/stop() 幂等调用。`start()` 会创建新线程 |
| `window_detector.py` | 检测前台窗口信息（进程名、标题），用于粘贴模式和自动回车的应用匹配 |
| `signal_handler.py` | 注册 SIGINT/atexit 退出回调 |
| `chinese_itn/` | 中文逆文本正则化（数字、单位转换） |
| `zhconv/` | 繁简中文转换 |
| `token_sync.py` | 将格式化后的文本变化同步回 token 序列 |
| `srt_from_txt.py` | 从文本/JSON 生成 SRT 字幕 |
| `format_tools.py` | 文本格式化工具 |
| `punc_converter.py` | 标点转换工具 |
| `daemon_executor.py` | 守护线程执行器 |
| `empty_working_set.py` | Windows 物理内存清理 |
| `clean_assets.py` | 清理临时音频文件 |

---

## 关键数据流

### 麦克风实时识别
```
用户按下快捷键
  → win32_event_filter (shortcut_manager.py)
  → ShortcutEventHandler.handle_keydown (event_handler.py)
  → ShortcutTask.launch (task.py)
    → state.start_recording()
    → show_recording_indicator()
    → AudioRecorder.record_and_send() [async]

音频流回调 (stream.py)
  → state.queue_in.put(data)

AudioRecorder (recorder.py)
  → 从 queue_in 读取数据
  → base64 编码 → AudioMessage → WebSocket 发送

用户松开快捷键
  → ShortcutEventHandler.handle_keyup
  → 判断时长 > threshold?
    → 是: task.finish() → state.queue_in.put(finish)
    → 否: task.cancel() → emulator.emulate_key() (补发按键)

服务端返回结果
  → ResultProcessor._handle_message (result_processor.py)
  → 热词替换 (PhonemeCorrector + RuleCorrector)
  → 去标点 (TextOutput.strip_punc)
  → LLM 润色 (LLMHandler.process_and_output, 可选)
  → 文本输出 (keyboard.write 或 Ctrl+V 粘贴)
  → 日记保存 + 音频重命名
```

### 文件转录
```
命令行传入文件路径
  → FileRunner.run()
  → FileTranscriber.check() → 检查 FFmpeg + WebSocket 连接
  → FileTranscriber.send() → FFmpeg 提取音频 → 分块发送
  → FileTranscriber.receive() → 接收结果 → 热词替换
  → ResultHandler.save_results() → 保存 SRT/TXT/JSON
```

### Companion 模式（Electron 伴侣应用连接时）
```
Electron 应用连接到 ws://127.0.0.1:6019
  → CompanionServer._handler() 注册客户端
  → 发送 {"type": "status", "state": "connected"}

用户按下快捷键
  → ShortcutTask.launch()
    → CompanionServer.emit_recording_start_threadsafe()
    → 广播 {"type": "recording_start"}

服务端返回识别结果
  → ResultProcessor → 热词替换 → LLMHandler.process_and_output()
  → RoleDetector 检测角色前缀（如 "助理 xxx"）
  → 检测到 companion 已连接 → 跳过本地 LLM
  → CompanionServer.emit_user_message(content, role)
    → 广播 {"type": "user_message", "text": "xxx", "role": "助理"}
  → 返回 LLMResult（不执行打字/Toast 输出）

用户松开快捷键
  → ShortcutTask.finish()
    → CompanionServer.emit_recording_end_threadsafe()
    → 广播 {"type": "recording_end"}

注意：无角色前缀的普通文本仍走常规打字输出路径，不经过 Companion。
```

---

## 关键设计模式与约定

1. **门面模式**：`CapsWriterClient` 和 `CapsWriterServer` 是各自模块的唯一入口
2. **线程模型**：主线程运行 asyncio 事件循环；pynput 钩子在独立线程；UI（Tk）在独立线程；ThreadPoolExecutor 处理阻塞操作
3. **防自捕获**：emulator 补发按键时设置 `_emulating_keys` 标志，钩子回调检测到该标志后放行不处理
4. **单例模式**：RecordingIndicator、GlobalHotkeyManager 使用线程安全单例
5. **配置方式**：用户通过修改 `config_client.py` 顶层变量配置，无 YAML/JSON（`config_server.py` 属于服务端发行目录，不在本仓库）
6. **LLM 角色**：`LLM/` 目录下每个 .py 文件是一个角色，定义 RoleConfig 字段作为模块级变量；toast 输出角色可配置 `toast_font_family`、`toast_font_size`、`toast_font_color`、`toast_bg_color`、`toast_window_alpha`、`toast_corner_radius` 等样式字段
7. **热词文件**：`hot.txt` 每行一个热词（音素模糊匹配），`hot-rule.txt` 每行 `正则 = 替换` 格式
8. **Companion 模式**：当 Electron 伴侣应用连接到 CompanionServer 时，匹配角色前缀的文本跳过本地 LLM，直接转发给 Electron 处理
9. **多 Tk 解释器与字体约束**：录音指示器与 Toast 弹窗分别在独立线程运行各自的 Tk 解释器；创建 `tkinter.font.Font` 时必须显式传 `root=` 绑定所属窗口的解释器，否则命名字体会建到进程内第一个 Tk（`_default_root`）上，跨解释器引用时字体名会被当作不存在的字体家族，静默回退为小号宋体
10. **OpenMate 展示**：toast 角色在 LLM 调用前探测 OpenMate（`config_client.py` 的 `openmate_*` 配置），可用则自动获取角色情绪状态列表、注入情绪标签指令（标签用状态 ID，别名作语义注解）并将流式输出转交 OpenMate 展示；`<state:...>` 标签由 OpenMate 解析剥离，本地存历史/广播前也统一剥离。OpenMate 隐藏在托盘时收到消息会自行无激活恢复显示，无需客户端干预。`openmate_character` 留空时每次请求走 `GET /v1/presentation` 解析当前生效角色，按该角色取情绪状态并把流/分段队列固定到它
11. **悬浮指示器状态优先级**：录音进行中（`mode == "recording"`）时服务端回传的分段进度只更新数据、不切换模式。麦克风边录边发，服务端每攒够 `mic_seg_duration + mic_seg_overlap * 2`（默认 68s）才切一段回传一次，切成 progress 会让实时录音计时被一个一分钟内都不变的「已处理」时长顶掉

---

## 开发环境与验证

本目录是打包后的 Release 目录，不是可直接跑测试的源码工程：

- **运行方式**：双击 `start_client.exe`，它使用 `internal/` 内嵌的 Python 3.8 运行时和依赖库
- **系统 Python 不可直接使用**：没有本项目的依赖（rich、pynput、sounddevice 等），且 `import core` 会在包 `__init__` 中连锁导入 rich 而失败；单元测试需先在 `sys.modules` 预注入桩模块（伪造 `core`、`core.client.llm` 等包及其 `logger`），再用 `importlib` 按文件路径加载目标模块
- **验证手段从轻到重**：`ast.parse` 语法检查 → 桩模块单元测试 → `start_client.exe` 实机验证
- **严禁提交 pyc**：`__pycache__` 不得进仓库（历史上误提交过，见提交 71103d3）；语法检查用 `ast.parse` 而非 `py_compile`，避免生成缓存文件
- **文档同步**：行为变更需同步更新本文档相关章节；跨会话的待办、决策与已知怪状态记录在 [docs/DEV_NOTES.md](docs/DEV_NOTES.md)

---

## 常见修改指南

| 修改目标 | 涉及文件 |
|---|---|
| 添加/修改快捷键 | `config_client.py` (shortcuts 列表) |
| 修改快捷键行为逻辑 | `shortcut/event_handler.py`, `shortcut/task.py` |
| 修改按键映射 | `shortcut/key_mapper.py` |
| 修改按键补发逻辑 | `shortcut/emulator.py` |
| 修改钩子事件处理 | `shortcut/shortcut_manager.py` |
| 修改录音/音频发送 | `audio/recorder.py`, `audio/stream.py` |
| 修改识别结果处理 | `output/result_processor.py` |
| 修改文本输出方式 | `output/text_output.py` |
| 添加 LLM 角色 | 在 `LLM/` 目录新建 .py 文件 |
| 修改 LLM 处理逻辑 | `llm/llm_handler.py`, `llm/llm_processor.py` |
| 修改热词匹配 | `hotword/hot_phoneme.py` |
| 修改 UI 悬浮提示 | `core/client/ui/recording_indicator.py` |
| 修改 Toast 弹窗 | `core/ui/toast.py`, `core/ui/toast_manager.py`, `core/ui/toast_base.py`, `core/ui/toast_text.py`, `core/ui/toast_label.py` |
| 修改系统托盘 | `core/ui/tray.py`, `core/client/manager/tray_manager.py` |
| 修改 ASR 引擎 | `core/server/engines/` 对应子目录 |
| 修改识别流水线 | `core/server/worker/pipeline.py` |
| 修改文本拼接算法 | `core/server/merger/` |
| 修改 C/S 通信协议 | `core/protocol.py` + 两端收发代码 |
| 修改 Companion 通信协议 | `core/client/companion/companion_protocol.py` |
| 修改 Companion 服务器行为 | `core/client/companion/companion_server.py` |
| 启用/禁用 Companion | `config_client.py` (`companion_enabled`, `companion_port`) |
| 配置/修改 OpenMate 接入 | `config_client.py` (`openmate_*`), `llm/llm_output_openmate.py`；角色级覆盖字段 `openmate_enabled`/`openmate_character`/`enable_result_splitter`/`result_split_delimiter` |

---

## 外部依赖（主要）

- **pynput**: 键盘/鼠标钩子与模拟（核心交互）
- **sounddevice**: 音频输入流
- **websockets**: C/S 通信
- **keyboard**: 模拟打字输出（keyboard.write）
- **pyclip**: 剪贴板操作
- **rich**: 控制台格式化输出
- **watchdog**: 热词文件变化监视
- **tkinter**: 录音指示器和 Toast UI
- **numpy**: 音频数据处理
- **pypinyin**: 拼音提取（热词系统）
- **psutil / win32gui / win32process**: 窗口检测
- **openai**: LLM API 调用
- **onnxruntime**: ASR 模型推理（服务端）
- **FFmpeg**: 音频格式转换（外部可执行文件）
