# CLAUDE.md

## 安全规则

- **禁止读取 `LLM/secrets.py`**：该文件存放 API 密钥，任何 AI agent 不得读取、引用或输出其中的内容。

## 开发约束

- 本目录是打包后的 Release 目录：系统 Python 没有项目依赖，不要直接 `import core` 跑测试（会因缺少 rich 等失败）。验证方式见 PROJECT_INDEX.md 的「开发环境与验证」一节。
- 禁止将 `__pycache__`/`.pyc` 提交进仓库；语法检查用 `ast.parse` 而非 `py_compile`。
- 修改行为后必须同步更新 PROJECT_INDEX.md 相关章节；跨会话的待办、决策与已知怪状态记录在 `docs/DEV_NOTES.md`。
