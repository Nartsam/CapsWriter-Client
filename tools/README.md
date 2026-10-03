# tools/ — 开发期验证脚本

本目录**不参与运行时**，`start_client.exe` 不会碰它。全部脚本用系统 Python 3.8+
直接跑，零第三方依赖。

本目录是打包后的 Release，系统 Python 没有项目依赖，`import core` 会在包
`__init__` 里连锁导入 rich 而失败。所以这里的测试一律走「预注入 `sys.modules`
桩模块 + `importlib` 按文件路径加载」，测的仍是仓库里的真实代码。

## 一把梭

```
python tools/run_tests.py
```

跑完语法检查 → 未定义名字检查 → 桩模块单测，全程离线、不花额度，退出码非 0
表示有失败项。**改完代码提交前跑这个。**

## 各脚本

| 脚本 | 作用 |
|---|---|
| `run_tests.py` | 上面那条流水线的入口 |
| `check_undefined.py` | AST 作用域链检查（迷你 pyflakes）。`ast.parse` 只验语法、抓不到 `NameError`，而「逐层转发的参数改错了函数」这类 bug 常常要开着某个外部程序才走得到那条分支 |
| `undefined_baseline.txt` | 上面那个检查器的已知豁免清单 |
| `test_vision.py` | 视觉输入：剪贴板解码、DIB→BMP、待挂图片状态机、时效判定、带图消息构建 |
| `test_state_tags.py` | `StateTagFilter`。重点是**穷举分片切割位置**——标签被切在任意一处都必须被完整吃掉 |
| `test_openmate_segments.py` | OpenMate 分段发送：纯标签段随下一段正文发送、尾部标签丢弃、跨分片一致性、首段及时交付、取消与失败回退；真实发送线程和请求构造，HTTP 在内存中收集 |
| `test_history_payload.py` | 对话历史的上行载荷：不带 `timestamp`、不交出活引用、assistant 保留状态标签、旧图降级 |
| `test_selection.py` | 选区捕获：默认不发键/不碰剪贴板、回退条件、焦点与超时、Scintilla 范围/编码/标量消息边界、历史去重预览、typing/toast/openmate 单次快照转发、终端输出顺序；所有输入与外部接口均为桩 |
| `test_stream_output.py` | 流式上屏：逐句打字与热词、开头角色名改为松手后处理、角色名被切开时等下一句、打断后不再打字、旧服务端（无 `delta`）按整段处理、去末尾标点只作用于最后一句；另含角色文件导入失败的日志（出自 secrets.py 时不记异常信息）。ResultProcessor 等的真实方法绑到桩对象上运行 |
| `test_shortcut_keys.py` | 快捷键钩子：打断键按住连发不放行；按住右 Ctrl 录音时远控软件注入的左 Ctrl 按下被拦截、松开放行，打断后到松开录音键前同样拦截，单击模式不拦。按键序列取自 `probe_keys.py` 在 RayLink 下的实录；真实的过滤函数、事件处理器与 `abort` 绑到桩对象上运行 |
| `test_selection_native.py` | 原生 Windows 冒烟：自有隐藏 Edit 真实 UIA 读取，加自有 Scintilla 协议窗口验证只读 Win32 消息链路；后者不是真实 Scintilla 库。覆盖 Unicode、长度上限、空光标和剪贴板序号不变；仅替换焦点定位，无键盘输入或剪贴板写入。需 Windows 会话，不进流水线，不能替代 Notepad4 / Windows Terminal/TUI 验收 |
| `probe_selection.py` | 只读诊断真实前台 UIA 父链、控件类型、进程号与选区长度；不打印选区正文，不发送输入或读写剪贴板。`--wait 10` 留出手动选择时间，`--depth 16` 可复现旧上限，`--all-patterns` 检查各祖先的选区长度（该模式最后的 status 不代表捕获结果）；`--samples 23 --interval 2` 可连续观察焦点/选区变化，控件 Name 仅按编辑器/终端标签分类，不输出原文。需要用户桌面访问，沙箱隔离桌面可能返回 HWND=0 |
| `probe_keys.py` | 只读记录按键时序：低级钩子看到的每个键（含注入标记，排在客户端钩子之前，客户端拦截的也看得到）、系统按键状态里修饰键与 Esc 的变化（只反映没被拦截的）、鼠标按键、前台窗口与菜单模式、焦点在 Scintilla 上时选区个数与模式的变化。字符键只记「字符键」。先启动客户端再运行，复现后 Ctrl+C，日志在 `logs/probe_keys.log`。需要用户桌面，不进流水线 |
| `test_backends.py` | LLM 后端切换（`LLM/backends.py`）：两套 profile 取值、未知名字回退、`backends.py` 必须排在角色文件之前（热切换的前提）、四个角色在 `backend='deepseek'` 下与改造前逐字相同 |
| `test_toast_padding.py` | Toast 顶部留白分岔：LLM 白框 40px 拖动区 / 普通 toast 15px、画布坐标与窗口高度、`MIN_WINDOW_HEIGHT` 兜底未触发、判别器与 `close_llm_windows` 同源。**真构造 Tk 窗口、会闪窗**，不进流水线，见下 |
| `test_toast_scaling.py` | Toast 线程的 `tk scaling` 被显示配置变更改掉后能自动恢复：启动真实的 `ToastMessageManager`，只向本进程的 Toast 线程发 `WM_DISPLAYCHANGE`；先关掉修复确认能触发字号放大，再验证恢复、日志与「先恢复再构造窗口」。**会闪窗**，不进流水线 |
| `probe_llm_live.py` | **真发请求、消耗额度**，单独跑。见下 |
| `probe_role_segments.py` | **真发请求、消耗额度**。用当前两套后端检查雾切响子或新岛真的分条和语气；默认仅测当前提示词，`--baseline` 可读归档提示词复现对照。生产发送器只在内存中收集消息，不向 OpenMate 投递。2026-09-13 调整前的原提示词归档在 git 历史里（提交 521f482 的 `docs/experiments/*.json`），做改前/改后对照时用 `git show` 取到仓库内的新文件（如 `logs/` 下）再传给 `--baseline`；输出目录必须为空 |

## 为什么两个 Toast 测试不进流水线

`test_toast_padding.py` 与 `test_toast_scaling.py` 真的建 `tk.Tk()` 和 Toplevel——
留白只有落到画布坐标和窗口几何上才算数，缩放比例只有真的收到窗口消息才会被改，
都桩不出来。代价是需要桌面会话、屏幕上会闪几个无边框窗口，而 `run_tests.py`
要能在没有桌面的环境里跑完，所以单列：

```
python tools/test_toast_padding.py
python tools/test_toast_scaling.py
```

改完 `core/ui/toast_*.py` 手动跑一次。它们不联网、不花额度。

## 原生选区冒烟

修改原生选区模块后另跑 `python tools/test_selection_native.py`；它使用隐藏的
Win32 控件，不需要 Tk 或第三方依赖，也不夺取用户窗口焦点。

## 为什么需要 probe_llm_live.py

有几条设计结论是押在 DeepSeek 当前行为上的（写在对应代码注释和 DEV_NOTES 里），
本地单测证不了，厂商改了也不会有任何报错，只会表现成某个功能悄悄失灵：

- **P1** 图片能与服务端内置 `web_search` 工具共存（不共存就得给带图请求加降级分支，见 `llm_processor._stream_request`）
- **P2** 历史里的图在后续轮次仍然可见（「只保留最近一张真图」的前提，见 `ContextManager.demote_images`）
- **P3** 情绪标签跨轮保持（历史存原文才有意义，见 `llm_state_tags` 模块说明）
- **P4** `thinking:disabled` 是否仍然「被接受但完全无效」（DEV_NOTES「已知问题」）

```
python tools/probe_llm_live.py          # 全跑
python tools/probe_llm_live.py P2 P3    # 只跑指定项
```

P2/P3 用仓库里**真实的** `RoleConfig` / `MessageBuilder` / `ContextManager` /
`LLMProcessor` 组装请求，只桩掉网络，所以它们同时也是生产链路的回归测试。

密钥从 `LLM.secrets` 读入，脚本从不打印它，输出里也不含 Authorization 头。

## 加新测试时

- 语法检查用 `ast.parse` 而非 `py_compile`——后者会在仓库里留下 `__pycache__`，
  而 pyc 严禁进仓库
- 每个脚本开头写 `sys.dont_write_bytecode = True`
- 路径按 `__file__` 推导仓库根，别用 `os.getcwd()`：脚本要能从任何工作目录跑
- 新测试文件加进 `run_tests.py` 的 `SUITES`
- 涉及 Tk 文字度量的测试，建 Tk 之前先调 `ctypes.windll.shcore.SetProcessDpiAwareness(2)`，
  并断言 `winfo_fpixels('1i') > 96`：不声明 DPI 感知的进程被系统虚拟成 96 DPI，
  量出来的宽度只有实机的一半，「文字没被截断」这类断言会在测试里假通过
