# 2026-11-24 服务检查与提交说明

本次补齐执行安全和可诊断性，依赖已有版本固定、证据校验与任务记录。用户授权检查现有程序、修复问题并提交GitHub；未授权生产发布，因此本轮不部署、不启动排程、不改生产Prompt或历史材料。

## 本次修复

- 评测管理路由原来只有模型调用确认字段。现在在 `/api/v1/evaluation` 统一核验真实连接地址、Host和浏览器Origin，仅允许localhost及回环地址，不依赖客户端伪造的转发头。非本机请求返回403/EVALUATION_LOCAL_ONLY；读取和执行同样受限。IPv4、IPv6、本机CLI和前端开发端口保持可用。
- 综述跨论文比较失败现在返回具体规则、section和从零开始的point_index。主题句只引用一篇、解释段落引用两篇时，仍按原规则拒绝发布，明确提示核对主题句引用；合法编号不等于结构满足要求。失败文件继续保留完整候选草稿，页面诊断不泄漏原文或本地路径。未修改分析、引用数量门槛或模型提示词。
- 标注功能对照表和目标文件中的历史进度，避免“前端未接后端”等旧说明被理解为当前状态。

## 已有变更一并纳入版本

计划产物验收、固定材料任务回放、数据策略实验、独立调度与回放渠道控制，以及模型选择宿主工具、Prompt/技能候选与共享预算的演化入口。本轮保持这些待提交功能的实现，不重排成熟模块或移植新Agent框架。包含代码和受控测试，不包含本地论文、密钥、数据库、人工标注、原始运行日志或outputs产物。

## 实际验证

|检查|实际结果|
|---|---|
|全量隔离后端（修复前基线）|1062通过，11项真实PostgreSQL相关测试跳过，5条已有PyMuPDF/Swig弃用警告|
|修复后相关后端回归|136通过，覆盖新访问边界、既有评测入口、候选/晋级、运行固定、分析综述引用及错误诊断|
|前端|TypeScript检查和构建通过；10项单元测试通过；15组渲染、数据及交互逻辑检查通过|
|敏感文件检查|Git可见候选文件无.env、PDF、数据库、运行目录；未匹配本机.env内的真实密钥，凭据URL格式命中已区分为占位示例及合成测试|
|空白错误检查|git diff --check通过|

全量命令：

```bash
RESEARCH_TEST_PYTHON=/path/to/python bash scripts/run_acceptance_checks.sh
```

修复后相关命令（隔离环境，不连接业务库）：

```bash
TEST_DATABASE_URL='' DATABASE_URL=sqlite:// \
LLM_PROVIDER=openai LLM_API_KEY=isolated-audit-key \
LLM_MODEL=isolated-audit-model LLM_BASE_URL=https://example.invalid/v1 \
ANALYSIS_RUNTIME_PINNING=false RESEARCH_RUNTIME_PINNING=false \
python -m pytest -q -p test.audit_isolation \
  test/test_evaluation_access.py test/test_evaluation_adversarial.py \
  test/test_evolution_gate_adversarial.py test/test_evolution_agent.py \
  test/test_runtime_snapshots.py test/test_api_reviews.py \
  test/test_review.py test/test_review_citations.py
```

## 未验证范围和兼容边界

- npm镜像和官方registry的audit查询均因当前执行环境DNS/网络限制失败，未取得依赖安全公告结果，不宣称依赖无已知漏洞。
- 本轮不重跑真实PostgreSQL、模型、浏览器手工验收；前端交互检查为受控脚本。既有真实运行记录不被模拟结果替代。
- 新访问限制只覆盖评测管理路由，不替代全站用户认证。其他业务继续按本机单用户模式使用，应绑定127.0.0.1；经远程或共享代理开放前仍须独立认证、授权及访问控制。
- 综述原有结构规则继续生效；模型仍可能产生被拒绝的草稿。已发现的“主题句/解释段落引用粒度”失败保留，不通过强挂引用掩盖，也不据此认定科研语义错误。
- 目前P3/P4真实回放主要为固定材料问答，P5只作用于新回放任务；报告独立语义评审、原协议综述回放和整套科研流程自动进化仍未完成。当前工作树不作为这些阶段全部通过的声明。
- 代码升级后的旧任务恢复须匹配原版本；原artifact不可用时明确停止，新实验另建版本，不重置旧预算。

结论：本次自动检查及相关代码复核范围内未留下已复现的阻断问题；不作“绝无漏洞”或“全部真实科研质量验收通过”的保证。检查原始输出保存在本机outputs/acceptance，不提交GitHub。
