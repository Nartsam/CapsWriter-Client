# 开发笔记

> 跨会话的待办、重要决策与已知怪状态，供后续开发（人或 AI agent）快速接续上下文。
> 每条一两行并署日期；架构与模块说明请写 PROJECT_INDEX.md，不要写在这里。

## 待验证

- [ ] OpenMate 接入尚未实机验收：启动 OpenMate 后说「助理 xxx」验证转交展示、情绪立绘切换、托盘自动唤出（OpenMate 文档自注"无激活恢复显示仍需 Windows 实机验收"）。（2026-07-16）
- [ ] 回复分段实机验收：启动 OpenMate 后对「新岛真」或「雾切响子」说话，验证多行回复逐条展示、第一条不等全文、ESC 停止剩余段、OpenMate 不在时回退本地完整弹窗。（2026-07-17）
- [ ] 长录音悬浮提示实机验收：连续录音超过 68s，确认提示框始终显示「正在录音 xx.xs」而不再跳成卡住的「识别进行中 已处理 60.0s」；松开后仍能正常进入等待/处理状态。（2026-07-26）
- [ ] typing 指示实机验收：确认接管后本地「正在处理结果」悬浮窗消失、OpenMate 显示打字动画、首条内容到达自动熄灭；LLM 报错时指示被显式关闭；普通打字输入（非 toast 角色）本地提示照常。（2026-07-17）

## 待办

- [x] 「正在处理结果」状态移交 OpenMate：OpenMate 侧 `PUT /v1/presentation/typing`（需求文档 `docs/OpenMate-正在输入指示改造需求.md`）与客户端接线均已完成（2026-07-17），待实机验收（见「待验证」）。
- [x] LLM 角色回复分段功能：OpenMate 侧「可追加分段队列」（需求文档 `docs/OpenMate-可追加分段队列改造需求.md`）与客户端 `OpenMateSegmentSender` 均已完成（2026-07-17），待实机验收（见「待验证」）。

## 已知状态

- ~~OpenMate 情绪立绘不切换~~（2026-07-16 验收发现，2026-07-17 已修复）：曾因 `openmate_character` 留空时按角色列表第一个取状态，注入了非当前角色（雾切响子）的状态 ID，标签全部失配回退默认立绘。OpenMate 侧新增 `GET /v1/presentation` 后，客户端改为留空时每次请求解析当前生效角色再取其状态，并把流固定到该角色，待实机复验。

- Companion WebSocket 服务器（端口 6019）目前没有已知的对端应用：它在提交 38ea3a4（2026-06-14）随一批混杂改动引入，设计对端是一个 Electron 伴侣应用，但该应用不在本仓库、也未部署。注意 Companion 连接存在时优先级高于本地 LLM/OpenMate（角色文本直接转发、跳过本地处理）。OpenMate 走 HTTP 18760，与其无关。（2026-07-16）
- 本 Client Release 含 `core/server/` 源码但不含 `config_server.py` 与 `start_server.exe`，服务端配置不在本目录修改。（2026-07-16）

## 决策记录

- OpenMate 情绪标签使用稳定状态 ID，别名仅作提示词中的语义注解：ID 是 ASCII 稳定标识，规避 NFC 规范化和别名集合变动问题，OpenMate API 文档 §4.3 亦如此建议。（2026-07-16）
- OpenMate 接入采用无状态按需探测（每次 toast 请求前 GET /health，0.5s 超时），不维护长连接：OpenMate 可随时启停，无需重连逻辑；情绪状态列表缓存 5 分钟（失败缓存 1 分钟）。（2026-07-16）
- 不对 LLM 输出的情绪标签做本地校验：OpenMate 对无效状态回退默认立绘并剥离标签，本地增加跨分片流式校验层复杂度高且无用户可见收益。（2026-07-16）
- 本地定制的变更记录写本文件，不写 docs/CHANGELOG.md：后者是上游随版本发布的文件，混入本地条目会在升级覆盖时冲突。（2026-07-16）

## 本地定制变更（相对上游 Release）

- 2026-07-26：修复长录音（>68s）时悬浮提示被服务端分段进度顶掉的问题：`progress` 命令在 `mode == "recording"` 时只记录已处理时长/预览文本，不再切换显示模式。原因是麦克风边录边发，服务端每 `mic_seg_duration + mic_seg_overlap * 2` 才回传一次进度，中间一分钟数字不变，观感像卡死。
- 2026-07-17：「正在处理结果」状态移交：OpenMate 接管确认后隐藏本地悬浮提示，改发 `PUT /v1/presentation/typing` 显示打字动画（首条内容自动熄灭；未交付/失败路径显式关闭；typing 开启失败则保留本地提示，兼容旧版 OpenMate）。
- 2026-07-17：新增 LLM 角色回复分段（`enable_result_splitter`/`result_split_delimiter`，RoleConfig 角色级字段）：OpenMate 路径下经 open 分段队列（`POST /v1/messages` open + `PATCH` 追加/封口）把回复切成多条消息逐条展示；新增「雾切响子」「新岛真」两个人设角色并开启分段（按 `\n` 切分）。本地历史/日志仍存全文；未连接 OpenMate 时回退完整显示。
- 2026-07-17：OpenMate 接入改用新增的 `GET /v1/presentation` 端点：`openmate_character` 留空时每次请求解析当前生效角色（解析不缓存，仅失败结果短缓存），按该角色取情绪状态并把流固定到它；注入情绪指令时增加 INFO 日志（角色 ID + 状态数）。移除按角色列表第一个取状态的旧回退。
- 2026-07-16：新增 OpenMate 桌面看板接入（`core/client/llm/llm_output_openmate.py`、`config_client.py` 的 `openmate_*` 配置），toast 角色输出可转交 OpenMate 展示并驱动情绪立绘。
- 2026-07-14 前后：LLM 弹窗与录音指示器字体修复（跨 Tk 解释器命名字体失效问题，见 PROJECT_INDEX 约定 9）、弹窗字号调整、删除「小助理」角色。
- 2026-06-14：引入 Companion WebSocket 服务器（见上文"已知状态"）。
