# 2026-11-19 服务与提交前检查

本次检查覆盖当前 P0–P2 实现，不新增自主执行范围。程序和受控流程检查通过不等于真实模型连通、真实 PostgreSQL 联调或语义判断已验收，也不能保证所有运行条件下绝无缺陷。

## 发现并修复

1. 从已关联评审的执行继续补读或加入补充论文时，新分支曾继承旧 `review_selection_id`，导致旧评审选择与新分析集合冲突。现清除失效选择标识，并明确补读分支类型；原执行和评审历史保留，新分析不冒用旧评审。
2. 已保存综述此前只按原文/分析判断当前状态。现生成前、发布前和读取时再次核对固定评审关联，评审变化时拒绝发布新结果，已保存综述标为历史；Markdown/JSON 等历史导出继续可读，不覆盖成功历史。

新增回归覆盖“评审分支→补读→覆盖”“评审分支→补检索→补充阅读→覆盖”及“综述发布后评审内容变化”。没有降低引用断言、改写模型标签或隐藏失败项。

## 实际检查结果

- 全量后端：**965 passed、11 skipped**，耗时约322秒。11项真实 PostgreSQL 专项因本次未连接测试库而跳过，未计为通过；另有5条 PyMuPDF 依赖弃用警告。
- 修复专项：26项通过，包含上述跨阶段恢复和历史综述导出。
- Python 源码编译检查及 `git diff --check` 通过。
- 前端：构建、10项单测，以及 render、data、questions、planning、citations、claim-review、execution-review、library-execution、coverage、reread、supplement-search、supplement-readings、execution-review-links 全部检查通过。修复后重验构建及受影响页面/交互。
- 隔离 ASGI 启动成功，OpenAPI 生成56条路径；ping、health、任务查询、来源目录查询均200；缺失计划404、非法评审选择422。使用临时 SQLite 与示例模型配置，未调用真实模型或修改真实任务。
- 提交前对仓库已跟踪/未忽略内容检查：未发现当前 `.env` 真实密钥或完整数据库连接串；未纳入本地 PDF、Word、任务数据库及原始运行日志。`.env` 和运行产物继续忽略。

## 复验命令与隔离

前端在 `web/` 中执行 `npm run build`、`npm test` 及 `package.json` 中各 `test:*` 脚本。

全量后端本次使用临时 pytest 插件，把任务、上传目录、评审检查点、诊断和身份密钥路径全部指向每项测试的临时目录。仓库提供等价可选插件 `test.audit_isolation`，不改变普通启动或默认测试行为。可在项目目录执行：

```bash
TEST_DATABASE_URL='' DATABASE_URL=sqlite:// \
LLM_PROVIDER=openai LLM_API_KEY=isolated-audit-key \
LLM_MODEL=isolated-audit-model LLM_BASE_URL=https://example.invalid/v1 \
python -m pytest -q test -p test.audit_isolation --tb=short
```

这里的 Key 和地址均为无效示例；测试使用传输替身。不要把该命令中的配置改为生产库或真实密钥来进行受控回归。真实 PostgreSQL 专项另按 `TESTING.md` 使用受保护的 `researchpilot_test` 临时 schema 验收。

运行快照测试期间不要编辑源文件/文档、更新依赖或并行构建；程序会拒绝在途代码改变。代码更新后，历史任务只能使用兼容原 artifact 续跑，或明确建立新版本；不得用当前默认值伪造旧配置。检查点不删除。

## 尚未验证

当前工具环境对本地8001、5173端口的套接字探测返回 `PermissionError`；真实数据库只读连接探测未成功。**不能据此判断用户实际服务停机，也不能据隔离 health 成功宣称真实服务可用。**

本次未执行生产迁移、真实模型调用或真实浏览器完整研究流程，未宣称人工语义复核完成。请在本机启动实际服务后，按 P0–P2 各验收文档核对真实 PostgreSQL 表、模型连接、PDF 阅读与导出。
