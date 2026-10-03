# CLAUDE.md

本仓库是 CapsWriter 离线语音输入的**客户端**（Windows）；配套服务端在 CapsWriter-Server 中（另一个仓库，不在修改范围内）。

## 安全规则

- **禁止读取 `LLM/secrets.py`**：该文件存放 API 密钥，任何 AI agent 不得读取、引用或输出其中的内容。搜索时也要显式排除它（Grep 用 `glob: "!LLM/secrets.py"`，grep 命令用 `--exclude=secrets.py`）。

## 阅读顺序

1. [PROJECT_INDEX.md](PROJECT_INDEX.md) — 项目地图：架构、模块位置、跨模块约定、协议、验证方法、常见修改指南
2. [docs/DEV_NOTES.md](docs/DEV_NOTES.md) — 当前状态：待验证、已知问题、仍然有效的决策与待办
3. 按需：[tools/README.md](tools/README.md)（验证脚本）、`docs/` 下的功能使用说明（[readme.md](readme.md) 有清单）
4. 改某个文件之前，先读它的文件头和相关函数的注释：只和那段代码有关的坑写在那里

## 开发约束

- 本目录是打包后的 Release 目录：系统 Python 没有项目依赖，不要直接 `import core` 跑测试（会因缺少 rich 等失败）。验证方式见 PROJECT_INDEX.md 的「开发环境与验证」一节。
- 运行时是 `internal/` 里的 **Python 3.8**：不能用 3.9+ 的语法和标准库（如 `match`、`str.removeprefix`；注解里写 `list[int]` 要先 `from __future__ import annotations`）。只能用 `internal/` 里已有的第三方库，新依赖要先确认它在 `internal/` 里——导入失败常被 `try/except` 吞掉，功能会悄悄失效（`window_detector` 曾因此取不到进程名）。
- 禁止将 `__pycache__`/`.pyc` 提交进仓库；语法检查用 `ast.parse` 而非 `py_compile`。
- 修改行为后必须同步更新 PROJECT_INDEX.md 相关章节及这件事的其他归属位置（见下面的「文档维护」）；跨会话的待办、决策与已知问题记录在 `docs/DEV_NOTES.md`。

## 文档维护

**每个事实只写在一个地方**，其他地方需要时写一句话并链接过去：

| 内容 | 归属 |
|---|---|
| 给人看的操作步骤 | [readme.md](readme.md) + `docs/` 下的功能使用说明（「XX功能如何使用.md」式命名） |
| 架构、模块位置、跨模块约束、修改指南 | [PROJECT_INDEX.md](PROJECT_INDEX.md) |
| 只和某段代码有关的坑与原因 | 那段代码的注释 / docstring |
| 待验证、已知问题、仍然有效的决策与待办 | [docs/DEV_NOTES.md](docs/DEV_NOTES.md)，完成或失效就删除 |
| 做了什么、怎么验证的（历史） | git 提交说明 |

- 改了行为，就更新这件事的归属位置，再搜一下其他提到它的地方是否仍然成立。
- PROJECT_INDEX.md 不逐步复述代码，也不写日期和测试记录；某一节超过一屏，就拆到 `docs/` 下单独成文。
- 服务端的话题（模型、显卡加速、部署）写在服务端仓库，这里只链接过去。
