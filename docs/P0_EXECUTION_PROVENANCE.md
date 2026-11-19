# P0：执行溯源与实际配置固定

2026-11-18：网页研究链路已接入，受控测试已通过。用户反馈已按本文执行验收；尚未说明是否全部通过及具体覆盖项，暂不将真实 PostgreSQL、模型、浏览器和异常恢复各项统一标记为通过。

验收依据：用户反馈“已经按照 docs/P0_EXECUTION_PROVENANCE.md 进行了验收”。本次仅记录该反馈，没有重新执行真实调用或取得新的逐项运行记录；科研结论的人工语义质量复核继续单独记录。

## 已接入的链路

- 目标理解与规划：新根任务入队前，一次捕获规划、检索初筛、论文分析、语义评审、问题推荐及综述六个角色配置。
- 计划检索与摘要初筛：查询、来源映射、年份、数量和筛选规则随计划修订固定。失败续跑复用已完成查询和筛选，后续请求使用原模型与参数。检索属于实时检索加检查点，不宣称冻结了外部索引。
- 全文与分析执行：继承根任务角色清单，PDF 下载预算也固定。子任务在执行前保存父任务及计划步骤。复用历史分析时记录原产物及已知版本，不冒充本轮重新生成；未完成的已绑定分析按原批次继续。
- 辅助语义评审：逐条沿用原运行配置，缓存键包含实际版本编号。继续失败评审时明确携带原 review_id，关闭新任务开关或改变默认值也不会切换版本。原配置无法恢复时，可用“新建评审”显式采用当前配置并保留旧评审。保留 references-v2 和严格原文校验，不改变语义策略。
- 问题推荐与综述：所选分析版本固定；用户选择或修改问题后，综述关联推荐任务及实际确认的问题，并标记是否修改。计划内报告继承根任务清单。

`AgentVersion` 保存 Git SHA、dirty 状态、代码指纹、Python/依赖版本、实际 Prompt/Schema 与消息构造内容、协议及工具配置。`RuntimeSnapshot` 单独保存实际模型、提供方、地址、连接方式、超时、SDK 重试和输出参数。Codex 保存显式模型、CLI 版本/执行文件哈希及代理开关。内容按规范化 JSON 计算编号，只追加，不覆盖旧内容。

## 存储与关联

PostgreSQL 使用四张表：`agent_versions`、`runtime_snapshots`、`research_interactions`、`research_plan_revisions`。既有 SQLite 任务、计划、检查点和报告继续兼容。

交互记录保留原始研究目标和已知范围；任务记录包含 `interaction_id`、`root_task_id`、`parent_task_id`、`resumed_from_task_id`、计划编号/修订/快照、步骤和实际角色版本。分析和综述产物保留来源任务；综述 JSON 与 Markdown 导出保留执行来源编号；执行记录区分新产物与复用产物。未知 token/费用为 null，未支持的取消能力明确为 false。

计划修改会产生新的不可变内容快照。SQLite 只增加修订定位索引，并与当前计划在同一事务提交；正文保存在 PostgreSQL。若 PostgreSQL 已保存而 SQLite 提交失败，只会留下未被引用的内容，不会覆盖已提交修订或阻塞随后编辑。

正常业务处理不授予离线评测权限：新交互的 `evaluation_allowed=false`，本次未建设评测授权管理入口，也不把材料自动加入评测集。

历史任务缺少的配置或交互关联保持未知，不用当前默认值回填。旧计划在新开关下开始检索时，可以为本次新执行建立清单，但这不补造旧规划的配置。

## 启用

本轮没有更改本机 .env，也没有在真实数据库上执行建表。

1. 确认 Python 环境和 .env 中的数据库实例，显式执行：

   ```bash
   python -m storage.init_runtime_versions
   ```

   命令只创建缺失的四张 P0 表。已经建过前两张版本表，也需要再运行以补齐两张溯源表；不迁移或清空原任务。

2. 在 .env 中启用完整网页链路，然后重启后端：

   ```env
   RESEARCH_RUNTIME_PINNING=true
   ```

   原 `ANALYSIS_RUNTIME_PINNING` 继续保留。仅启用原开关仍只固定网页单篇分析；启用新开关会让研究链路和独立分析、评审、问题推荐、综述建立角色清单，无需再同时打开原开关。已绑定的任务不受关闭新任务开关影响，恢复时仍按原清单核对。

   Codex 模式须显式配置账号可用的 `CODEX_MODEL`，不能依赖 CLI 隐式默认模型。

3. 创建一个新研究计划，继续检索、选文、分析、评审和综述。任务查询应包含角色版本和交互编号，最终报告应包含相同交互编号、计划上下文和来源任务。

4. 可通过接口检查关联：

   ```text
   GET /api/v1/tasks/{task_id}/trace
   GET /api/v1/agent/plans/{plan_id}/revisions/{revision}
   ```

   前者返回交互、父子/续跑关系、计划修订、产物和已知耗时；未采集的历史返回 legacy_unknown。当前没有新增专门的溯源图页面。

5. 失败评审的前端“继续”会携带 `resume_review_id`；接口也可使用：

   ```text
   POST /api/v1/analyses/{analysis_id}/claim-reviews?resume_review_id={review_id}
   ```

   另建评审可使用同一入口的 `?new_run=true`，不与 resume_review_id 同时传入。新配置创建独立交互并保留分析来源。

   继续规划可在原规划 POST 请求中传 `resume_task_id`，研究目标必须一致；日期和配置沿用原任务。旧任务没有快照时不能伪造恢复。

`outputs/runtime_identity.key` 是本机随机身份校验密钥，权限 0600，已被 Git 忽略。API Key、数据库密码、代理密码不会写入快照。应妥善保留该文件和原执行环境；丢失校验密钥、轮换凭据、改变网络配置或无法取回原版本时，会明确停止并保留检查点。

## 受控检查与真实验收

```bash
python -m pytest -q test/test_research_runtime_provenance.py test/test_runtime_snapshots.py test/test_api_analysis_runtime.py test/test_analysis_runtime.py test/test_codex_model.py test/test_batch_analysis.py test/test_api_analyses.py test/test_research_planning.py test/test_research_search.py test/test_research_execution.py test/test_plan_reports.py test/test_api_claim_reviews.py test/test_api_reviews.py test/test_api_review_questions.py
cd web
npm run build
npm test
npm run test:questions
npm run test:claim-review
```

新闭环测试断言 MockTransport 实际收到的模型、地址、超时、思考模式和各角色输出预算，覆盖根任务默认切换、并发、检索/评审恢复、缓存、问题编辑、父子任务、历史修订及跨存储失败后的再次保存。来源映射与下载预算检查也验证实际工具收到原配置。默认开关在受控测试中显式隔离，不依赖本机 .env。

可选真实 PostgreSQL 验收：

```bash
python -m pytest -q test/test_runtime_versions_postgres.py
```

仅允许 TEST_DATABASE_URL 指向 researchpilot_test，并使用独立临时 schema。受控 SQLite、MockTransport 和组件交互通过，不等于真实 PostgreSQL、模型、检索网站和浏览器联调通过。

## 保留的边界

- 本次覆盖网页中的研究工作流。CLI、普通科研对话、单独手动检索接口、摘要翻译和连接诊断未接入完整交互清单；不是所有项目入口都已可回放。
- 代码只有内容清单，尚不自动打包/启动旧 artifact。代码、依赖、Prompt 或协议变化时拒绝恢复；文档也在严格代码清单中，修改后旧任务可能需要保留原 checkout 或明确新建任务。
- Codex 登录由 CLI 管理，只绑定登录目录和工具，尚不能证明目录内的账号身份从未切换。操作系统路由、远端模型升级、默认采样参数、随机性和实时索引仍不可完全复现，不承诺逐字输出一致。
- SQLite 与 PostgreSQL 之间不伪装分布式原子事务。版本先保存，再提交业务定位；失败时可能留下无引用的版本内容，不会自动删除，旧业务历史保留。
- 没有新增自动评测授权、自动晋级、Evolution 或取消调度系统。人工语义质量复核与真实环境验收分别记录，不能据代码测试或未明确结果的验收反馈宣布整个 P0 或科研质量已验收。

## 下一步建议

按 AGENTS.md 的 P1 路线，先接通 Analysis → Review：分析完成后基于该固定分析版本发起辅助评审，沿用根任务的评审配置并关联父子任务；分析与评审分别展示成功、失败和恢复状态，评审失败保留分析产物，恢复时只继续未完成评审。模型评审仍供人工核对，不自动删改分析结论或据此宣称研究问题已经充分回答。

后续实现更新：用户要求继续后，已将计划执行中的 Analysis → Review 接通为显式勾选选项，默认保持原范围，具体见 [P1 第一步](P1_ANALYSIS_REVIEW.md)。用户已反馈该步真实流程验收完成，与既有 P0 验收记录分开保存。后续授权的 select_library_papers 已接通实际执行，其真实验收单独记录，见 [P1 文献库执行](P1_LIBRARY_EXECUTION.md)。
