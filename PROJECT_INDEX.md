# CapsWriter-Client 项目索引

> 项目地图：模块在哪、怎么协作、跨模块约束、改什么动哪里。
> 这里不逐步复述代码，也不记历史与测试记录；只和某段代码有关的坑写在那段代码的注释里。硬性约束与文档分工见 [CLAUDE.md](CLAUDE.md)。

## 项目概览

CapsWriter 离线语音输入的**客户端**（Windows）。上游 [HaujetZhao/CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)（v2.6，版本号仍写在 `config_client.py` 的 `__version__`）把客户端和服务端放在一起；这里只维护客户端，服务端在独立仓库 `../CapsWriter-Server`（见「服务端与协议」）。

核心流程：按住快捷键 → 录音经 WebSocket 发给服务端 → 收到识别文字 → 热词与规则替换 → 可选交给 LLM 角色 → 模拟键盘上屏或弹窗展示 → 写日记、存录音。

**发行目录**：`start_client.exe` 是 PyInstaller 启动器（运行时在 `internal/`，Python 3.8），项目代码 `core/`、`config_client.py`、`LLM/` 从源码目录实时加载，改完重启 exe 即生效（`LLM/` 与热词文件支持热重载）。exe 无法在本目录重建。

**便携性是硬约束**：新增代码、依赖、配置与辅助工具都留在本目录内，不修改外部环境——不靠给目标程序装扩展、改它的设置或启动参数、装全局依赖或注册系统服务来解决兼容性问题（选区读取同样适用）。

相对上游的本地功能：流式上屏、录音悬浮指示器（含音量声波条）、录音打断、连续对话、视觉输入、一次性提醒、选区只读获取、服务端多候选、Companion 服务器；LLM 角色的 OpenMate 展示（情绪立绘、分段、typing 指示）、联网搜索与工具标签、后端切换、Toast 自适应宽度与智能时长、空回复诊断；`tools/` 离线验证。

---

## 目录结构

```
├── start_client.exe        # 启动器（无法重建）；把文件拖到它上面 = 文件转录模式
├── internal/               # 冻结运行时：Python 3.8 + 第三方库，按字节原样跟踪
├── config_client.py        # 唯一的客户端配置（ClientConfig 类属性，另有快捷键写法说明）
├── LLM/                    # 角色：每个 .py 一个角色；backends.py = 后端切换，secrets.py = 密钥（禁止读取）
├── hot.txt / hot-rule.txt  # 热词（音素模糊匹配）/ 正则规则，热重载
├── core/
│   ├── protocol.py         # C/S 协议 AudioMessage / RecognitionMessage —— 与服务端仓库逐字相同
│   ├── constants.py        # 音频格式（16kHz float32 单声道）、标点集合 —— 与服务端仓库逐字相同
│   ├── logger.py           # 文件按 log_level 记（logs/client_latest.log，超 10MB 截断），控制台固定 WARNING+
│   ├── client/             # 客户端各子系统（见「模块地图」）
│   ├── ui/                 # Toast、托盘、上下文/热词对话框、DPI 声明
│   └── tools/              # 通用工具（清单见 core/tools/__init__.py；clean_assets.py 是独立脚本，不被客户端导入）
├── tools/                  # 开发期验证脚本，不参与运行时（tools/README.md）
├── docs/                   # 功能使用说明 + DEV_NOTES.md
├── assets/                 # icon.ico、demo.png
└── logs/  2026/ …          # 运行时生成：日志、提醒队列、视觉归档、角色记忆；按 年/月 存的日记与录音（不进 git）
```

---

## 运行时架构

| 线程 | 内容 |
|---|---|
| 主线程 | asyncio 事件循环：ResultProcessor 收结果、AudioRecorder 发音频、WebSocket、Companion 服务器、提醒调度 |
| pynput 键盘/鼠标 Listener | `ShortcutManager` 的 `win32_event_filter`（低级钩子：录音键、打断键、连续对话切换键） |
| GlobalHotKeys Listener（单例） | `llm_stop_key`（StopMonitor）与 `vision_capture_key`（VisionManager）共用 |
| sounddevice 回调 | 48kHz 采集，块数据投进 `state.queue_in`；AudioRecorder 降采样到 16kHz。录音时每块另算一次电平喂给悬浮指示器的声波条（只做一次赋值，不经命令队列） |
| Tk 线程（各自一个解释器） | 录音悬浮指示器、Toast 管理器、托盘菜单打开的上下文对话框 |
| 其他 | 托盘（pystray）、快捷键线程池（补发按键、打断收尾）、LLM 请求（`to_thread`）、视觉单线程 executor、OpenMate 发送线程、选区读取线程、剪贴板序号轮询、watchdog（热词文件、`LLM/` 目录） |

两种模式：无参数启动为麦克风模式（MicRunner）；命令行带文件（拖放）为文件模式（FileRunner），逐个处理后等回车退出。

---

## 模块地图

表中没写全的路径都相对 `core/client/`。具体坑与原因写在各文件头和相关函数的注释里，改之前先读。

### 入口、状态与快捷键

| 文件 | 职责 |
|---|---|
| `core/client/app.py` | `CapsWriterClient` 门面：`__init__` 切到项目根并创建全部子系统，`start()` 选运行器，`stop()` 统一释放 |
| `manager/mic_runner.py` | 启动顺序：托盘 → 启动提示 → 音频流 → 快捷键 → UDP 控制 → 热词 → LLM → 视觉输入 → Companion → 提醒恢复 → ResultProcessor |
| `manager/file_runner.py` | 媒体文件走 FileTranscriber；`.txt/.json/.srt/.vtt` 走 SrtAdjuster 按字级时间戳重生成字幕 |
| `manager/tray_manager.py`、`core/ui/tray.py` | 托盘菜单：复制结果、上下文（编辑发给服务端的 `context`）、热词（打开 hot.txt）、清除记忆、重开音频、显示/隐藏、重启、退出 |
| `state.py` | `ClientState`：音频队列、连接、录音状态；打断用的 `pending_task_id`/`cancelled_task_ids`；连续对话与图片的每句话路由快照；等待结果的任务登记（`awaiting_results`，转交录音时长） |
| `task_line.py`、`core/tools/task_id.py` | 麦克风任务在控制台的第一行（任务标识、录音时长、时延，或打断/出错说明）；任务标识 `8 位随机十六进制数_月-日_时-分-秒`，三种任务共用（格式约束见其文件头） |
| `shortcut/shortcut_manager.py` | 低级钩子入口：防自捕获、录音键分发与前台进程让键（`_route_shortcut`）、打断键（`_handle_cancel_key`）、连续对话切换键（`_handle_chat_key`，配置校验在 `_resolve_chat_key`） |
| `shortcut/event_handler.py`、`task.py` | 长按/单击两种模式；`ShortcutTask.launch/finish/cancel/abort`（建立快照、显示指示器、驱动 AudioRecorder、作废回滚） |
| `shortcut/emulator.py`、`key_mapper.py` | 补发被拦截的按键、修饰键释放（为何要补发见 `MODIFIER_KEYS` 上方注释）；VK 码与按键名互转 |

### 音频、连接与结果处理

| 文件 | 职责 |
|---|---|
| `audio/stream.py`、`recorder.py`、`file_manager.py` | 采集；按 `AudioMessage` 分块发送、打断时丢缓存并仍发 `is_final`；录音存 MP3/WAV 并按识别文本改名（`年/月/assets/`） |
| `connection/websocket_manager.py` | 连接与重连。`addr` 可写候选列表：未锁定时一次 `connect()` 内按序探测，首个连上即锁定（取舍见类说明） |
| `output/result_processor.py` | 主循环：收结果 → 繁简 → 热词 → 去末尾标点 → 正则 → LLM 或直接上屏 → 自动回车 → 日记与录音改名 → LLM md 记录 → 报告图片去向 → 连续对话状态迁移；流式上屏的逐句打字（`_on_stream_delta` / `_finish_stream`，输出后的收尾与整段共用 `_after_output`） |
| `output/stream_output.py` | 流式上屏单次录音的状态（待判定 / 逐句上屏 / 改为松手后处理）与取舍说明 |
| `output/text_output.py`、`clipboard/clipboard.py` | 打字（`keyboard.write`）或粘贴输出；剪贴板读写（`try_paste` 区分「读不到」与「空文本」，占用时退避重试） |
| `hotword/` | `manager.py` 加载并监视 hot.txt/hot-rule.txt；`hot_phoneme.py` 音素 RAG（粗筛 + 精算）；`hot_rule.py` 正则替换 |
| `diary/diary_writer.py`、`llm/llm_write_md.py` | `年/月/日.md` 听写日记（`save_audio` 开启时写）；`年/月/日-角色.md` 角色对话记录 |
| `transcribe/` | 文件转录：FFmpeg 解码 → 分段发送 → 收 tokens/timestamps → 存 srt/txt/json；SrtAdjuster 用 json 时间戳重对齐改过的 txt |
| `udp/`、`companion/` | UDP 控制录音（START/STOP）与结果广播；Companion WebSocket 服务器（默认 6019，事件 `recording_start/end/cancel`、`user_message`）。Companion 目前没有已知的对端应用 |
| `ui/recording_indicator.py`、`ui/tips.py` | 悬浮指示器（Tk 单例；录音/等待/进度/处理/打断/错误态，连续对话底色、挂图描边）；左侧声波条录音时随音量起伏、松手后变加载波浪，独立约 30 帧/秒的动画循环，音量由 `LevelNormalizer` 按底噪线与说话峰值线自适应换算（不同麦克风电平差 20 dB 也一致）；启动时的控制台提示 |

### LLM 角色（`core/client/llm/`）

| 文件 | 职责 |
|---|---|
| `llm_handler.py` | 协调器。`process_and_output()`：角色检测（连续对话时跳过）→ 提醒入口 → Companion 转发 → 读一次选区 → 按 `output_mode` 分发；`process()` 包装回调（OpenMate / typing 全局中断键的唯一武装点）；`resolve_role()` 供连续对话按名重新解析；记忆按角色 `module_name` 区分，热重载沿用同一个 `ContextManager`，`clear_history()` 清内存并删记忆文件 |
| `llm_role_config.py`、`llm_role_loader.py`、`llm_role_detector.py`、`llm_watcher.py` | 角色字段定义；按文件名顺序加载 `LLM/*.py`；`startswith` 前缀匹配；目录变化 3s 防抖后重载（保留历史） |
| `llm_message_builder.py`、`llm_context.py` | 组装 system + 历史 + 当前时间/热词/选区/用户输入（带图时为 Chat 口味多模态 content）；每角色一份历史，超 `max_context_length` 80% 从旧裁剪，旧图降级为 `［图片］`；每轮整体落盘（`add_turn`），启动时 `restore()` 读回 |
| `llm_memory.py` | 记忆文件：`logs/llm_memory/<角色 .py 文件名>.json`，先写临时文件再替换；读不了的改名为 `.bad-时间.json` 保留；图片落盘为占位符（由 `ContextManager` 转换） |
| `llm_processor.py`、`llm_client_pool.py` | 四条流式路径：Ollama 原生、Anthropic Messages（`api_format='anthropic'`）、Responses（`api_format='responses'` 固定使用；旧 `openai` 格式开启搜索时也走这里）、Chat Completions；`thinking_level` 按协议转换；工具事件 → `used_tools`；空回复/截断/拒答诊断；Anthropic 流内错误进入统一响应错误处理，本轮不写记忆；每次请求 INFO 一行「请求构成」 |
| `llm_anthropic.py` | Anthropic 格式：内部 Chat 口味消息的转换，及基于 httpx 的极简客户端（SSE 解析、HTTP 状态映射到与 openai 路径相同的包装异常）；请求重试策略见 [DEV_NOTES 的有效决策](docs/DEV_NOTES.md#仍然有效的决策) |
| `llm_output_typing.py`、`llm_output_toast.py`、`llm_output_openmate.py` | 三种输出：打字/粘贴；本地 Toast（以「正在思考…」占位弹出，工具状态与正文整段取代它）；OpenMate 看板（探测 `/health`，本机端口预查；情绪指令注入、流式或分段队列、typing 指示与工具状态文案，状态和正文共用发送线程；失败回退本地 Toast） |
| `llm_state_tags.py` | `<state:…>` 情绪标签的识别与剥离；流式用 `StateTagFilter`。模型偶尔补上的结束标签 `</state:…>` 本地一并剥掉，OpenMate 路径只去结束标签（`close_only`） |
| `llm_stop_monitor.py` | OpenMate / typing 的全局中断键两层闸门；本地 Toast 使用独立 `ToastRequest`，跨线程共享取消状态、互斥确定完成与取消 |
| `llm_get_selection.py` | 按 `selection_mode` 获取选区，`SelectionContext` 区分本轮附加文字与终端预览，按角色相邻轮次去重 |
| `llm_error_handler.py` | 错误分类：认证/连接/响应错误弹红色错误框；超时、限流及其他未归类异常降级为把识别原文打字输出 |
| `LLM/backends.py` | 角色文件只写 `backend = 'deepseek' \| 'custom'`，模型/地址/请求格式/超时由 `resolve()` 给出（约束见其文件头）；DeepSeek 走 Anthropic 兼容接口，custom 固定走 Responses；配置与迁移见 [角色功能说明](docs/角色功能如何使用.md#九切换后端deepseek--custom) |

当前角色文件：`default.py`（typing，已禁用）、`助理.py`、`翻译.py`（当前禁用）、`雾切响子.py`、`新岛真.py`（后四个为 toast；两个人设角色开启 OpenMate 分段，按单换行切）。内置角色的本地 Toast 宽度配置已统一，取值与优先级见 [角色功能说明](docs/角色功能如何使用.md#toast-模式)。

### 视觉输入（`core/client/vision/`）与一次性提醒（`core/client/reminder/`）

| 文件 | 职责 |
|---|---|
| `vision_manager.py` | 注册 `vision_capture_key`，回调只 submit；工作线程做时效检查、重复挂载拦截、读取、归档、写入待挂槽 |
| `clipboard_image.py`、`clipboard_watcher.py` | 读剪贴板图片（PNG → CF_DIBV5 → CF_DIB，缩放、PNG/JPEG 编码与体积上限）；轮询剪贴板序号记录变更时刻 |
| `image_archive.py`、`notice.py` | 挂载图片存 `logs/vision/` 并按去向改名；终端提示去重 |
| `reminder/service.py` | 协调：预筛 → 隔离的非流式语义分析（`analyzer.py`）→ 本地校验（`model.py`）→ 原子存储（`store.py`，`logs/reminders.json`）→ 调度 → 投递（`output.py`：内部状态窗、角色 Toast 或 OpenMate 批次）；`fullscreen.py` 判断前台全屏/锁屏 |
| `core/ui/toast_reminder.py` | 内部状态专用窗：底部固定、固定时长、不可拖动 |

提醒的用户行为、配置与已接受的限制见 [docs/提醒功能如何使用.md](docs/提醒功能如何使用.md)。

### UI 与工具

| 文件 | 职责 |
|---|---|
| `core/ui/toast_manager.py` | Toast 管理器（Tk 单例线程）：消息队列、窗口生命周期、`close_llm_windows()`、显示配置变更后恢复 `tk scaling`（`_ensure_tk_scaling`） |
| `core/ui/toast_base.py`、`toast_text.py`、`toast_label.py`、`toast_constants.py` | 窗口基类（无边框置顶、透明圆角、宽度只增不减、拖动、滚轮、复制、Markdown 最终视图、智能时长）；Text 版用于流式（续写只追加，不是续写时整段替换）、Label 版用于普通提示 |
| `core/ui/dpi.py` | 进程 DPI 感知统一声明（固定 SYSTEM 级，原因见文件头） |
| `core/tools/windows_selection.py` | ctypes 调原生 UIA（独立 MTA 线程、焦点及祖先的 TextPattern），UIA 不支持时对 Scintilla 焦点控件做标量只读查询 |
| `core/tools/window_detector.py` | 前台窗口进程名，供 `paste_apps`/`enter_apps`/`trash_punc_apps` 匹配；录音键用 `get_foreground_process_name` 只查 HWND/PID/进程名，查询失败或期间切换前台时返回空串 |

---

## 数据流

**麦克风识别**：钩子 KEYDOWN → 按前台进程判断归属（让键时直接放行，本机不进入任务）→ `ShortcutTask.launch()`（连续对话闲置判断、建快照、绑定待挂图片、显示指示器）→ `AudioRecorder.record_and_send()`（生成 task_id、登记快照、过阈值后分块发送）→ KEYUP：超过阈值 `finish()` 发 `is_final`，否则 `cancel()` 并补发按键 → 服务端逐段回 `RecognitionMessage`（非最终包只刷新指示器进度）→ 最终包进 ResultProcessor 流水线（见上表）。

**流式上屏**（快捷键配置 `streaming`，用法与限制见 [docs/流式上屏如何使用.md](docs/流式上屏如何使用.md)）：`launch()` 按快照判定这次录音是否流式（连续对话中、挂图、默认角色启用时不流式）→ `AudioMessage.streaming` → 服务端在停顿处切句、逐句回带 `delta` 的非最终包 → `_on_stream_delta`：第一句命中角色就整次改为松手后处理（之后只攒不打），否则逐句做繁简/热词/规则并打字 → 最终包只带最后一句，`_finish_stream` 打出后走与整段相同的收尾。没收到过 `delta`（旧服务端、只有一句）的最终包一律按整段处理。

**打断**：打断键在录音中 → `task.abort()`（登记 `cancelled_task_ids`、回滚快照、`queue_in` 放 abort，recorder 丢缓存、删录音、仍发 `is_final`）；在等待结果中 → `_abort_pending_result()`。长按模式下打断后到松开录音键之前（`task.wait_release`），录音键的按下不重新开始录音，同族修饰键的按下照样拦截（见下面约定 3）。被打断任务的结果在 `_handle_message` 入口整条丢弃。最终包到达后打断窗口关闭（文本随时可能上屏）。

**LLM**：`process_and_output` → 提醒入口（仅录音入口的具名角色）→ Companion（有连接时转发并返回）→ `capture_selection`（一次，结果经 `on_selection` 先打印预览）→ `handle_toast_mode` / `handle_typing_mode`（toast 模式先试 OpenMate）→ `LLMHandler.process` → `LLMProcessor` 流式请求 → 回调逐层回到展示 → `LLMResult`（`used_tools`、`image_used`、`reminder_handled`）回到 ResultProcessor。

**文件转录**：FileRunner → `FileTranscriber.check()`（FFmpeg + 连接）→ `send()`（FFmpeg 输出 16kHz f32le，按 `file_seg_*` 分段参数发送）→ `receive()`（热词替换）→ `ResultHandler` 存 srt/txt/json。

---

## 跨模块约定

1. **门面与单例**：`CapsWriterClient` 是唯一入口；RecordingIndicator、ToastMessageManager、GlobalHotkeyManager 是线程安全单例。
2. **低级钩子回调只做状态翻转和派发**：超过约 300ms 会被系统摘钩，此后所有快捷键失效。耗时操作一律 submit 到线程池（打断收尾、补发按键、截图读取都是如此）。
3. **拦截决策在 KEYDOWN 锁存、KEYUP 沿用**：判据会被按键动作本身翻转，两次各自判断会产生不配对的按下/松开。打断键与连续对话切换键只在录音阶段拦截。长按的录音键是修饰键时，按住期间同族修饰键的按下也拦截（`_is_modifier_sync`）：系统里这个修饰键是松开的，RayLink 等远控软件会注入一个不配对的按下来「同步」，放行就卡住。
   录音键的进程让行由 `_shortcut_routes` 锁存，放行分支必须绕过整个事件处理器（含短按/修饰键补发）；单击模式已有本机录音时保留结束入口。名单为空时不查询前台，启用后只在首次按下且需要决定归属时查询；配置与用户行为见 [进程让键说明](docs/常见问题.md#如何让指定程序自行处理录音快捷键)。
4. **GlobalHotKeys 共用一个 Listener**：注册新热键会重启它；一个非法组合会让整个 Listener 起不来、连带 `llm_stop_key` 失效，所以注册前自行 `HotKey.parse` 校验。
5. **多 Tk 解释器**：创建 `tkinter.font.Font` 必须传 `root=` 指向所属窗口，否则命名字体建到进程里第一个 Tk 上，跨解释器引用时静默回退为小号宋体。
6. **日志**：控制台处理器固定 WARNING，诊断一律走 INFO（升级等于往运行终端刷字）；用户需要当场看见的结果用 `console.print`。模块用包内的 `from . import logger`，`logging.getLogger(__name__)` 不在项目日志器树上，写不进 `logs/`。
7. **每句话快照路由**：连续对话的粘滞角色与待挂图片都在 `launch()` 绑定到本次录音、拿到 task_id 后登记、最终结果到达时 pop，不读结果到达时的全局状态。任何让录音作废的路径（短按取消、录音或等待阶段打断）都要回滚本次绑定。
8. **新增输出模式或流式路径时的清单**（漏了都不报错）：逐层转发 `used_tools`、`image_data`、`selection`（按函数核对，而不是按调用点）；展示前剥离情绪标签（流式用 `StateTagFilter`）；空正文告警与终局状态解析；中断分支留 INFO 终局记录；toast/OpenMate 空回复返回空串而不是识别原文（typing 模式的降级是真打出去了，保留）。
9. **情绪标签只在展示层剥离**：对话历史存模型原文（历史里剥掉会让模型从第二轮起不再输出标签）；Toast、打字、md 记录、UDP 广播各自在展示前剥。
10. **`smart_duration` = 「LLM 角色白框」**：同一个标志决定智能时长、40px 顶部拖动区和 `close_llm_windows()` 的顶替范围；新 LLM 回复（含 OpenMate 接管）先顶掉所有旧白框。
11. **剪贴板**：之后要写回剪贴板的读取必须用 `try_paste()`，`safe_paste()` 会把「读不到」折叠成空串、写回时清掉原内容（典型是待挂的截图）。只读选区模式（`selection_mode='uia'`）不碰剪贴板，复制模式与粘贴输出仍会写。
12. **配置读取**：本地新增的 `ClientConfig` 项在代码里用 `getattr(Config, 名, 默认值)` 读取（多数还会校验取值、非法时回退默认），缺项时照常运行；新增配置照此办理。角色文件未写的字段取 `RoleConfig` 默认值。
13. **协议、搜索与思考**：custom 的普通回复和提醒解析均由 `api_format='responses'` 固定路由；`enable_web_search` 只决定是否挂 `web_search`（事件是 `<工具>_call`）。`anthropic` 挂 `web_search_20250305`（事件是 `server_tool_use` 块），DeepSeek 保持此格式（`tools/test_backends.py` 钉住）；旧 `openai` 路由仍在开启搜索时转 Responses。检索期数秒无正文，必须设 `request_timeout`。参数差异收敛在 `_build_responses_params` 与 `_build_anthropic_params`：`thinking_level` 分别转成 `reasoning.effort`、`thinking` + `output_config.effort`；DeepSeek / custom 的提醒分析独立使用后端默认思考，不传角色档位或额外选项。
14. **本地 Toast 取消隔离**：`toast_request` 从输出层经 Handler 传到 Processor；线程认领完成后才提交记忆，取消后不再更新窗口、选区去重或历史。输出层立即结束本地等待，底层网络线程收尾只读自己那一轮的状态；`LLMResult.cancelled` 让 ResultProcessor 跳过角色归档和自动回车。点击与 Esc 的用户行为见 [角色功能说明](docs/角色功能如何使用.md#toast-模式)。
15. **一个任务的控制台输出排在一起**：麦克风任务的全部输出只在结果处理里打印（接收循环逐条 `await _handle_message`，含 LLM 回复，天然串行），录音协程松手和打断时只登记 `awaiting_results`，不打印；被打断的任务在丢弃回包处打一行。例外是确定等不到结果的任务：未连接、发送失败由录音协程当场打，等待中断线由 `_cleanup` 补打，登记表谁取出谁打印。新增任务相关的控制台输出放进结果处理的流程里；不属于任何任务的提示（挂图、热重载、连接状态、警告日志）照旧当场打印。

> 旧编号对照：`LLM/backends.py` 注释与 `tools/probe_llm_live.py` 输出文字里仍有「约定 15/21/22」，分别指本节第 13 条、视觉输入（`core/client/vision/`）、第 9 条。

---

## 服务端与协议

- 服务端在独立仓库 `../CapsWriter-Server`（Qwen3-ASR 识别，文件转录用 Qwen3-ForcedAligner 出字级时间戳），部署与配置看那边的文档。上游遗留的服务端代码已从本仓库删除。
- **`core/protocol.py`、`core/constants.py` 与服务端仓库逐字相同**，改协议必须两边同步。
- 客户端发 `AudioMessage`（JSON 文本帧，`data` 为 base64 的 float32 16kHz 单声道 PCM），带 `seg_duration`/`seg_overlap`（麦克风与文件分别取 `mic_seg_*`、`file_seg_*`，默认 60/4，分段在服务端做）、`context`（服务端当作 Qwen3 的 system prompt）、`language`。服务端每段回一条 `RecognitionMessage`，最后一条 `is_final=True`；听写用 `text`，文件转录用 `tokens`/`timestamps`。流式任务（`streaming=True`）不用 `seg_*`，服务端在停顿处切句，每句一条消息，新增文字在 `delta` 里且发出后不再改；服务端的切句与前缀续写见服务端仓库 PROJECT_INDEX「流式任务」。子协议 `binary`，`max_size=None`。
- **录音打断不走协议**：服务端的音频缓冲按连接持有、只有 `is_final` 才清空，所以打断仍要发一个 `is_final` 冲掉残余音频，结果回来后在客户端按 task_id 丢弃。没有新增取消消息，是为了不必两个仓库同步部署。

---

## 开发环境与验证

- 系统 Python 没有项目依赖（rich、pynput、sounddevice…），`import core` 会在包 `__init__` 里失败，不要直接导入或运行客户端。测试的做法是预注入 `sys.modules` 桩模块，再用 `importlib` 按文件路径加载目标模块。
- 验证从轻到重：`ast.parse` 语法检查 → 未定义名字检查 → 桩模块单测 → `start_client.exe` 实机。前三步是 **`python tools/run_tests.py`**（离线、零依赖、不花额度），改完代码提交前跑一遍。
- `ast.parse` 抓不到 `NameError`，而逐层转发参数改错函数这类错误常要开着某个外部程序（如 OpenMate）才走得到，由 `tools/check_undefined.py` 补救（看的是有没有新增，已知豁免在 `tools/undefined_baseline.txt`）。
- 不进流水线的脚本：`tools/test_toast_padding.py` 与 `test_toast_scaling.py`（真建 Tk 窗口）、`test_selection_native.py`（真实 UIA）、`probe_selection.py`（诊断前台窗口选区）、`probe_keys.py`（诊断按键、钩子拦截与 Scintilla 选区的时序）、`preview_indicator.py`（真建 Tk，用已保存的录音驱动声波条，会出声）、`probe_llm_live.py` 与 `probe_role_segments.py`（真发请求、消耗额度）。用法见 [tools/README.md](tools/README.md)。
- 不要提交 `__pycache__`/`.pyc`；语法检查用 `ast.parse` 而不是 `py_compile`。

---

## 常见修改指南

| 修改目标 | 涉及文件 |
|---|---|
| 快捷键配置 / 行为 | `config_client.py` 的 `shortcuts`、`shortcut_passthrough_apps`；`shortcut/event_handler.py`、`task.py`；钩子分发与让键 `shortcut_manager.py`；进程查询 `core/tools/window_detector.py`；补发 `emulator.py`；按键名 `key_mapper.py`；验证 `tools/test_shortcut_keys.py` |
| 录音打断 | `cancel_recording_key`；`shortcut_manager._handle_cancel_key`、`task.abort`、`audio/recorder.py` 的 abort 分支、`state` 的打断名单 |
| 连续对话 | `continuous_chat_key/_timeout`；`state.py`（`ContinuousChatSnapshot`、`continuous_*`）、`shortcut_manager._handle_chat_key/_resolve_chat_key`、`task.py`、`result_processor._resolve_forced_role/_update_continuous_state`、`llm_handler`（`forced_role`/`resolve_role`）；配色 `recording_indicator` 的 `CONTINUOUS_*` |
| 视觉输入 | `vision_*` 配置；`core/client/vision/` 各文件；`state` 的 `*_image_snapshot`；`result_processor._report_image_usage`；带图消息格式 `llm_message_builder` 与 `llm_processor._to_responses_content`；描边 `recording_indicator` 的 `IMAGE_BORDER*` |
| 给角色开视觉 | 角色文件 `enable_vision = True`，并换成视觉模型（两者都要） |
| 服务端地址 / 多候选 | `addr`、`port`、`addr_probe_timeout(_lan)`；`websocket_manager.py`；启动提示 `ui/tips.py` |
| 录音与发送 | `audio/recorder.py`、`audio/stream.py` |
| 结果处理 / 输出方式 | `output/result_processor.py`、`output/text_output.py` |
| 控制台的任务行 / 任务标识 | 照「跨模块约定」第 15 条；`task_line.py`、`core/tools/task_id.py`、`state` 的 `awaiting_results`；登记在 `audio/recorder.py`，打印在 `result_processor`（`_print_task_line`、打断丢弃处、`_cleanup`）；验证 `tools/test_task_line.py`、`test_stream_output.py` |
| 流式上屏 | 开关：`shortcuts` 的 `streaming`；是否流式 `shortcut/task.py` 的 `_want_streaming`；逐句上屏与角色回退 `result_processor` 的 `_on_stream_delta` / `_decide_stream_mode` / `_finish_stream`、`output/stream_output.py`；指示器文案 `recording_indicator` 的 `streaming` / `stream_note`；同步 [docs/流式上屏如何使用.md](docs/流式上屏如何使用.md)；验证 `tools/test_stream_output.py`。切句参数在服务端 |
| 热词匹配 | `hotword/hot_phoneme.py`、`hot_rule.py` |
| 新增或修改角色 | `LLM/` 下新建/编辑 .py（热重载）；字段见 `llm_role_config.py` |
| 角色记忆（重启后恢复） | `llm_context.py`（`add_turn`/`restore`/落盘）、`llm_memory.py`（文件格式）、`llm_handler`（`_init_context_managers`、`clear_history`）、`llm_processor._record_turn`（空回复不写）；角色表提示 `llm_role_formatter`；同步 [docs/角色功能如何使用.md](docs/角色功能如何使用.md)；验证 `tools/test_llm_memory.py` |
| 角色后端 / 自定义模型 | 角色文件的 `backend` 与 `thinking_level`；`LLM/backends.py` 的 `CUSTOM_MODEL`、`CUSTOM_URL`、`PROFILES`（每套 profile 给出 `api_format`；`CUSTOM_API_KEY` 在 `secrets.py`，由用户自己改）；验证 `tools/test_backends.py`、`test_anthropic_path.py` |
| LLM 请求与流式解析 | `llm_handler.py`、`llm_processor.py`、`llm_anthropic.py`；联网搜索见「跨模块约定」第 13 条；验证 `tools/test_anthropic_path.py`，厂商行为 `tools/probe_llm_live.py` |
| 新增输出模式 | 照「跨模块约定」第 8 条的清单 |
| 选区获取 | `selection_mode`；`core/tools/windows_selection.py`、`llm_get_selection.py`；验证 `tools/test_selection.py`、`test_selection_native.py` |
| 中断键生效条件 | `llm_stop_key`；`llm_stop_monitor.py`；`llm_handler`（`process` 的回调包装、`process_and_output` 的 try/finally）；验证 `tools/test_stop_gate.py` |
| OpenMate 接入 | `openmate_*` 配置；`llm_output_openmate.py`；角色级 `openmate_enabled`/`openmate_character`/`enable_result_splitter`/`result_split_delimiter` |
| 情绪标签剥离 | `llm_state_tags.py` |
| 一次性提醒 | `reminder_*` 配置；`core/client/reminder/`、`core/ui/toast_reminder.py`；同步 [docs/提醒功能如何使用.md](docs/提醒功能如何使用.md)；验证 `tools/test_reminders.py` |
| Toast 弹窗 | `core/ui/toast_*.py`；顶部留白 `toast_constants.py`；改完跑 `tools/test_toast_padding.py`、`test_toast_scaling.py` 并实机弹一次窗口（构造失败是静默的）；LLM 白框的占位流程验证 `tools/test_toast_status.py` |
| 悬浮指示器 | `core/client/ui/recording_indicator.py`；声波条的最大可见高度（含圆端头）由 `BAR_MAX_H` 控制，尺寸、配色（`BAR_COLOR_MODE`）、动画与音量换算参数都是文件头常量，电平来自 `audio/stream.py` 的 `_audio_callback`；看效果 `tools/preview_indicator.py`，换算与竖条高度验证 `tools/test_level_meter.py` |
| 托盘 | `core/ui/tray.py`、`core/client/manager/tray_manager.py` |
| Companion | `companion_*` 配置；`core/client/companion/` |
| C/S 协议 | `core/protocol.py` + 两端收发代码（**服务端仓库同步修改**） |
| 测试 | `tools/`，新测试加进 `run_tests.py` 的 `SUITES`（见 tools/README.md） |

---

## 主要依赖（均在 `internal/`）

pynput（钩子、模拟按键、全局热键）、keyboard（`keyboard.write` 打字）、sounddevice + numpy（采集）、websockets 13（C/S 与 Companion）、openai / ollama（LLM；Anthropic 格式直接用 openai 依赖的 httpx 发）、rich（控制台）、watchdog（文件监视）、tkinter + markdown + tkhtmlview（指示器与 Toast）、Pillow（视觉输入）、pypinyin（热词音素）、pyclip（剪贴板）、pywin32（窗口检测）、pystray（托盘）、srt / typer（字幕）。外部可执行文件：FFmpeg（文件转录，需在 PATH 或客户端根目录）。
