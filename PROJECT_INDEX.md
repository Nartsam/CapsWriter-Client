# CapsWriter Offline Client — 项目索引

> 本文档面向 AI Agent，作为快速理解项目结构的入口。修改代码前先读此文档，再按需读取相关源文件。

## 项目概览

CapsWriter-Offline 是一个 Windows 离线语音输入工具（GitHub: HaujetZhao/CapsWriter-Offline）。本仓库是 Client 端的 Release 目录，附带 Server 端源码（`core/server/`）以供参考——但 `config_server.py` 和 `start_server.exe` **不在本目录**，服务端配置无法在此修改。整体由 Python 编写，使用 asyncio 事件循环、pynput 键盘/鼠标钩子、WebSocket 通信、sounddevice 音频采集。

**核心流程**：用户按住快捷键 → 客户端录音 → 音频通过 WebSocket 发送到 Server → Server 用 ASR 引擎识别 → 结果返回客户端 → 客户端执行热词替换/LLM 润色 → 模拟键盘打字输出到当前窗口。

**便携性是硬约束**：新增代码、依赖、配置与辅助工具均须留在 Client 目录内，不修改外部环境。不得以安装目标程序扩展、修改其设置或启动参数、安装全局依赖或注册系统服务来解决兼容性问题；选区读取同样遵守此约束（2026-09-15 用户确认）。

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
│   │   ├── vision/            # 视觉输入（剪贴板图片 → 随下一句上传）
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
│   ├── backends.py            # 角色的后端切换（DeepSeek 直连 / CPA 局域网网关），见约定 23
│   └── secrets.py             # API 密钥（未被 git 跟踪）
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
| `core/client/state.py` | **ClientState** dataclass。共享状态：`queue_in`（音频队列）、`websocket`、`recording`、`audio_files` 等；`pending_task_id`（当前可打断的任务）与 `cancelled_task_ids`（已打断待丢弃结果的定长名单，见约定 12）；连续对话的 `continuous_role`（粘滞角色名）、`continuous_last_active`（闲置超时基准）与 `ContinuousChatSnapshot` 路由快照（`continuous_current` + 按 task_id 索引的定长字典，见约定 19）；视觉输入的 `pending_image`（待挂图片槽）与同构的图片快照 `image_current`/`image_snapshots`（见约定 21） |
| `core/client/manager/mic_runner.py` | 麦克风模式运行器。启动顺序：托盘→提示→音频流→快捷键→UDP→热词→LLM→视觉输入→Companion→ResultProcessor（视觉输入必须排在 LLM 之后：两者共用 `GlobalHotkeyManager` 单例） |
| `core/client/manager/file_runner.py` | 文件转录模式运行器。遍历文件，对媒体文件调用 FileTranscriber，对文本文件调用 SrtAdjuster |

### 2. 快捷键子系统 (`core/client/shortcut/`)

这是用户交互的核心，使用 pynput 的 `win32_event_filter` 低层钩子。

| 文件 | 职责 |
|---|---|
| `shortcut_config.py` | **Shortcut** dataclass。字段：`key`、`type`(keyboard/mouse)、`suppress`(阻塞)、`hold_mode`(长按/单击)、`enabled` |
| `key_mapper.py` | VK 码 ↔ 按键名称转换。`vk_to_name()` 用于钩子回调识别按键，`name_to_key()` 用于补发时获取 pynput Key 对象。包含 Windows 消息常量定义 |
| `shortcut_manager.py` | **ShortcutManager**。创建 keyboard/mouse Listener，在 `win32_event_filter` 中处理按键事件。包含防自捕获逻辑（`_emulating_keys`、`_restoring_keys`）、打断键处理（`_handle_cancel_key`，抑制决策锁存，见约定 12）与连续对话切换键处理（`_handle_chat_key` + `_resolve_chat_key` 的五条配置校验，见约定 19） |
| `event_handler.py` | **ShortcutEventHandler**。处理 keydown/keyup 逻辑。hold_mode：按下→launch()，松开→判断时长→finish()/cancel()+补发。click_mode：单击切换录音 |
| `emulator.py` | **ShortcutEmulator**。使用 pynput Controller 补发被阻塞的按键/鼠标点击。`_emulating_keys` 标志防止补发的按键被自身钩子再次捕获 |
| `task.py` | **ShortcutTask**。单个快捷键的录音任务状态。`launch()`：记录时间→设状态→连续对话超时懒判断与建快照（`_prepare_continuous`）→把待挂图片移入本次录音（`begin_image_snapshot`）→显示 UI→通知 Companion→启动 AudioRecorder。`finish()`：落地连续对话退出→通知 Companion 结束录音→停止录音。`cancel()`/`abort()`：取消录音，并回滚本次录音的连续对话切换与图片绑定 |

**数据流**：`win32_event_filter` → `ShortcutEventHandler.handle_keydown/up` → `ShortcutTask.launch/cancel/finish` → `AudioRecorder.record_and_send`

### 3. 音频子系统 (`core/client/audio/`)

| 文件 | 职责 |
|---|---|
| `stream.py` | **AudioStreamManager**。使用 sounddevice 创建 48kHz InputStream，回调中将音频数据放入 `state.queue_in` |
| `recorder.py` | **AudioRecorder**。从 `queue_in` 读取数据，通过 WebSocket 发送到服务端。支持本地保存（MP3/WAV）。协议使用 `AudioMessage`。收到 `abort` 任务时丢弃未发送缓存、删除录音文件，但仍静默发一个 `is_final` 冲掉服务端缓冲（见约定 12） |
| `file_manager.py` | **AudioFileManager**。管理录音文件的创建（MP3 via FFmpeg / WAV）、写入、关闭、按识别文本重命名 |

### 4. 连接与通信 (`core/client/connection/`)

| 文件 | 职责 |
|---|---|
| `websocket_manager.py` | **WebSocketManager**。管理 WebSocket 连接的建立、重连、消息收发。使用 `core/protocol.py` 定义的 `AudioMessage`/`RecognitionMessage`。`Config.addr` 支持写成候选列表：未锁定时在**一次 `connect()` 调用内**按序探测（内网/回环走 `addr_probe_timeout_lan`、其余走 `addr_probe_timeout`），首个连上的即锁定，此后含断线重连都只连它（见约定 20）。模块级 `resolve_server_endpoints()` 负责归一化，启动提示复用它 |

**协议**：
- 客户端 → 服务端：`AudioMessage`（task_id, source, base64 音频数据, is_final, 分段参数）
- 服务端 → 客户端：`RecognitionMessage`（task_id, is_final, text, text_accu, tokens, timestamps）

### 5. 结果处理与输出 (`core/client/output/`)

| 文件 | 职责 |
|---|---|
| `result_processor.py` | **ResultProcessor**。主工作循环：连接 WebSocket → 接收消息 → 热词替换 → 去标点 → 正则替换 → LLM 润色 → 文本输出 → 日记/音频保存。`_format_llm_result` 负责控制台那行 `模型结果[角色(工具)]：…`，工具名来自 `LLMResult.used_tools`；结果为空时显示黄色「（空回复，详见日志）」而不是原样打印文本（见约定 18）。`_resolve_forced_role`/`_update_continuous_state` 按本次录音的路由快照决定连续对话归属并做状态迁移（见约定 19）。`_report_image_usage` 报告本次绑定图片的去向：用掉记 INFO，丢弃打一行黄色控制台提示并说明原因（见约定 21） |
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
| `llm_handler.py` | **LLMHandler** 协调器。`process_and_output()`：角色检测 → Companion 转发（若已连接则跳过 LLM）→ LLM 调用 → 输出。传入 `forced_role` 时（连续对话）跳过角色检测、整句原样发给该角色；传入 `image_data` 时只有 `enable_vision=True` 的角色才真的带图，否则静默丢弃并在 `LLMResult.image_used` 上标记（见约定 21）。`resolve_role()` 按显示名取回启用中的具名角色，供连续对话每次请求重新解析。管理角色加载、上下文、客户端池 |
| `llm_role_config.py` | **RoleConfig** dataclass。角色配置字段：provider、model、`enable_vision`（能否接收图片，不校验 model 是否真支持视觉）、system_prompt、output_mode(typing/toast)、Toast 宽度边界与样式（字体、颜色、透明度、圆角）、显示时长（`toast_duration`，智能时长下作为最短停留保底基数，见约定 17）、联网搜索三件套（`enable_web_search`/`enable_datetime`/`request_timeout`）等 |
| `llm_role_loader.py` | **RoleLoader**。扫描 `LLM/` 目录下的 .py 文件，每个文件作为一个角色模块加载 |
| `llm_role_detector.py` | **RoleDetector**。根据输入文本前缀匹配角色（如 "翻译 xxx" → 翻译角色） |
| `llm_processor.py` | **LLMProcessor**。执行 LLM API 调用（支持 OpenAI/Ollama/DeepSeek 等），流式输出。三条流式路径：Ollama 原生流、Chat Completions 流、Responses API 流（角色开启 `enable_web_search` 时走此路，挂载服务端内置 `web_search` 工具，见约定 15）。工具事件同时触发展示占位文案与 `used_tools` 出参回传（见约定 16）。`_to_responses_content` 把内部统一的 Chat 口味多模态 content 转成 Responses 口味（`text`→`input_text`、`image_url` 嵌套对象→`input_image` + 裸字符串），纯字符串原样透传因此不带图的请求逐字不变。三条路径都带空回复诊断：Responses 流解析 `response.completed/incomplete/failed` 与 refusal，记录截断原因、usage（含 reasoning token）与事件类型计数；Chat 流记录 `finish_reason`（见约定 18）。每次请求在 INFO 打一行 `请求构成：N 条历史 + 本句，M 张图`（`_describe_messages`）——完整请求 JSON 只在 DEBUG 打，没有这行时「这次到底带没带历史/图」事后完全无从查证 |
| `llm_client_pool.py` | **ClientPool**。按 provider+url 缓存 OpenAI 客户端实例 |
| `llm_message_builder.py` | 构建发送给 LLM 的消息列表（system prompt + 对话历史 + 当前时间 + 热词 + 选中文字 + 用户输入）。历史必须走 `context_manager.get_history()`（只返回 `{role, content}` 副本）：`history` 原对象还带一个 `timestamp` 字段，而 `_build_responses_params` 对纯字符串 content 是原样透传的，那个字段会跟着上行到厂商 API |
| `llm_context.py` | **ContextManager**。管理对话历史记录。`demote_images()` 在写入新的带图消息前把历史里更早的图降级成 `［图片］` 占位符，保证历史里最多只有一张真图（见约定 21）。`get_history()` 是对外唯一出口，不要直接读 `history`（见 `llm_message_builder.py` 那行） |
| `llm_output_typing.py` | typing 模式输出（直接模拟打字/粘贴） |
| `llm_output_toast.py` | toast 模式输出（弹窗显示，支持 Markdown 渲染，并按角色配置传入字体、颜色、透明度、圆角等样式）。窗口走智能时长（`smart_duration=True`）：时长随字数对数计算、悬停不顺延且到期后移开即关、拖拽后永不自动关闭；创建新窗口前顶掉所有旧的 LLM 白色对话框（见约定 17） |
| `llm_output_openmate.py` | OpenMate 桌面看板输出。toast 角色在探测到 OpenMate 以 external 模式运行（`GET /health`）时改由其展示：未配置目标角色时按 `GET /v1/presentation` 解析当前生效角色（每次都查），自动获取该角色情绪状态列表（`GET /v1/characters/{id}/states`，带缓存）并注入 `<state:情绪ID>` 标签指令、后台线程聚合分片发送（流/队列固定到已注入状态的角色）、ESC 中断映射为取消、OpenMate 中途失效时剥离标签回退本地 Toast。角色开启 `enable_result_splitter` 时走 open 分段队列（`POST /v1/messages` + `PATCH` 追加）：回复按 `result_split_delimiter` 切成多条消息逐条展示，边生成边追加；否则走流式正文。接管确认后本地「正在处理结果」悬浮提示移交为 OpenMate 的 typing 指示（`PUT /v1/presentation/typing`，开启失败则保留本地提示）。API 文档见 OpenMate/docs/api/external-http-api.md |
| `llm_state_tags.py` | **状态标签的识别与剥离**（`STATE_TAG_RE` / `strip_state_tags` / `StateTagFilter`）。标签是**展示层**指令：对话历史存原文，本地 Toast、打字、md、UDP 在展示前才剥（见约定 10）。流式分片会把 `<state:微笑>` 切成 `<sta` + `te:微笑>`，逐片正则拦不住，故提供 `StateTagFilter`：只暂存可能还会补成标签的那截尾巴，其余立即放行 |
| `llm_stop_monitor.py` | **StopMonitor**。监听 ESC 键中断 LLM 输出。中断键是全局热键，故设两层闸门：`begin_request`/`end_request` 圈出请求作用域，`mark_output_started` 在首个正文分片（或首条工具状态占位）到达时才让按键生效（见约定 25） |
| `llm_watcher.py` | 监视 LLM/ 目录变化，自动重载角色 |
| `llm_get_selection.py` | `capture_selection` 按全局 `selection_mode` 选择只读获取、只读后回退 Ctrl+C 或直接 Ctrl+C。`SelectionContext` 分开保存本轮附加文字与终端预览，保留按角色的相邻轮次去重（见约定 26） |

**角色文件格式**（`LLM/*.py`）：每个文件定义一组变量（name、provider、model、system_prompt 等），由 RoleLoader 自动加载。当前内置角色文件为 `default.py`、`翻译.py`、`助理.py`。四个 toast 角色（助理、雾切响子、新岛真、翻译）的 API 段不再写死，改为 `backend = 'deepseek' | 'cpa'` 一行加 `resolve()` 取值，见约定 23。翻译角色开启选中文字读取：有选中文字时将其视为原文、语音输入仅作为目标语言或风格要求；无选中文字时语音输入本身为原文。未指定目标语言时，中文译为英文，其他语言译为简体中文。助理角色面向简单日常与知识问答，默认用一至三句纯文本直接作答，不输出寒暄、问题复述、无关扩展或 Markdown 等特殊格式。助理、雾切响子、新岛真三个角色开启联网搜索与当前时间注入（见约定 15），提示词均规定只有时效性问题才检索、闲聊与稳定知识不检索，且检索结果不得改变各自的输出格式（纯文本、无网址与引用编号）。

### 8. UI 子系统

| 文件 | 职责 |
|---|---|
| `core/client/ui/__init__.py` | 客户端 UI 门面。注入 logger，重新导出 toast/tray/recording_indicator |
| `core/client/ui/recording_indicator.py` | **RecordingIndicator**。录音悬浮指示器（Tk 实现，单例，线程安全）。显示录音时长、等待状态、处理进度、错误提示。通过 Windows API 获取输入光标位置定位。状态优先级：录音进行中时服务端分段进度只记录不切换模式（见约定 11）。手动打断时原地换成 `cancelled` 文案（浅蓝 `CANCEL_ACCENT`），1.2s 后自动隐藏。录音态另有连续对话四态：底色白/浅黄/浅绿区分，圆点恒为蓝，切换走独立的 `update_continuous` 命令（见约定 19）。面板宽度 `PANEL_W` 是**最小值不是固定值**：只由标题驱动、可增可减、上限为屏幕宽度的 90%（`_fit_panel_to_title`），详情行不参与测宽，测量一律用 `_compose_text(stable=True)` 把省略号动画固定成三个点 |
| `core/client/ui/tips.py` | **TipsDisplay**。启动时的 Rich 控制台提示信息 |
| `core/ui/toast.py` | Toast 浮动消息通知（Tk 实现，支持 Markdown 渲染） |
| `core/ui/toast_base.py` | Toast 窗口基类。负责置顶无边框窗口、透明度、Canvas 透明色圆角背景、顶部留白（`padding_top`：LLM 角色白框 40px 拖动区，普通 toast 15px 与底部对齐，见约定 17）、内容宽度测量、只增不减的窗口增宽、拖动锚点、滚轮、复制、Markdown 最终视图字体/tag 样式同步与按真实内容重算高度，以及文字区域选择和空白/边缘区域拖动的事件隔离。另含智能时长（`smart_duration`/`pause_and_resume` 标志）：随字数对数计时、悬停不顺延绝对截止时间且到期后移开即关（`_timer_expired`）、拖拽后永不自动关闭（`_never_auto_close`，见约定 17）；普通 toast 保持固定时长 + 悬停移开重置的旧行为 |
| `core/ui/toast_text.py` | 基于 Text 的 Toast 窗口。用于 LLM 流式输出，支持增量更新、按内容自动增宽/增高、达到最大宽度后自动换行、选中文字和复制。窗口高度 = 行数 × 行高 + 基类的 `height_padding` |
| `core/ui/toast_label.py` | 基于 Label 的 Toast 窗口。用于普通提示，支持自动换行、自动增高和 Markdown 转换。高度同样叠加基类的 `height_padding` |
| `core/ui/dpi.py` | 进程 DPI 感知声明。`SetProcessDpiAwareness` 每进程只能成功一次，历史上三处各自调用、谁先导入谁生效，现统一到此并固定 `PROCESS_SYSTEM_DPI_AWARE(1)`（等同于此前实际生效的那条，行为不变） |
| `core/ui/tray.py` | 系统托盘图标（pystray） |
| `core/ui/toast_manager.py` | Toast 窗口管理器。`ToastMessage` 支持字体、颜色、持续时间、Markdown、可编辑、透明度和圆角配置 |

### 9. 其他客户端模块

| 文件 | 职责 |
|---|---|
| `core/client/clipboard/clipboard.py` | 剪贴板操作工具（try_paste, safe_paste, safe_copy, paste_text）。`try_paste()` 能区分「读不到」（返回 None，典型是剪贴板里是图片）与「本来就是空文本」（返回 ''）；**凡是之后要把读到的内容写回剪贴板的调用方必须用它**，用 `safe_paste()` 会拿失败折叠出的空串写回去，先 EmptyClipboard 把原内容清掉 |
| `core/client/diary/diary_writer.py` | **DiaryWriter**。按日期归档识别结果到 Markdown 文件（YYYY/MM/DD.md） |
| `core/client/transcribe/file_transcriber.py` | **FileTranscriber**。文件转录：FFmpeg 提取音频 → 分块发送 → 接收结果 → 热词替换 → 保存 |
| `core/client/transcribe/srt_adjuster.py` | **SrtAdjuster**。调整 SRT 字幕时间轴 |
| `core/client/udp/udp_control.py` | **UDPController**。监听 UDP 端口，接收 START/STOP 命令控制录音 |
| `core/client/udp/udp_broadcaster.py` | 将识别结果通过 UDP 广播到配置的目标 |
| `core/client/companion/companion_server.py` | **CompanionServer**。Companion WebSocket 服务器，管理与 Electron 伴侣应用的连接。当有客户端连接时，CapsWriter 跳过本地 LLM，将去前缀后的文本直接转发给 Electron。事件：`recording_start` / `recording_end` / `recording_cancel`（作废与正常结束分开）/ `user_message` |
| `core/client/companion/companion_protocol.py` | Companion 通信协议。定义出站事件类型（`EventType`）和 JSON 消息构造函数（`make_event`） |
| `core/client/global_hotkey/global_hotkey.py` | **GlobalHotkeyManager**。使用 pynput GlobalHotKeys 实现全局快捷键（单例） |
| `core/client/vision/clipboard_image.py` | 剪贴板图片读取。按 `"PNG"` → `CF_DIBV5` → `CF_DIB` 顺序取当前剪贴板内容（不支持 `CF_HDROP` 文件引用），DIB 靠拼 `BITMAPFILEHEADER` 交给 PIL 解码，最长边超 1920 等比缩小，编码 PNG 后超 `MAX_IMAGE_BYTES`(3MB) 则转 JPEG 兜底、仍超才丢弃，产出 `PendingImage`（base64 data URL + 尺寸 + 字节数 + 来源 + 编码格式）。**每一条失败路径都自己往控制台打一行原因**（见约定 21⑪） |
| `core/client/vision/clipboard_watcher.py` | **ClipboardWatcher**。记录剪贴板最近一次变更的时刻——Windows 剪贴板不带时间戳，只能自己轮询 `GetClipboardSequenceNumber`（不需 `OpenClipboard`、零争用、单次 0.455µs）。周期 5s，检查时再同步补读一次以捕捉刚发生的变化；误差单向（只会高估新鲜度、不会误杀新图），上限一个周期 |
| `core/client/vision/notice.py` | 视觉输入的终端提示。`notify(msg, dedupe=True)` 与上一条完全相同就不再打——连按快捷键时每次结果往往一字不差，逐条打出来只是刷屏。**只有「什么都没发生」的提示能去重，挂载成功必须 `dedupe=False`**（见约定 21⑬）；文件日志不受影响，仍每次都记 |
| `core/client/vision/image_archive.py` | **挂载图片的落盘归档**（排查用）。每张成功挂载的图存进 `logs/vision/`，结果到达时按去向改名补上 `task_id` 与 used/dropped；超出 `vision_archive_keep` 的按 mtime 从旧到新删除，**只删符合自身命名格式的文件**。任何失败只记日志不外抛 |
| `core/client/vision/vision_manager.py` | **VisionManager**。注册截图快捷键（`vision_capture_key`），回调只做一次 submit、读取全在工作线程——回调跑在低级键盘钩子线程上，PNG 编码几百毫秒会触发系统摘钩（同约定 12）。注册前用 `keyboard.HotKey.parse` 自校验，非法字符串会让共用的 GlobalHotKeys Listener 整个起不来、把 `llm_stop_key` 一起带走 |

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
| `windows_selection.py` | 标准库 ctypes 调用原生 UI Automation，只查前台焦点及祖先的 TextPattern；UIA 不支持时补充 Scintilla 焦点控件的标量只读查询。独立线程、总等待上限、COM/BSTR 释放，返回选区/空选区/失败等明确状态（见约定 26） |
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

用户按下打断键（录音中 / 等待结果中，见约定 12）
  → win32_event_filter → ShortcutManager._handle_cancel_key
  → 录音中: task.abort() → state.queue_in.put(abort)
             → AudioRecorder 丢缓存 + 删录音文件 + 静默发 is_final
  → 等待中: ShortcutManager._abort_pending_result()
  → state.mark_task_cancelled(task_id) + show_cancelled_indicator()

服务端返回结果
  → ResultProcessor._handle_message (result_processor.py)
  → 命中打断名单则整条流水线跳过（最终包同时移出名单）
  → 热词替换 (PhonemeCorrector + RuleCorrector)
  → 去标点 (TextOutput.strip_punc)
  → LLM 润色 (LLMHandler.process_and_output, 可选)
      → 角色确认、提醒与 Companion 分流后读取一次选区
      → on_selection 回调打印选区预览、识别结果；同一快照传给模型
  → 文本输出 (keyboard.write 或 Ctrl+V 粘贴)
  → 日记保存 + 音频重命名
```

### 连续对话（免前缀粘滞路由，见约定 19）
```
按下录音键
  → ShortcutTask.launch()
    → _prepare_continuous()
      → state.expire_continuous_if_idle()   # 闲置超时懒判断，无后台定时器
      → state.begin_continuous_snapshot()   # 记录此刻的粘滞角色
    → show_recording_indicator(..., snapshot.indicator_mode(), sticky_role)
  → AudioRecorder.record_and_send()
    → state.bind_continuous_snapshot(task_id)   # 补上 task_id 索引

录音期间按下切换键
  → win32_event_filter → ShortcutManager._handle_chat_key
  → state.toggle_continuous()（翻转快照，非幂等，有自动重复闸门）
  → update_recording_indicator_continuous(mode, role)   # 独立命令，不重置录音计时

松开录音键
  → ShortcutTask.finish() → state.commit_continuous_exit()   # 退出在此落地

服务端返回最终结果
  → ResultProcessor._handle_message
  → state.pop_continuous_snapshot(task_id)          # 只看快照，不读全局状态
  → _resolve_forced_role()  角色已失效 → 自动退出连续对话
  → LLMHandler.process_and_output(..., forced_role) # 有粘滞角色则跳过角色检测
  → _update_continuous_state()
      武装 + 命中具名角色 → state.enter_continuous(role)
      武装 + 未命中       → 切换作废
      退出               → 仅控制台告知（松手时已落地）
      连续对话中普通一句  → state.touch_continuous() 刷新闲置计时

录音作废（短按取消 / ESC 打断 / 等待阶段 ESC）
  → state.rollback_continuous_snapshot(task_id)
  → 丢弃快照；已落地的退出恢复回去
```

### 视觉输入（剪贴板图片随下一句上传，见约定 21）
```
用截图工具截图（Win+Shift+S 等），图片进剪贴板
  → 按下 vision_capture_key（默认 ctrl+alt+d）
    → VisionManager._on_hotkey()        # 低级钩子线程，只 submit 不干活
    → 工作线程 _is_fresh()              # 剪贴板变更距今 > vision_clipboard_max_age 则整个跳过
    → 工作线程 grab_clipboard_image()   # PNG / CF_DIBV5 / CF_DIB，缩放 + 编码 + 限额
    → state.set_pending_image()         # 待挂槽，后按的覆盖先按的

按下录音键
  → ShortcutTask.launch()
    → state.begin_image_snapshot()      # 待挂图移入本次录音（互斥）
    → show_recording_indicator(..., has_image)   # 最外圈描边变淡紫
  → AudioRecorder.record_and_send()
    → state.bind_image_snapshot(task_id)

服务端返回最终结果
  → ResultProcessor._handle_message
  → state.pop_image_snapshot(task_id)          # 只看快照，不读全局待挂槽
  → LLMHandler.process_and_output(..., image_data)
      角色 enable_vision=True  → MessageBuilder 产出 Chat 口味多模态 content
                                 → _build_responses_params 转 Responses 口味 → 上传
      角色 enable_vision=False → 不带图，image_used=False
  → _report_image_usage()   用掉记 INFO；丢弃打一行黄色控制台提示 + 原因

录音作废（短按取消 / ESC 打断 / 等待阶段 ESC）
  → state.rollback_image_snapshot(task_id)     # 图退回待挂槽（新图已在则不覆盖）
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

### 一次性提醒（具名角色入口）

`ResultProcessor` 提供录音结束时间 → `LLMHandler` 具名角色检查 → `reminder/analyzer.py` 宽松预筛与隔离的非流式语义分析 → `model.py` 本地校验 → `store.py` 原子保存 → `service.py` 调度 → `output.py` 内部状态 / 角色 Toast / OpenMate 批次。

`fullscreen.py` 用 Win32 判断前台全屏、锁屏和屏保；`toast_reminder.py` 专用于底部固定超时且不可拖动的内部状态。`app.py` 持有服务，`MicRunner` 在资源就绪后恢复，退出时停止。配置、协议、恢复边界和验收清单见 [docs/REMINDERS.md](docs/REMINDERS.md)。

内部提醒使用独立 18 号字体、浅暖黄底深色字与不透明圆角；宽度按正文测量，最大取 1600px 与主屏 80% 的较小值。`reminder_status_font_size` 单独配置，不影响 LLM 角色。创建分析提示词区分事项短语与自然提醒口吻，具体表达交由模型，展示层不拼接固定语气前缀。

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
  → 具名角色先经过独立提醒入口（见约定 24）；已处理则结束
  → 普通角色请求检测到 companion 已连接 → 跳过本地 LLM
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
6. **LLM 角色**：`LLM/` 目录下每个 .py 文件是一个角色，定义 RoleConfig 字段作为模块级变量；toast 输出角色用 `toast_min_width_px`/`toast_max_width_px` 设置优先采用的像素边界，用 `toast_min_width_ratio`/`toast_max_width_ratio` 设置像素值无效时的屏幕比例回退，并可配置字体、颜色、透明度、圆角等样式。宽度从最小值随内容只增不减，到最大值后自动换行；未拖动时保持中心增宽，实际拖动后固定左边界向右增宽。`toast_duration` 在智能时长下是最短停留保底基数，实际自动关闭时长按最终文本字数对数计算并封顶 30s（见约定 17）
7. **热词文件**：`hot.txt` 每行一个热词（音素模糊匹配），`hot-rule.txt` 每行 `正则 = 替换` 格式
8. **Companion 模式**：当 Electron 伴侣应用连接到 CompanionServer 时，匹配角色前缀的文本跳过本地 LLM，直接转发给 Electron 处理
9. **多 Tk 解释器与字体约束**：录音指示器与 Toast 弹窗分别在独立线程运行各自的 Tk 解释器；创建 `tkinter.font.Font` 时必须显式传 `root=` 绑定所属窗口的解释器，否则命名字体会建到进程内第一个 Tk（`_default_root`）上，跨解释器引用时字体名会被当作不存在的字体家族，静默回退为小号宋体
10. **OpenMate 展示**：toast 角色在 LLM 调用前探测 OpenMate（`config_client.py` 的 `openmate_*` 配置），可用则自动获取角色情绪状态列表、注入情绪标签指令（标签用状态 ID，别名作语义注解）并将流式输出转交 OpenMate 展示；`<state:...>` 标签由 OpenMate 解析剥离；本地侧的剥离规则见约定 22。OpenMate 隐藏在托盘时收到消息会自行无激活恢复显示，无需客户端干预。`openmate_character` 留空时每次请求走 `GET /v1/presentation` 解析当前生效角色，按该角色取情绪状态并把流/分段队列固定到它

    **分段必须有可见正文**：`OpenMateSegmentSender._prepare_segment()` 同时处理完整段与结尾残段。空白段丢弃；仅含状态标签的段按原顺序缓存，附到下一段正文前；结束或取消时未附着正文的标签丢弃。否则 OpenMate 剥离标签后仍会为该段写入空历史并占用展示时间。标签识别复用 `llm_state_tags`，不改模型对话历史；非法或未闭合标签照常发送。情绪注入提示词要求标签紧贴正文。雾切响子、新岛真仍按单换行分段，新闻、知识汇总由提示词要求按完整信息块组织，引导语与正文同条；雾切响子的必要追问、新岛真的收尾吐槽或叮嘱接在最后一条正文后。**普通简单回复以一条完整消息为主，50 字仅作为简洁程度的参考，不作为拆条阈值**；同条内保留短句、省略号、语气词及原有角色语气，复杂回答继续按独立信息块分条。新岛真受惊、害羞时的 5-15 字短句连发作为独立例外保留。这些约束由提示词表达，发送器不强制合并，已有历史不清空。两后端分段与语气对照见 [docs/ROLE_SEGMENT_FIX_2026-09-13.md](docs/ROLE_SEGMENT_FIX_2026-09-13.md)。离线回归见 `tools/test_openmate_segments.py`。

11. **悬浮指示器状态优先级**：录音进行中（`mode == "recording"`）时服务端回传的分段进度只更新数据、不切换模式。麦克风边录边发，服务端每攒够 `mic_seg_duration + mic_seg_overlap * 2`（默认 68s）才切一段回传一次，切成 progress 会让实时录音计时被一个一分钟内都不变的「已处理」时长顶掉
12. **录音打断**：`cancel_recording_key`（默认 esc，留空禁用）在录音中或等待识别结果时按下，本次输入彻底作废（不上屏、不触发角色、不写日记、删除录音文件）。服务端的 `AudioCache` 按连接持有、只有 `is_final` 才 reset，所以打断**必须仍发一个 `is_final`** 冲掉缓冲，否则残余音频会拼进下一次录音；结果回来后按 `cancelled_task_ids` 在 `_handle_message` 入口丢弃（含非最终包）。**抑制决策必须在 KEYDOWN 锁存、KEYUP 沿用**——判据 `state.recording` 会被打断动作自身翻转，两次分别判断会产生「吞掉按下、放行松开」的不配对事件，前台程序与 pynput 组合键状态都会因此错乱。只在录音阶段拦截，等待结果阶段放行（那个状态依赖远程服务端回包，拦截会让打断键全局失灵）。打断键分支运行在键盘钩子线程里，只做状态翻转和派发：低级钩子超过 300ms 会被系统摘掉，之后所有快捷键失效
13. **Tk 几何字符串与负坐标**：`wm geometry` 把 `±x±y` 开头的符号当作「从左/上边缘量」还是「从右/下边缘量」的标志位消费掉，符号之后的数字再单独按有符号数解析。负坐标必须写成 `+-100+50`（即 `f"+{x}+{y}"`）；`f"{x:+d}"` 产出的 `-100` 会被理解成「距屏幕右边缘 100px」，窗口拖出左/上边界时会跳到屏幕另一侧。Toast 的位置/尺寸统一走 `ToastWindowBase._set_geometry()` / `_set_position()`
14. **Toast 构造失败是静默的**：`ToastMessageManager._process_queue` 用 `except Exception` 兜住窗口构造，失败时调用方只看到 `wait_for_window` 超时和「Toast 窗口创建失败」。改 `core/ui/toast_*.py` 后必须真弹一次窗口验证，只看日志没红色报错不足以说明可用
15. **角色联网搜索**：`enable_web_search` 让该角色改用 DeepSeek 的 **Responses API**（`client.responses.create`），并挂载服务端内置工具 `[{"type": "web_search"}]`，检索在 DeepSeek 侧执行，本地不需要任何搜索 API Key。是否检索由模型自行判断，不触发时的延迟与普通请求一致。三个连带约束：①Responses API 的 base_url 官方示例不带 `/v1`，而 `APIConfig.DEFAULT_API_URLS['deepseek']` 带 `/v1`，开启该功能的角色必须显式写 `api_url = 'https://api.deepseek.com'`；②检索期间数秒没有任何分片，provider 默认 2s 超时会直接失败，必须设 `request_timeout`（助理为 60s）；③参数名与 Chat Completions 不同——system 消息走 `instructions`、其余走 `input`、`max_tokens` 变 `max_output_tokens`、没有 `stop`。搭配 `enable_datetime`（在用户消息里注入当前时间，让模型判断记忆是否过期；放用户消息而非 system 是为了不破坏服务端前缀缓存）。DeepSeek 侧目前仅 `deepseek-v4-flash` 系列支持 Responses API。CPA 网关同样实现了 `/v1/responses` 与 `web_search`（2026-09-05 在小主机侧补齐并实机验证通过），因此这条约定对两套后端一致，角色不需要按 backend 开特例。网关补齐之前曾有过「接受参数但不实现」的阶段，症状是「闲聊正常、一问时效性问题就空回复」，判据与排查路径留在 docs/DEV_NOTES.md「已知状态」，日后换网关版本时可据此快速定位同类症状
16. **工具调用的回传用出参列表**：本次请求实际触发了哪些服务端工具，由流解析层沿 `used_tools` 列表逐层追加，最终进 `LLMResult.used_tools`，控制台显示成 `模型结果[助理(搜索)]`。之所以不扩成四元组返回：三个输出模式（typing/toast/openmate）里有十余处 `return ("", 0, 0.0)` 的降级分支，漏改一处就是运行时崩溃；之所以不挂 handler 实例属性：那会引入跨请求残留。工具 ID 从事件名 `response.<工具ID>_call.<阶段>` 解析而来（`_extract_tool_id`），不硬编码 `web_search`；ID → 中文的映射集中在 `llm_constants.ToolNames`，未登记的新工具回退显示原始 ID 而不是静默丢弃。**新增输出模式或在其中新增 `handler.process` 调用点时必须一并转发 `used_tools`**，漏了不会报错，只是标签永远不出现

17. **LLM 角色 Toast 智能时长**：toast 模式角色（`smart_duration=True`，`handle_toast_mode` 与 OpenMate 回退 `_show_fallback_toast`）的自动关闭时长不再固定 `toast_duration`，而是从内容生成完成（`finish()`）起按最终文本字数对数计算：`min(toast_duration + 1200·ln(1+字数), 30s)`；`toast_duration` 退化为最短停留保底基数。悬停只阻止销毁、不改变计时：鼠标进入后倒计时仍按原绝对截止时间推进，到期时仍悬停则记录 `_timer_expired` 并保留窗口，真正移出整个 Toast 后立即关闭；到期前移出则只剩实际剩余时间，反复移入移出也不顺延。拖拽发生任何位移后窗口永不自动关闭（`_never_auto_close`），只在新的 LLM 回复窗口生成或命中提前关闭路径（ESC/异常/中断/Markdown 失败/程序退出）时结束。**新 LLM 回复窗口创建时（`ToastMessageManager.close_llm_windows`，在 `add_message` 入队前调用）立即顶掉所有旧的 LLM 白色对话框——无论旧窗口是否被拖拽**，保证新窗口一弹出（即使还是空文本占位）旧窗口已消失、新旧不叠加。`close_llm_windows` 在 destroy 前会触发旧窗口的 `stop_callback`（与 ESC 关闭路径语义对称）。OpenMate 接管展示时同样调用 `close_llm_windows`（`handle_toast_mode` 的接管分支），否则拖拽过的旧本地窗口会在一直走 OpenMate 路径时永久残留。**这些行为仅作用于 `smart_duration=True` 的窗口（基类 `pause_and_resume` 标志），普通 toast 保持旧的固定时长 + 悬停移开重置行为**。实现集中在 `toast_base.py` 的 `_timer_expired` + `_start_smart_destroy_timer`/`_on_smart_timer_expired`/`_destroy_if_expired_outside`；滚轮调整位置不算拖拽（只认鼠标拖拽位移）。`active_windows` 的 `<Destroy>` 回调必须校验被销毁的是 Toplevel 本身（`str(e.widget) == str(w.window)`）——Tk 的 `<Destroy>` 在任意子 widget 销毁时也会触发 Toplevel 绑定，裸绑定会把已 finish 的窗口误移出列表、导致顶不掉（详见 docs/DEV_NOTES.md「已知状态」）。
    **`smart_duration` 同时是「这是 LLM 角色白框」的事实判别器**，除时长语义外还决定顶部留白：基类 `__init__` 按它取 `padding_top`——`True` 用 `DEFAULT_PADDING_TOP`(40px) 提供一条不会误触正文的拖动抓手，`False` 用 `COMPACT_PADDING_TOP`(15px) 与底部对齐。`height_padding = padding_top + DEFAULT_PADDING_BOTTOM` 是上下留白之和，`toast_text`/`toast_label` 按行数反推窗口高度时用它（原本是 `HEIGHT_PADDING` / `LABEL_HEIGHT_PADDING` 两个模块常量，已降为实例属性）。**顶部不能取 0**：单行普通 toast 会算出 51px 撞上 `MIN_WINDOW_HEIGHT`(60px) 兜底，省下的留白原样挪到底部，白改。同理错误提示的 `initial_height` 已从 80 改成 0，否则写死的高度会盖过计算值。留白分岔的真 Tk 冒烟测试见 `tools/test_toast_padding.py`

18. **空回复必须显形，不得降级成识别原文**：LLM 返回空正文时，`handle_toast_mode` 与 OpenMate 路径**返回空串**而不是 `polished_text or content`——那段识别原文在这两种模式下从未展示给用户，拿它冒充模型输出会让控制台、`年/月/日-角色.md` 记录、UDP 广播三处同时误报成一次成功回复，静默失败连事后都查不出来（typing 模式保留 `or content` 降级：那里的文本是真的打字输出去了，如实反映）。控制台由 `_format_llm_result` 显示黄色「（空回复，详见日志）」。原因由 `llm_processor.py` 的诊断日志给出：Responses 流把 `response.incomplete`（打印 `incomplete_details.reason`）、`response.failed`、refusal 事件全部记录，并在流结束时汇总「状态 + usage（含 reasoning token）+ 事件类型计数」——**未被解析分支认领的事件只会在这个计数里显形**，是判断「额度被思考内容吃光」的唯一线索；Chat Completions 流记录 `finish_reason`，`length` 单独告警。空回复走 WARNING，而 `logger.py` 把控制台处理器固定在 WARNING 级，因此这些诊断会直接出现在运行终端里。新增流式路径时必须一并补上这两条（空正文告警 + 终局状态解析），否则又会退回静默

19. **连续对话（免前缀粘滞路由）**：`continuous_chat_key`（默认 space，留空禁用）在**录音期间**按下，本句若命中**具名角色**（`display_name` 非空且非「默认」）就进入与该角色的连续对话，此后每句话跳过角色检测直接发给它；连续对话中再按一次则退出。**这个功能只改路由，不碰对话记忆**——历史一直由各角色的 `enable_history` 维护，`翻译` 这种 `enable_history=False` 的角色照样能进。九条约束：

    ① **路由按每句话的快照决定，不读结果到达时的全局状态**。按切换键在录音期间，而「命中了哪个角色」要等服务端回包才知道，中间隔着一整个往返，期间用户完全可能已经开始说下一句或切换了状态。`ContinuousChatSnapshot` 在 `launch()` 时建（那时粘滞角色已确定）、`task_id` 生成后登记进字典、最终结果到达时 pop，定长兜底防服务端异常滞留。

    ② **进入延迟、退出立即，这个不对称无法消除**。进入必须等结果（不知道粘谁），退出在 `finish()` 松手时落地（下一次录音的指示器立刻恢复正常配色，不必等服务端）。

    ③ **任何让这次录音作废的路径都要回滚本次切换**：同一次录音内再按切换键、短按取消（< threshold）、ESC 打断（录音阶段**和**等待阶段都算）。等待阶段的回滚要靠快照里的 `exit_committed` 把已落地的退出恢复回去，否则会出现「录音阶段按 ESC 能撤销退出、松手后按 ESC 撤销不了」这种要背时序才能解释的差别。

    ④ **连续对话中完全不做角色检测**。角色名是「翻译」「助理」这类日常词，`RoleDetector` 用的是 `text.startswith(name)`——对助理说「翻译一下这句话」会被静默劫持到翻译角色，且助理那边的历史凭空断一句。

    ⑤ **切换键在录音期间必须拦截**。录音键 `ctrl_r` 的 KEYDOWN 虽然被吞了，但物理上它确实按着，放行的空格会被系统当成真正的 **Ctrl+Space，即中文输入法的中英文切换热键**——每切换一次连续对话就翻一次输入法，而且要等下次打字才发现。抑制决策照约定 12 在 KEYDOWN 锁存、KEYUP 沿用。

    ⑥ **切换不是幂等操作，必须有自动重复闸门**。打断键的 `abort()` 幂等所以长按连发 KEYDOWN 无所谓；切换会被连发翻来覆去，用 `_chat_key_down` 保证一次物理按下只切换一次。

    ⑦ **拒绝修饰键，不做补发兜底**。修饰键被抑制时有卡在「按下」状态的风险（见 `key_mapper.MODIFIER_KEYS` 上方注释），录音键为此专门补发合成释放事件；切换键没有非用修饰键不可的理由。连同「与录音键同名」「与 `cancel_recording_key` 同名」「鼠标键」「`llm_enabled=False`」共五条校验，命中即禁用功能并打 WARNING。

    ⑧ **指示器四态只用面板底色区分，圆点恒为蓝**（白＝普通录音，浅黄＝本次录音正在改变连续对话状态，浅绿＝已在连续对话中）。录音中切换状态**必须走独立的 `update_continuous` 命令**，重发 `show_recording` 会重建 `IndicatorState` 把 `start_time` 一并刷新、录音计时清零。松手后的等待/进度/处理三态不染色——它们描述识别流水线，与连续对话正交。**面板宽度必须随标题增长**：`PANEL_W`(450) 是物理像素而字体是磅值，高 DPI 下盒子不放大、字放大，200% 缩放时「正在与“雾切响子”连续对话 12.3s」要 552px 而可用宽只有 356px，全部被 `_fit_text` 截成省略号（见 docs/DEV_NOTES.md「已知状态」的 DPI 条目）。加宽只由标题驱动（详情行不参与——progress 的识别预览可以任意长），宽度变化后必须重调 `_place_window()`（`_tick` 每 100ms 只调 `_render`，光配宽画布而不改 `wm geometry`，多出来的部分看不见）。**宽度可增可减，但测量必须用 `_compose_text(stable=True)`**：`等待识别结果.`/`..`/`...` 三帧实测差 22px，拿动画帧测宽会让窗口每秒抖十次；秒数不必处理，字体里数字等宽（各 23px）且同一次录音内单调递增。

    ⑨ **闲置超时用懒判断，不开后台定时器**（`continuous_chat_timeout`，默认 300s，0 表示永不超时）：只在下一次 `launch()` 时检查，静默退出、无提示，由指示器自证。粘滞的只是角色名，每次请求经 `LLMHandler.resolve_role()` 重新解析，角色被删/改名/禁用时自动退出并告警。

20. **服务端多候选与锁定**：`addr` 可写成单个字符串（旧写法）或候选列表，列表项支持 `'host:port'` 覆盖 `port`。未锁定时 `connect()` 在**一次调用内**按序探测整个列表（因此 `ResultProcessor` 的重连循环与 `FileTranscriber.check()` 两个调用点零改动，文件转录模式自动继承该能力），**首个连上的候选立即锁定**，此后含断线重连都只连它、绝不改投其他候选，换机需重启客户端。四条约束：

    ① **锁定发生在「首次连接成功」而不是「启动那一刻」**。客户端常随开机自启，那时服务端往往还没起来，若按启动时刻定终身就会锁死在一个不可达地址上。一轮全败则 2s 后从头再扫（沿用既有节奏，**不做指数退避**——退避会拖慢服务端恢复后的重连，而那正是最需要快的时刻）。

    ② **探测超时按地址分档**：内网/回环字面量（RFC1918 三段 + 127/8）走 `addr_probe_timeout_lan`(2s)，公网、域名、主机名、IPv6 一律走保守的 `addr_probe_timeout`(3s)。分档在 **DNS 解析之前**按配置字符串判定——为分档先去解析域名，可能自己就先挂住几秒。判定**不能用 `ipaddress.is_private`**：它还涵盖 169.254/16、198.18/15、240/4 等保留段，口径比 RFC1918 宽得多，会把链路本地地址误划进短档。

    ③ **单候选与已锁定的连接不传 `open_timeout`**，沿用 websockets 默认的 10s。传 `None` 是「永不超时」，与「用默认值」不是一回事；靠这条，旧的单地址配置行为逐字不变。探测超时触发时抛的是 `asyncio.TimeoutError`，Python 3.8 下它与内建 `TimeoutError` 不是同一个类，两个都要捕获。

    ④ **「不切换」必须在日志里自证**。「另一台明明开着却不切过去」是刻意设计但极其反直觉，半年后自己看到都会当 bug 查：锁定后重连失败打一条写明「按设计不会切换到其他候选，如需换机请重启客户端」的 WARNING（仅多候选时打，单候选那句话没有意义）；一轮全不可达打一条列出全部候选的 WARNING。两者都走 `_connect_fail_logged` 闸门、连上即复位，所以不会刷屏。

21. **视觉输入（剪贴板图片随下一句上传）**：`vision_capture_key`（默认 `<ctrl>+<alt>+d`，留空禁用）按下时读取**剪贴板当前内容**里的图片暂存内存，随下一句话上传给开启了 `enable_vision` 的 LLM 角色。十一条约束：

    ① **不自己实现框选，读剪贴板当前内容**。系统截图工具本身就是框选，落进剪贴板的已经是用户圈定的那块区域，自绘一套拿到的图完全一样，却要额外背上全屏遮罩、虚拟屏空洞（本机副屏在主屏正下方且左缩进 485px，虚拟矩形里有两条不属于任何显示器的竖带）、DPI 与第三个 Tk 解释器这一堆边界。格式优先级 `"PNG"` → `CF_DIBV5` → `CF_DIB`；**刻意不支持 `CF_HDROP`**（资源管理器里对文件 Ctrl+C 只放文件引用，要读盘，可能是网络路径）。Win+V 的剪贴板历史是另一套机制，本功能走 `OpenClipboard`/`GetClipboardData` 读当前剪贴板，不受其影响。

    ② **快捷键回调不能干活**。`GlobalHotKeys` 是 pynput `keyboard.Listener` 的子类，Windows 上走 `WH_KEYBOARD_LL`，**回调在钩子线程上同步执行**；而 DIB 解码 + 缩放 + PNG 编码在 4K 图上可达数百毫秒，超过系统 300ms 容忍上限就会被摘钩，连 `llm_stop_key` 一起失效（同约定 12）。回调只 submit，读取全在工作线程。

    ③ **「下一条」从图片挂载成功那一刻起算**，不是从按下快捷键起算。②的异步是架构必然，所以「按完快捷键立刻开始录音」时槽位还空着，那次录音就是一次普通不带图的录音，图留给再下一次——这是正确语义而非缺陷。

    ④ **严格「下一条」，无超时、无过期**。下一次录音一旦有效落地，图必然消失：命中开启 `enable_vision` 的角色就上传，否则丢弃。**命中了角色但该角色 `enable_vision=False` 同样丢弃**，一条规则不留例外——留着会重新引入「图什么时候真的消失」的不确定性，而那正是拒绝超时方案的理由（超时静默失效不可见，弹提示又太吵）。

    ⑤ **路由按每句话的快照决定**，与约定 19 同构同因：图片绑定在 `launch()`、`task_id` 生成后登记、最终结果到达时 pop，定长兜底。**`pop_image_snapshot` 不得清 `image_current`**——上一句的结果到达时下一次录音可能已 launch 但还没 bind（`record_and_send` 是另一个协程），清掉会把新录音的图凭空抹掉。

    ⑥ **任何让这次录音作废的路径都要把图退回待挂**（短按取消、录音阶段 ESC、等待阶段 ESC），与约定 19③ 同一条规则：重截一张图的代价比重说一句话高得多。退回时若待挂槽已有更新的图则不覆盖。

    ⑦ **唯一的持续可见信号是录音悬浮框内侧多出一圈描边**（`IMAGE_BORDER` / `IMAGE_BORDER_WIDTH` / `IMAGE_BORDER_INSET`），不弹任何窗。它出现在唯一还来得及补救的时刻——录音刚开始、话还没说。**必须单独画这一圈，不能只把面板最外层 `outline` 换色**：那圈是 Tk 默认的 1 **物理**像素，而面板尺寸是物理像素、字体是磅值，200% 缩放下它细到几乎看不见（2026-09-04 实机反馈「看不出特殊效果」的根因）；内嵌还避开了 Tk 描边以路径为中心、`MARGIN=0` 时外侧一半会被窗口裁掉的问题。这圈描边与连续对话完全正交（约定 19⑧ 管的是**面板底色**），所以浅绿底 + 彩圈 = 连续对话中且这次带图。图片被丢弃时只打一行黄色控制台提示（事后已无从补救，弹窗只会打断正在上屏的输出）。

    ⑧ **`enable_vision` 不校验 `model` 是否真支持视觉**，写错了服务端返 400，由约定 18 的诊断日志打到终端。因此开启该字段的角色必须自己把 `model` 换成视觉模型，它会作用于该角色的**每一次**请求而不只是带图那次。图片实测可与 `web_search` 内置工具在同一个 Responses 请求里共存，无需降级分支。编码链路：最长边 >1920 等比缩小 → 编码 PNG → 超 `MAX_IMAGE_BYTES`(3MB) 则**转 JPEG 兜底**（质量 85）→ JPEG 仍超才丢弃。**兜底只走「缩放后 PNG 仍然超限」这一条路**，没超限就原样发 PNG，不为它多付一次编码时间。`_encode_jpeg` 对带 alpha 的图必须先合成到白底再编码，**不能直接 `convert('RGB')`**——那只是丢掉 alpha 通道、保留透明像素底下的 RGB 值，而截图里那些值往往是黑的，结果是透明区域整片变黑（真 PIL 实测：合成白底得 (255,255,255)，裸 convert 得 (0,0,0)）。历史里**只保留最近一张真图**，更早的由 `ContextManager.demote_images()` 降级成 `［图片］`；但 `demote_images()` 是在**响应结束后**才调的，而请求早已用降级前的历史组装好，所以**换图那一轮的请求载荷里实际有两张真图**（历史里一张、本句一张，分属两条不同的 user 消息，不在同一条里）——存储侧的不变量成立，请求侧比这句话多一张，2026-09-12 确认为可接受、不改。`image_data` 与 `used_tools` 一样要逐层转发到三条输出路径，**新增输出模式时漏了不报错、只是图片静默丢失**。

    ⑨ **图片必须足够新**（`vision_clipboard_max_age`，默认 60s，0 表示不检查）。防的是「很久以前截的图还躺在剪贴板里，按快捷键时被误当成刚截的那张附上去」——**淡紫边框只说明「这次带了图」，说明不了「带的是哪张」**，唯独这种误配它看不出来。Windows 剪贴板不带时间戳（`GetClipboardSequenceNumber` 只是变更计数器，`GetClipboardOwner` 只给 HWND，Win+V 的历史时间戳又漏掉 `CF_DIB` 那类来源且要 WinRT），只能自己轮询序号记录变更时刻。三个要点：**检查时必须再同步补读一次序号**，否则「截完图 0.2s 就按快捷键」会因轮询没跟上而把新图误杀；误差方向是单向的（只会把稍旧的当成刚变的），所以轮询周期必须显著小于阈值（5s 对 60s）；**客户端启动后剪贴板一直没变过时，时刻是「未知」，按过期处理**——规则是「必须是近 N 秒内写入的」，证不出来就不算，代价是重启客户端后要重新复制一次。序号读不到（无剪贴板访问权限）时**退回不检查**而不是拒绝挂载，避免把整个功能连坐禁掉。

    ⑩ **只读选区模式不访问剪贴板，回退/直接 Ctrl+C 与粘贴输出仍会写剪贴板**。`selection_mode='uia'`（见约定 26）时，读取选区不会影响待挂图片。两种复制模式共用旧的 `try_paste()` 保护：原内容不是文本时不拿空串写回还原；但 Ctrl+C 复制成功本身仍会覆盖图片，文本剪贴板也可能因占用而还原失败。粘贴模式 `paste_text` 同样必须写剪贴板，改动这一带时仍需考虑视觉输入。

    ⑪ **挂载的每一种结果都必须在控制台留一行，成功与失败不能长得一样**。视觉链路全程只用 `logger.info`，而 `logger.py` 把控制台处理器固定在 WARNING（约定 25④），于是按下快捷键之后成功和失败给用户的反馈**完全一致——都是什么都没有**；唯一的区别要等到下一次录音看有没有淡紫描边，那时话都快说完了。2026-09-12 实机因此误判了三次：连续 16 次挂载被体积上限挡下，终端一片安静，现象看起来像「快捷键失灵」，还顺带被归因到刚开启的连续对话上；而更早挂上的一张 185×83 小图在待挂槽里躺了 80 秒（两次 ESC 打断把它退回、两次重新挂载又都超限），最后被当成「刚复制的那张」发了上去。**失败原因由 `grab_clipboard_image` 自己 `console.print`**（没有图 / 解码失败 / 体积超限 / 剪贴板被占用各一条），`VisionManager` 只负责成功那行和时效两条，**调用方不再猜原因**——此前 `vision_manager` 把所有 None 一律写成「剪贴板中没有可用图片」，体积超限也被说成没有图，日志因此同样查不出真相。新增失败分支时必须一并补控制台输出。

    ⑬ **连按快捷键要合并，排队里最多只留一个捕获**（`VisionManager._capture_queued` + `_capture_lock`）。读取跑在单线程 executor 上，排着的那个任务跑起来时读的是**它执行那一刻**的剪贴板，因此它必然涵盖此刻之后的任何一次按键——再排一个只会重复解码、重复归档、重复刷一行终端，拿到的还是同一张图。**放闸必须在 `_capture` 开始执行时**（而不是结束时）：此刻之后的按键可能对应更新的剪贴板内容，不放闸就会漏掉「先按快捷键、再截新图、再按一次」这条路。**但闸门挡不住人手速度的连按**——剪贴板没变时时效检查 1ms 内就返回，队列根本来不及堆积，2026-09-12 实机 9 连按仍然刷了 9 行。真正管这件事的是另外两道：**①剪贴板序号没变且上次挂的图还在待挂槽里就整个跳过**（`current_sequence()` + `_last_mounted_seq`，连解码和归档都不做；图一旦被某次录音消耗掉 `pending_image` 变空，同一张图就能重新挂上，所以不会误杀）；**②终端提示走 `notice.notify()`，与上一条完全相同的不再打**——那 9 行「剪贴板自客户端启动以来未变动过」走的就是这条。**去重只用于「什么都没发生」的提示**（没有图 / 体积超限 / 时效过期 / 剪贴板被占用），**挂载成功必须传 `dedupe=False`**：两张不同的图完全可能渲染出逐字相同的一行（同尺寸、同整数 KB、同来源），吞掉第二条就等于让用户以为新图没挂上——挂载 A → 被录音消耗 → 挂载同规格的 B，第二条连「已顶掉」后缀都没有，与第一条一模一样。另外**不去重的消息也要刷新「上一条」**，否则中间插一条成功提示后，再出现与更早那条相同的失败提示会被误吞。序号读不到（无剪贴板访问权限）时返回 0，此时**不得跳过**，否则功能会被连坐禁掉。提交失败（退出过程中 executor 已 shutdown）要复位闸门，否则后续按键永久失效。待挂槽只有一个位置，**后挂的顶掉先挂的时必须在提示里说出来**——否则连按两次会看到两行「已挂载」，读起来像挂了两张，而实际只有最后一张会被用；被顶掉那张的归档同时标成 `drop`。

    ⑫ **每张挂载的图都落盘存档，文件名必须以时间戳开头**（`vision_archive_keep`，默认 100 张，0 关闭）。淡紫描边只说明「这次带了图」，日志也只说得出尺寸，**「那次到底是哪一张」事后无从查证**——2026-09-12 那次误判就是靠翻截图目录、比对文件时间和平均亮度才认出来的。归档文件形如 `260912055702_DV5_pend.png`，结果到达时由 `_report_image_usage` 改成 `..._used_<task_id>.png` 或 `..._drop_<task_id>.png`。**每一列都定长，列目录时逐列对齐**：时间戳 12 位（两位年份 + 日期时间连写）、来源代号 3 位、去向 4 位（`pend`/`used`/`drop`）。来源写代号而不是原名（`PNG` / `DV5`=CF_DIBV5 / `DIB`=CF_DIB / `UNK`=未知，映射在 `_SOURCE_CODES`）——原名长度是 3/6/8 位，会把后面几列挤得错开；日志里仍用全称。尺寸不进文件名（日志里有）。被下一张图顶掉的那张从没绑定过录音、没有 task_id，直接记成 `..._drop.png`。**时间戳必须排在最前面，不能拿 `task_id` 打头**：它是 UUID1，开头的 `time_low` 只是时间戳低 32 位，每约 429.5 秒回绕一次，按它排序得不到时间顺序（日志里的现成反例：03:10 是 `62ccea6f-ae14-…`，03:40 是 `97def774-ae18-…`）。清理按 mtime 从旧到新，且**只删符合自身命名格式的文件**——自动删除不可逆，宁可少删不可错删。

22. **状态标签在展示层剥离，不在记忆层剥离**：`<state:微笑>` 这类标签是 OpenMate 的展示指令，实现集中在 `llm/llm_state_tags.py`。**对话历史必须存原文**——此前存历史前先剥离，结果是历史里的 assistant 消息永远不带标签，模型从第二轮起照抄自己上一条的形态、不再输出任何标签（few-shot 漂移压过 system 指令），表现为「情绪立绘只在每轮对话的第一句变化，之后全是默认状态」。2026-09-04 用真实 API 三轮对照实测：历史剥标签 → 微笑/无/无，历史留标签 → 微笑/失落/微笑。代价是**每条本地展示路径都必须自己剥**：Toast、打字、md 记录、UDP 广播。其中两条流式路径不能逐分片跑正则——`<state:微笑>` 会被切成 `<sta` + `te:微笑>`，打字模式下漏一个就是真的敲进用户正在编辑的文档，必须用 `StateTagFilter`（只暂存可能补成标签的尾巴）。**新增输出模式时漏剥不会报错，只会把标签直接显示/打出去。**

23. **LLM 后端切换（DeepSeek 直连 / CPA 局域网网关）**：四个 toast 角色（助理、雾切响子、新岛真、翻译）的 API 段改为角色文件里一行 `backend = 'deepseek' | 'cpa'`，其余取自 `LLM/backends.py` 的 `resolve(backend)`（返回 `provider`/`api_url`/`api_key`/`model`/`request_timeout` 五项的副本）。`default`（typing 角色）不在范围内，逐字未动。六条约束：

    ① **`backends.py` 的文件名必须排在所有角色文件之前**。`load_all_roles` 对 `sorted(glob('LLM/*.py'))` 逐个 `del sys.modules` 再 import，共享模块只有先被重新执行，角色随后 import 到的才是新值。`b` 排在 `default.py`/`secrets.py` 与四个中文角色名之前，这一点成立；改成 `zz_backends.py` 之类会让热切换静默失效（读到上一轮的旧值，不报错）。`tools/test_backends.py` 有一条断言钉住它。

    ② **模型选择只在 `backends.py`（`CPA_MODEL`），后端选择只在角色文件**。分层的目的是角色文件里只剩一个旋钮；换模型改一处、四个走 cpa 的角色一起变，改后端则是每个角色各自决定。两处都是 `LLM/` 下的 .py，存盘即触发 watcher 重载，都不必重启客户端。

    ③ **`resolve()` 对未知名字回退到 DeepSeek 并打印告警，不抛异常**。角色文件在 import 期抛异常会被 `RoleLoader.load_all_roles` 的 `except Exception` 接住、该角色**整个从 `roles_registry` 消失**，而 errors 列表没有任何调用方在看（`llm_handler.reload_roles` 直接丢弃返回值），现象只剩「这个角色不响应了」。

    ④ **`request_timeout` 也由 backend 给出，两套统一 60s**。provider 兜底的 2s 两边都不够——DeepSeek 侧检索期数秒无分片（约定 15②），CPA 侧实测中位 3~7s、带图约 20s。**这让翻译角色在 DeepSeek 下的超时由 2s 变成 60s**，是本次改造唯一一处 DeepSeek 侧的行为偏差（它原本没写这一行）。

    ⑤ **CPA 侧 `provider` 写 `'cpa'` 而不是 `'gemini'`**：同一个网关还代理 kimi、glm、grok 等十几个模型，换 `CPA_MODEL` 时这个名字不用跟着改。它只影响日志/错误信息里的名字与 `APIConfig` 的兜底查表，而④恒定给出 `request_timeout > 0`，那张兜底超时表永远走不到。

    ⑥ **切换只是配置，`core/` 一行未改**：没有重试、没有自动回退、没有多候选探测。网关只在局域网内可达，人不在那个网里时走 cpa 的角色一律请求失败；上游抖动（`oauth2.googleapis.com/token` EOF、`auth_unavailable` 503 等）同样直接失败。要切回去就改角色文件里那一行。

---

24. **一次性提醒是独立任务**：仅具名角色/连续具名角色且录音入口有结束时间基准时参与，拦截位置早于 Companion。预筛以提醒相关词或时间线索减流：数字须带时间单位或时刻/日期格式，单独的数字及「一下、一段、200字」不触发；提醒词和时间线索任一命中即可，无需同时出现。LLM 一次返回操作、时间语义、事项与角色文案，本地强校验时间与存储；`none` 才串行恢复普通角色流程，失败直接内部通知。提醒不读选区/历史/图片，不调用工具/搜索，不记角色历史/md，不更新角色输出状态或 UDP；原始听写归档保留，连续对话照常进入/刷新，本句图片成功失败都消费且注明「提醒任务不使用图片」。处理完成后仅普通任务安排自动回车，避免提醒向前台注入按键。内部状态独立底部固定超时窗口；到期文案复用普通角色窗口或 OpenMate 批次。Client 将待执行事件原子持久化到 `logs/reminders.json`，文件缺失或无效时自动重建空快照；OpenMate 接受后的队列暂不持久化。完整行为与崩溃边界见 [docs/REMINDERS.md](docs/REMINDERS.md)。

25. **中断键有两层闸门，且中断必须留痕**：`llm_stop_key`（默认 esc）是 pynput 全局热键，在**任何**前台程序里按下都会打到 `StopMonitor.on_stop_pressed`，所以不能见按就停。

    ① **作用域闸门**：`process_and_output` 用 try/finally 包住整段，进入时 `begin_request()`、离开时 `end_request()`（后者顺带清停止标志，不让已置位的标志跨出请求边界）。作用域外的按键直接忽略，连日志都压在 DEBUG——那种按键占绝大多数。

    ② **起效闸门**：请求在跑、但**一个正文字符都还没产出**时按下不作中断，只记一条 INFO。武装点统一在 `LLMHandler.process` 包装 `callback`/`status_callback`，首个正文分片或首条工具状态占位触发 `mark_output_started()`，三条流式路径与粘贴模式（回调为 None 时也要传包装后的那个，否则永远武装不起来）全部覆盖。**判据是「用户屏幕上有没有东西」**：本地 Toast 还没弹、OpenMate 还没收到任何分片时，这一下不可能是冲着「停下正在显示的回复」来的。代价是彻底卡住、零输出的请求不能再用按键提前掐掉，只能等 `request_timeout`（两套后端均 60s）——那种状态下按键本来也没有任何反馈。

    ③ **中断必须在日志里自证**。此前一次「零输出中断」会让三处诊断同时失效：`on_stop_pressed` 只打 DEBUG；`llm_processor` 三条流的空回复告警都带 `not stopped` 条件而被抑制（见约定 18）；三条输出路径的中断分支一行不打。结果是控制台只剩「（空回复，详见日志）」而日志里什么都没有，OpenMate 一片空白且无从追查（2026-09-12 实际故障）。现在 `stopped` 分支各自补 INFO 终局快照，输出侧补「已展示/已发出 N 字」，且区分「尚无任何内容发往 OpenMate」。**新增流式路径或输出模式时，空正文告警与中断留痕两条都要补**。

    ④ **诊断一律走 INFO，不许升 WARNING**：`logger.py` 把控制台处理器固定在 WARNING，升级就等于往运行终端刷字。中断是正常操作，不该在命令窗口留下任何输出。

    ⑤ 连带修正：`llm_output_toast.py` 原先用 `logging.getLogger(__name__)`，那是 `core.client.llm.*` 层级，与项目日志器 `client` 不在同一棵树上，写出去的记录根本进不了 `logs/`——「Toast 窗口创建失败」这类错误一直是隐形的，已改为 `from . import logger`。提醒服务里的 `monitor.should_stop()` 因②实际恒为假（分析是非流式的），取消以 `self.stopped` 为准，该处已加注释。离线回归见 `tools/test_stop_gate.py`。

26. **选区获取由客户端全局三模式配置控制**：角色仍用 `enable_read_selection`/`selection_max_length` 决定是否读取及附加长度；`ClientConfig.selection_mode` 对所有角色统一生效，替代旧的 `selection_fallback_to_ctrl_c`。`'uia'` 为只读获取，`'uia_then_ctrl_c'` 为只读未取得文字后回退，`'ctrl_c'` 为直接复制。当前本地配置由原来的 True 迁移为 `'uia_then_ctrl_c'`；配置项缺失时按 `'uia'` 处理。

    ① `'uia'` 使用只读接口，不模拟输入、不读写剪贴板：优先 Windows UI Automation 的 `TextPattern.GetSelection/GetText`；UIA 不支持时可走⑥的 Scintilla 专用查询。`'uia_then_ctrl_c'` 复用这条只读链路，未取得非空白文字时回退 Ctrl+C，包含 `empty/unsupported/error/timeout`；UIA 空值不能证明用户没有选区。只读链路已检测到密码控件、密码属性读取失败、元素失效或前台/焦点变化（`blocked/changed`）时跳过回退。`'ctrl_c'` 完全不查询 UIA/Scintilla，直接执行旧复制流程，也不使用 UIA 的密码检测。两种复制模式发键前均核对原输入窗口，共用剪贴板还原、相同剪贴板内容判定与后续长度/历史去重/预览处理。回退或直接复制均可能中断终端任务，配置注释须保留说明；日志只记录状态、长度与接口/所选模式，不记录选区正文，不往终端额外刷诊断。

    ② `windows_selection.py` 不新增第三方依赖，使用 Windows 8+ 的 CUIAutomation8/IUIAutomation2。COM 只存在于独立无窗口 MTA 线程，检查焦点到原前台窗口的父链（上限 128 层；VS Code/Electron 实测可达 30 层，不能用原来的 16 层上限），不遍历整个窗口寻找其他面板的选区。连接/单次事务超时 300ms，调用方总等待 1s；最多一个读取线程在途，迟到结果丢弃，后续请求不会不断增生线程。工作线程退出时释放所有接口和 BSTR；系统调用若永久不返回，选区功能会持续报超时，需重启客户端恢复，不强杀持有 COM 的线程。

    ③ 正常入口在角色确认、提醒处理与 Companion 分流后，Toast/OpenMate 展示前只捕获一次，`SelectionContext` 经 typing/toast/openmate 全程传入 `process`。显式的空快照也必须转发，不能在输出模式里重新读取；`None` 只供直接调用 `process` 的兼容入口现场捕获。`on_selection` 回调让 ResultProcessor 在模型请求前打印选区预览与识别结果，无角色、提醒、Companion 和异常路径仍打印一次识别结果。

    ④ 成功捕获时在「识别结果」之前打印一行「选中文本」。显示层合并换行/空白、清理控制字符，最多 50 字加省略号，并按终端宽度截断（Rich 禁用 markup/highlight，不换行）。模型仍接收按角色长度限制截取的原文。重复选区按既有历史规则不再附加时，仍显示预览并标注「已在历史，本轮未重复附加」；与上次输出相同时说明未附加。清除历史同时清除选区去重记录。

    ⑤ Windows Terminal 原生选区可由 UIA 提供，TUI 内部高亮是否暴露取决于应用；`'uia'` 模式读不到时不附加，仍正常提问。另两种模式明确允许发送 Ctrl+C，不能保证终端任务不被中断。不要把终端中看得见的高亮一概视为原生选区，也不要为兼容性自动改发 Ctrl+Shift+C 或其他快捷键。回归见 `tools/test_selection.py`；原生 Edit 控件冒烟见 `tools/test_selection_native.py`，Windows Terminal/TUI 仍需实机验收。

    ⑥ **Scintilla 编辑器（含 Notepad4）**：UIA 返回 `unsupported` 后，仅对原前台窗口内、Win32 类名恰为 `Scintilla` 的键盘焦点控件查询 `SCI_GETSELECTIONS/GETSELECTIONNSTART/GETSELECTIONNEND` 与 `SCI_GETCHARAT`。消息参数/返回值全是整数：禁止把本进程的缓冲区指针直接传给另一进程的 `SCI_GETSELTEXT`，也不向外部进程分配/写入内存。只读取选区范围内的字节，按控件代码页增量解码，中文/emoji 不截断字节；多选区按文档顺序拼接换行。最多 64 段、16KiB，单消息最多 100ms、专用查询总计 500ms，仍受整个读取的 1s 等待上限控制；前后复核选区位置、文档长度、编码和前台焦点。空选区不读正文；不支持的其他控件绝不接收这些消息。

    ⑦ **VS Code 集成终端不等于 Windows Terminal**：xterm.js 的普通鼠标选区保存在内部 SelectionModel，默认不对应浏览器 DOM Range；屏幕阅读器模式暴露终端行文本也不保证暴露这份选区。纯外部 UIA 无法保证读取，当前没有专用接入；用户已明确排除安装 VS Code 扩展或改变其环境的方案。普通编辑区须单独诊断，不能套用终端的原因。不能把终端全文、隐藏输入框内容或上次剪贴板内容当作当前选区。来源与当前实测状态见 `docs/DEV_NOTES.md`。

    ⑧ **VS Code 普通编辑区取决于辅助功能状态**：本机 `native-edit-context` 焦点及 Document 祖先曾在用户保持选区时连续返回空文本。其 Simple/RichScreenReaderContent 只在 `isScreenReaderOptimized()` 为真时同步辅助节点的 DOM 文字与选区，否则清空节点文字；对应开关为 `editor.accessibilitySupport`，`editor.renderRichScreenReaderContent` 仅选择内容形式。2026-09-15 用户自行开启前者后确认读取成功，23:07:10 日志记录 UIA 成功读取 22 字。CapsWriter 不修改该设置，也不安装外部组件；未开启时的兼容限制仍存在。`TextPattern` 存在且返回空文本，不足以证明编辑器内部没有选区；原生 Win32 Edit 冒烟也不能代表 Monaco 兼容性。诊断与源码依据见 `docs/DEV_NOTES.md`。

## 开发环境与验证

提醒解析诊断：Toast 显示具体失败项，INFO 文件日志记录调用阶段、响应状态和有界字段/JSON 片段，终端不额外打印诊断。详见 `docs/REMINDERS.md`。

提醒时间前缀：实际投递晚于计划时间超过 10 秒才显示，与全屏暂停记录无关；本地合并各条独立判断时间前缀并保留角色名。此容忍值只影响展示。

本目录是打包后的 Release 目录，不是可直接跑测试的源码工程：

- **运行方式**：双击 `start_client.exe`，它使用 `internal/` 内嵌的 Python 3.8 运行时和依赖库
- **系统 Python 不可直接使用**：没有本项目的依赖（rich、pynput、sounddevice 等），且 `import core` 会在包 `__init__` 中连锁导入 rich 而失败；单元测试需先在 `sys.modules` 预注入桩模块（伪造 `core`、`core.client.llm` 等包及其 `logger`），再用 `importlib` 按文件路径加载目标模块
- **验证手段从轻到重**：`ast.parse` 语法检查 → 未定义名字检查 → 桩模块单元测试 → `start_client.exe` 实机验证。前三步已封装成 **`python tools/run_tests.py`**（离线、零依赖、不花额度），改完代码提交前跑一遍
- **`ast.parse` 只验语法，抓不到 `NameError`**：跨函数转发参数（`used_tools`、`image_data` 这类，见约定 16/21⑧）时改错函数，语法检查和桩测试可能全过，直到那条分支真被走到才炸——而它常常要「开着某个外部程序」才走得到。补救手段是 `tools/check_undefined.py`（AST 作用域链检查，按 局部→外层函数→模块→内置 解析每个 Load 名字，无第三方依赖）；已知豁免记在 `tools/undefined_baseline.txt`，**它看的是有没有新增而不是归零**
- **严禁提交 pyc**：`__pycache__` 不得进仓库（历史上误提交过，见提交 71103d3）；语法检查用 `ast.parse` 而非 `py_compile`，避免生成缓存文件
- **厂商行为要单独验**：有几条结论押在 DeepSeek 当前行为上（图片与 `web_search` 共存、历史里的图仍可见、情绪标签跨轮保持、`thinking:disabled` 无效），本地单测证不了，厂商改了也不会报错、只会悄悄失灵。`python tools/probe_llm_live.py` 一次性重测这四条，**会真发请求消耗额度**，所以不在 `run_tests.py` 里
- **角色分段对照**：`tools/probe_role_segments.py` 用当前两套后端真实请求，检查雾切响子或新岛真的分段与语气；复用生产参数构造和分段发送器，OpenMate 输出只在内存中收集。默认只测当前角色，`--baseline` 可加载归档的原提示词做配对对照；情绪状态获取失败时仅雾切响子有既有快照可用，新岛真不注入未知状态。会消耗额度，不进离线流水线。2026-09-13 的正式调整、限制与使用方式见 [docs/ROLE_SEGMENT_FIX_2026-09-13.md](docs/ROLE_SEGMENT_FIX_2026-09-13.md)。
- **文档同步**：行为变更需同步更新本文档相关章节；跨会话的待办、决策与已知怪状态记录在 [docs/DEV_NOTES.md](docs/DEV_NOTES.md)
- **原生选区读取**：`python tools/test_selection_native.py` 创建自有隐藏 Win32 Edit 控件，真实调用 UIA 验证选区、长度上限、空光标与资源释放；不发送键盘输入、不写剪贴板。仅焦点定位替换为自有控件，不能代替用户桌面的 Windows Terminal/TUI 验收；因此不进离线流水线。

---

## 常见修改指南

| 修改目标 | 涉及文件 |
|---|---|
| 添加/修改快捷键 | `config_client.py` (shortcuts 列表) |
| 修改快捷键行为逻辑 | `shortcut/event_handler.py`, `shortcut/task.py` |
| 修改按键映射 | `shortcut/key_mapper.py` |
| 修改按键补发逻辑 | `shortcut/emulator.py` |
| 修改钩子事件处理 | `shortcut/shortcut_manager.py` |
| 修改录音打断行为 | `config_client.py` (`cancel_recording_key`)、`shortcut/shortcut_manager.py` (`_handle_cancel_key`)、`shortcut/task.py` (`abort`)、`audio/recorder.py` (abort 分支) |
| 修改连续对话行为 | `config_client.py` (`continuous_chat_key` / `continuous_chat_timeout`)、`core/client/state.py` (`ContinuousChatSnapshot` 与 `continuous_*` 方法)、`shortcut/shortcut_manager.py` (`_handle_chat_key` / `_resolve_chat_key`)、`shortcut/task.py` (`_prepare_continuous` / `finish` / `cancel`)、`output/result_processor.py` (`_resolve_forced_role` / `_update_continuous_state`)、`llm/llm_handler.py` (`forced_role` / `resolve_role`) |
| 修改连续对话的指示器配色/文案 | `core/client/ui/recording_indicator.py` (`CONTINUOUS_*` 常量、`_compose_text`、`_panel_colors`) |
| 修改视觉输入行为 | `config_client.py` (`vision_capture_key` / `vision_clipboard_max_age`)、`core/client/vision/clipboard_watcher.py` (`POLL_INTERVAL`)、`core/client/vision/clipboard_image.py` (`MAX_EDGE_PX` / `MAX_IMAGE_BYTES` / `JPEG_FALLBACK_QUALITY` / 格式优先级)、`core/client/vision/vision_manager.py` (快捷键校验与线程)、`core/client/vision/image_archive.py` (归档命名与清理，配 `vision_archive_keep`)、`core/client/vision/notice.py` (终端提示去重)、`core/client/state.py` (`pending_image` 与 `*_image_snapshot` 方法)、`output/result_processor.py` (`_report_image_usage`)、`llm/llm_handler.py` (`image_data` / `image_used`) |
| 修改带图请求的消息格式 | `llm/llm_message_builder.py` (Chat 口味产出)、`llm/llm_processor.py` (`_to_responses_content`) |
| 给某个角色开启视觉 | 角色文件加 `enable_vision = True` 并把 `model` 换成视觉模型（两者都要改） |
| 修改挂图时的指示器描边 | `core/client/ui/recording_indicator.py` (`IMAGE_BORDER` / `IMAGE_BORDER_WIDTH` / `IMAGE_BORDER_INSET` 三个常量，以及 `_render` 里画内嵌环那句 `_rounded_rect`) |
| 修改服务端地址 / 多候选探测 | `config_client.py` (`addr` / `port` / `addr_probe_timeout` / `addr_probe_timeout_lan`)、`connection/websocket_manager.py` (`resolve_server_endpoints` / `connect` / `_probe_timeout`)、`core/client/ui/tips.py` (`_get_server_display`) |
| 修改录音/音频发送 | `audio/recorder.py`, `audio/stream.py` |
| 修改识别结果处理 | `output/result_processor.py` |
| 修改文本输出方式 | `output/text_output.py` |
| 添加 LLM 角色 | 在 `LLM/` 目录新建 .py 文件 |
| 把某个角色在 DeepSeek / CPA 网关之间切换 | 该角色文件里的 `backend = 'deepseek'` 或 `'cpa'`（存盘即热生效，见约定 23） |
| 换 CPA 网关用哪个模型 / 改网关地址 | `LLM/backends.py` 的 `CPA_MODEL` / `CPA_URL`（改一处，所有走 cpa 的角色一起变） |
| 新增一套后端 | `LLM/backends.py` 的 `PROFILES`，密钥加进 `LLM/secrets.py`（未被 git 跟踪） |
| 修改 LLM 处理逻辑 | `llm/llm_handler.py`, `llm/llm_processor.py` |
| 修改选区捕获、兼容回退或终端预览 | `config_client.py` (`selection_mode`)、`core/tools/windows_selection.py`、`llm/llm_get_selection.py`、`llm/llm_handler.py`、`llm/llm_output_*.py`、`output/result_processor.py`；验证 `tools/test_selection.py` 与 `tools/test_selection_native.py`（约定 26） |
| 修改中断键的生效条件 | `config_client.py` (`llm_stop_key`)、`llm/llm_stop_monitor.py`（两层闸门）、`llm/llm_handler.py`（`process` 的回调包装是唯一武装点、`process_and_output` 的 try/finally 是唯一作用域），验证跑 `tools/test_stop_gate.py`（见约定 25） |
| 开关角色联网搜索 | 角色文件的 `enable_web_search`/`enable_datetime`/`request_timeout`/`api_url`；实现在 `llm/llm_processor.py` 的 `_build_responses_params` 与 `_process_responses_stream` |
| 修改热词匹配 | `hotword/hot_phoneme.py` |
| 修改 UI 悬浮提示 | `core/client/ui/recording_indicator.py` |
| 修改 Toast 弹窗 | `core/ui/toast.py`, `core/ui/toast_manager.py`, `core/ui/toast_base.py`, `core/ui/toast_text.py`, `core/ui/toast_label.py` |
| 调整 Toast 顶部留白 / 拖动区 | `core/ui/toast_constants.py` 的 `DEFAULT_PADDING_TOP`(LLM 白框) / `COMPACT_PADDING_TOP`(普通 toast)；分岔在 `toast_base.py` 的 `padding_top`，验证跑 `tools/test_toast_padding.py`（约定 17） |
| 修改系统托盘 | `core/ui/tray.py`, `core/client/manager/tray_manager.py` |
| 修改 ASR 引擎 | `core/server/engines/` 对应子目录 |
| 修改识别流水线 | `core/server/worker/pipeline.py` |
| 修改文本拼接算法 | `core/server/merger/` |
| 修改 C/S 通信协议 | `core/protocol.py` + 两端收发代码 |
| 修改 Companion 通信协议 | `core/client/companion/companion_protocol.py` |
| 修改 Companion 服务器行为 | `core/client/companion/companion_server.py` |
| 启用/禁用 Companion | `config_client.py` (`companion_enabled`, `companion_port`) |
| 配置/修改 OpenMate 接入 | `config_client.py` (`openmate_*`), `llm/llm_output_openmate.py`；角色级覆盖字段 `openmate_enabled`/`openmate_character`/`enable_result_splitter`/`result_split_delimiter` |
| 修改状态标签的识别/剥离 | `llm/llm_state_tags.py`（`STATE_TAG_RE` / `strip_state_tags` / `StateTagFilter`）；新增输出模式时**必须**在展示前剥离，流式路径用 `StateTagFilter`（见约定 22） |
| 跑验证 / 加测试 | `tools/`（`run_tests.py` 一把梭、`probe_llm_live.py` 真发请求），说明见 `tools/README.md` |

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
