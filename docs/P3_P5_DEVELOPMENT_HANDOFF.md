# P3–P5 连续开发交接与统一测试清单

2026-11-23补充：已新增 [模型驱动的Prompt演化](AUTONOMOUS_PROMPT_EVOLUTION.md)，LLM可通过真实工具循环读取反馈、选择技能、提出通用指令和执行受预算限制的对照；不再仅能切换三个固定profile。下文为此前首版交接背景，任意候选代码、科研全流程自动回放及自动生产晋级仍未实现。新入口真实模型/PG运行单独验收。

后续更新：用户已明确要求fake漏洞检查，相关72项模拟回归及前端替身/类型检查通过并修复问题，见 [Fake检查记录](P3_P5_FAKE_TEST_AUDIT.md)。下文“本轮未运行”指此前连续开发阶段；不把这些专项结果扩展为全部验收通过。

用户本轮要求：先连续开发，最后统一测试。因此本轮只改代码/文档和编写待运行检查，**没有运行测试、编译、前端构建、真实模型调用、数据库建表、排程启用、发布、回滚或Git提交/推送**。下述为实现范围，不能写成验收通过或“全部科研能力已完成”。P2真实验收也没有凭开发自动补齐。

## 本轮实现范围

|阶段|入口与能力|默认状态及限制|
|---|---|---|
|P3|显式材料导出、授权清单、实际版本登记、固定材料回放、重复执行、人工判定归档、逐例比较|真实模型执行需要明确确认；不自动导入论文、不发人工要求给模型；尚未统一测试|
|P4|失败类型归因、数据策略候选、固定晋级规则、受预算限制的多轮开发实验、独立材料核验|仅白名单数据策略；不运行任意候选代码；缺独立自然材料和人工判定则维持基线|
|P5|独立排程、PG状态/租约/防重入、预算、回放渠道灰度、手动发布和回滚历史|新排程默认禁用，不挂在FastAPI worker；生产操作只保留显式CLI，不实际启用|

P3当前真实回放执行的是**固定材料问答任务**，不是全套规划/实时检索/下载/分析/综述的自动重演。仍保留离线评分中的 frozen/live_search 区分，但新回放器只接 frozen；实时检索策略的召回评测需要后续专门语料和适配器。P4当前演化范围仅是该问答任务的回答顺序/预算数据策略，不能泛化为任意科研Agent自我改写。P5发布渠道也只作用于新建**回放任务**，不替换现有科研主流程的根配置，不宣称已实现整个平台灰度发布。以上是当前代码的明确边界。

## P3：材料与实际执行记录

- `evaluation/task_export.py` 从明确选择的有效分析读取已引用片段，核对原论文/文档/引句，保留分析、文档指纹和导出范围。只导出 cited chunks，不冒充全文。Permission需明确允许评测，人工要求由操作者提供。
- `evaluation/task_replay.py` 复用原句切分与P0版本工厂。模型只选材料/原句编号，后端恢复连续原文，不接收模型改写的引用。输入超预算拒绝，不截断。实际 Prompt、Schema、材料输入、模型、凭据引用、代码及依赖绑定PG版本记录；实际请求开始前/返回后复核。
- `evaluation/replay_jobs.py` 每个案例/重复轮次/尝试都有检查点。调用前预扣，失败、修复和未知是否发出的尝试不退预算。默认每条尝试1次、无SDK额外重试；Codex输出上限由CLI管理，不能把策略字段中的Token数声称为Codex硬上限；恢复不重置预算或期限。每次实际请求的超时取原配置与剩余期限的小值，导出RuntimeSnapshot留存。迟到响应不发布；网络取消不承诺立即完成。
- 根任务/候选作业的人工要求只供离线评分，不送模型。结果默认没有人工标签，不能因为运行成功或引句真实就算回答正确。失败与缺失进入固定分母。人工归档另存版本，保留原运行、改标前记录及独立/受模型影响背景。
- 真实回放元数据在P0仓库核对，长期输入/结果复用PG `research_coverage_artifacts`；租约/预算/尝试检查点用单独SQLite文件。材料撤回记录按数据集产物ID拒绝未来调用；离线旧文件无法全局撤回，外部权限管理仍须配合。

新增管理CLI（代码完成后再在授权测试环境使用；本轮没有执行）：

```text
python manage_research_evaluation.py export export_spec.json
python manage_research_evaluation.py register-dataset dataset.json
python manage_research_evaluation.py register-binding --profile standard --timeout 120 --tokens 4000
python manage_research_evaluation.py prepare replay_job_spec.json
python manage_research_evaluation.py execute JOB_ID --confirm-model-calls
python manage_research_evaluation.py read RESULT_ID --output new_result.json
python manage_research_evaluation.py annotate RESULT_ID judgments.json
python manage_research_evaluation.py withdraw DATASET_ID --actor NAME --reason REASON
```

导出说明：`id/revision/annotation_version/cases[]`，每例用 `ExportCase` 字段，包含原分析ID、问题、范围、分组、集合、授权、人工要求。授权人只是操作者自述，程序不认证身份。

作业说明示例（ID替换为实际登记返回值）：

```json
{"dataset_artifact_id":"experiment_...","binding_artifact_id":"experiment_...","split":"development","repetitions":2,"max_calls":20,"total_seconds":1800,"max_attempts":1,"holdout_authorized":false}
```

人工归档说明：`run_fingerprint`对应原结果的 `run` 内容指纹，`items[]` 含 `case_id/repetition/judgments[]`。judgments使用原有requirement_id、verdict、assessor、reason、context、previous_verdict契约。不得把模型判断粘为独立人工答案。

网页新增“研究评测”高级入口：可登记获准材料/固定配置、准备作业、明确确认执行、查询产物、比较结果；输入JSON不接收密钥。页面不是完整评测集编辑器或实验发布平台，P4/P5管理仍使用CLI。

## P4：受控数据策略实验

候选必须写明假设、理由、作者、基线绑定和Strategy。profile只支持 standard/conditions_first/contradictions_first，另可改变输入/输出预算上限；不能修改程序硬规则、引用协议、权限、评测集合或晋级规则，不执行任意Python/shell补丁。这是固定实现内的数据隔离，不声称操作系统级沙箱。

```text
python manage_research_evolution.py candidate candidate.json
python manage_research_evolution.py freeze-policy gate_policy.json
python manage_research_evolution.py diagnose DEVELOPMENT_RESULT_ID
python manage_research_evolution.py prepare-experiment experiment.json
python manage_research_evolution.py execute EXPERIMENT_ID --confirm-model-calls
python manage_research_evolution.py compare EXPERIMENT_ID BASELINE_ANNOTATED_RESULT_ID CANDIDATE_ANNOTATED_RESULT_ID
```

实验说明字段为 baseline_binding_id/candidate_ids/policy_id/max_rounds/dataset_artifact_id/max_calls/total_seconds/author。开发包必须只含development，不含回归或留出要求。最多5个候选、3轮，父实验与子任务共同预扣共享调用预算，中断/恢复不归零。候选沿用基线模型/凭据/网络，通过新版本绑定实际策略，不能执行中读取默认模型替代。

规则先登记再准备实验。来源错误、伪造/权限关键错误直接拒绝；运行可靠性、独立人工回答程度采用预先固定非劣容差，并要求实际收益。重要改进需要独立自然留出材料、至少两个独立材料组、完整独立人工判定；开发同源材料不算独立验证。缺判断、收益不足、合成案例或规则不匹配均维持基线。身份独立性由人工声明，不认证，也不自动改变生产。

## P5：独立调度与回放渠道

新增长期PG表 `evaluation_schedules`、`replay_release_channels`。仅显式 `init-tables` 创建缺失表，不随API启动。现有任务/检查点/产物无需迁移或清库。若恢复旧代码，停止独立worker，保留新表和历史；不执行删除表的不可逆回退。

```text
python manage_research_operations.py init-tables
python manage_research_operations.py schedule disabled_schedule.json
python manage_research_operations.py enable SCHEDULE_ID --actor NAME
python manage_research_operations.py worker-once --worker-id WORKER --confirm-model-calls
python manage_research_operations.py disable SCHEDULE_ID --actor NAME
python manage_research_operations.py init-channel CHANNEL BASELINE_BINDING --actor NAME
python manage_research_operations.py publish CHANNEL DECISION_ID --revision N --actor NAME --canary-percent 10 --authorize-release
python manage_research_operations.py rollback CHANNEL --revision N --actor NAME --authorize-release
python manage_research_operations.py route CHANNEL NEW_TASK_ID
```

排程需有固定job_id、间隔、最多轮数、总调用上限、过期时间、操作者，创建时enabled=false。独立外部定时器可调用worker-once，API worker不启动重复调度；PG行锁、租约token及到期检查防止并发重复归档。每轮按子任务最大调用保守预扣，未知中断用量不返还。每轮有单独检查点身份；原轮不受默认配置变化影响。留出任务禁止定时执行。发布前必须有可复查的允许手动发布决策；灰度按稳定的新任务ID分桶，渠道修订通过比较更新，回滚确认兼容基线仍可恢复。

新作业可通过 `manage_research_evaluation.py prepare spec.json --channel CHANNEL --new-task-id STABLE_ID` 固定本次路由结果；已经准备的任务不再跟随渠道切换。渠道只管理回放组件，未挂接在线科研配置。发布/回滚CLI即为明确管理动作；本轮不执行这些生产变更。

## 最后统一测试清单（本轮未运行）

先检查隔离数据库、原文权限与产物路径，然后统一执行：

```bash
TEST_DATABASE_URL='' DATABASE_URL=sqlite:// \
LLM_PROVIDER=openai LLM_API_KEY=isolated-audit-key \
LLM_MODEL=isolated-audit-model LLM_BASE_URL=https://example.invalid/v1 \
python -m pytest -q test -p test.audit_isolation --tb=short
cd web
npm run build
npm test
npm run test:render
npm run test:data
npm run test:planning
npm run test:claim-review
npm run test:execution-acceptance
npm run test:execution-review-links
npm run test:evaluation-workspace
```

新增待运行契约检查在 `test/test_research_deferred_workflows.py` 和 `test/test_research_operations_deferred.py`。还需真实隔离PG验证：不可变产物并发/事务、排程租约竞争、租约过期、worker崩溃、中断后预算、发布并发修订、候选退步、独立验证拒绝、灰度与回滚、材料撤回；真实模型验证：版本切换、重复回放、超时、引用枚举/归属、输出截断、无答案泄漏、成本未知；浏览器验证：高级入口输入校验、明确调用确认、任务失败/恢复及产物读取。需要独立于模型的人工语义标注，不能凭受控替身宣告通过。

所有新增代码当前状态：**已编写、未统一测试**。现有P2和P3首步此前的测试结果仍保留，但不扩展到本轮新增模块。可能的缺陷应在统一测试中修复后再发布。没有提交/推送或部署。

### 开发清单状态

已编写：P3显式导出/登记、固定材料真实回放/预算恢复/人工归档、评测工作区；P4数据策略假设/候选/共享预算实验/固定规则/独立验证拒绝；P5独立排程/租约/保守预算/回放组件渠道/灰度/回滚。

尚未覆盖：科研全流程实时检索自动重演、任意代码补丁的操作系统隔离实验、整个在线科研主流程的生产灰度部署，以及获授权的真实代表性评测集与人工标准。这些不能由首版数据策略实验冒充完成，后续范围须明确。所有新代码尚未统一测试；本轮不新增任何测试通过声明。
