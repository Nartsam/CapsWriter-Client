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
| `test_history_payload.py` | 对话历史的上行载荷：不带 `timestamp`、不交出活引用、assistant 保留状态标签、旧图降级 |
| `test_backends.py` | LLM 后端切换（约定 23）：两套 profile 取值、未知名字回退、`backends.py` 必须排在角色文件之前（热切换的前提）、四个角色在 `backend='deepseek'` 下与改造前逐字相同 |
| `test_toast_padding.py` | Toast 顶部留白分岔（约定 17）：LLM 白框 40px 拖动区 / 普通 toast 15px、画布坐标与窗口高度、`MIN_WINDOW_HEIGHT` 兜底未触发、判别器与 `close_llm_windows` 同源。**真构造 Tk 窗口、会闪窗**，不进流水线，见下 |
| `probe_llm_live.py` | **真发请求、消耗额度**，单独跑。见下 |

## 为什么 test_toast_padding.py 不进流水线

它是唯一一个真的建 `tk.Tk()` 和 Toplevel 的测试——留白只有落到画布坐标和窗口
几何上才算数，桩不出来。代价是需要桌面会话、屏幕上会闪几个无边框窗口，而
`run_tests.py` 要能在没有桌面的环境里跑完，所以单列：

```
python tools/test_toast_padding.py
```

改完 `core/ui/toast_*.py` 手动跑一次。它不联网、不花额度。

## 为什么需要 probe_llm_live.py

PROJECT_INDEX 和 DEV_NOTES 里有几条结论是押在 DeepSeek 当前行为上的，本地单测
证不了，厂商改了也不会有任何报错，只会表现成某个功能悄悄失灵：

- **P1** 图片能与服务端内置 `web_search` 工具共存（约定 21⑧；不共存就得加降级分支）
- **P2** 历史里的图在后续轮次仍然可见（约定 21⑧「只保留最近一张真图」的前提）
- **P3** 情绪标签跨轮保持（约定 22；历史存原文才有意义）
- **P4** `thinking:disabled` 是否仍然「被接受但完全无效」（DEV_NOTES 已知状态）

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
