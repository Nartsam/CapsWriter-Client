# 开发笔记

> 跨会话的待办、重要决策与已知怪状态，供后续开发（人或 AI agent）快速接续上下文。
> 每条一两行并署日期；架构与模块说明请写 PROJECT_INDEX.md，不要写在这里。

## 待验证

- [ ] OpenMate 接入尚未实机验收：启动 OpenMate 后说「助理 xxx」验证转交展示、情绪立绘切换、托盘自动唤出（OpenMate 文档自注"无激活恢复显示仍需 Windows 实机验收"）。（2026-07-16）

## 已知状态

- Companion WebSocket 服务器（端口 6019）目前没有已知的对端应用：它在提交 38ea3a4（2026-06-14）随一批混杂改动引入，设计对端是一个 Electron 伴侣应用，但该应用不在本仓库、也未部署。注意 Companion 连接存在时优先级高于本地 LLM/OpenMate（角色文本直接转发、跳过本地处理）。OpenMate 走 HTTP 18760，与其无关。（2026-07-16）
- 本 Client Release 含 `core/server/` 源码但不含 `config_server.py` 与 `start_server.exe`，服务端配置不在本目录修改。（2026-07-16）

## 决策记录

- OpenMate 情绪标签使用稳定状态 ID，别名仅作提示词中的语义注解：ID 是 ASCII 稳定标识，规避 NFC 规范化和别名集合变动问题，OpenMate API 文档 §4.3 亦如此建议。（2026-07-16）
- OpenMate 接入采用无状态按需探测（每次 toast 请求前 GET /health，0.5s 超时），不维护长连接：OpenMate 可随时启停，无需重连逻辑；情绪状态列表缓存 5 分钟（失败缓存 1 分钟）。（2026-07-16）
- 不对 LLM 输出的情绪标签做本地校验：OpenMate 对无效状态回退默认立绘并剥离标签，本地增加跨分片流式校验层复杂度高且无用户可见收益。（2026-07-16）
- 本地定制的变更记录写本文件，不写 docs/CHANGELOG.md：后者是上游随版本发布的文件，混入本地条目会在升级覆盖时冲突。（2026-07-16）

## 本地定制变更（相对上游 Release）

- 2026-07-16：新增 OpenMate 桌面看板接入（`core/client/llm/llm_output_openmate.py`、`config_client.py` 的 `openmate_*` 配置），toast 角色输出可转交 OpenMate 展示并驱动情绪立绘。
- 2026-07-14 前后：LLM 弹窗与录音指示器字体修复（跨 Tk 解释器命名字体失效问题，见 PROJECT_INDEX 约定 9）、弹窗字号调整、删除「小助理」角色。
- 2026-06-14：引入 Companion WebSocket 服务器（见上文"已知状态"）。
