# P3–P5 Fake 测试与漏洞修复记录

用户在连续开发后明确要求编写fake用例并检查漏洞。本轮因此执行模拟回归，覆盖此前标为“待统一测试”的部分模块；不把此前暂缓测试的要求误当作禁止本次测试。未调用真实模型、未写生产库、未建生产表、未启用排程或发布、未提交/推送。

## 复现问题与修复

|问题|模拟复现|修复行为|
|---|---|---|
|进程崩溃后单例尝试次数可增加|模型已收到调用但抛出SystemExit；max_attempts=1的续跑仍调用第二次|全局预算与pending尝试在同一SQLite事务预登记；未知结果保留interrupted，续跑计入尝试上限；调用前固定实际RuntimeSnapshot，未知token/费用不编成0|
|人工归档可夹带改写的运行输出|原运行指纹正确，但标注副本替换answer后仍可被读取评分|标注副本仅允许改变human_judgments；回答、状态、引用、用量和版本必须与原运行一致|
|结果可以声明不属于原作业的运行版本|替换结果runtime_snapshot_id后读取未拒绝|从作业、固定材料及P0注册绑定重建预期元数据，核对结果内容；同时验证集合/轮次/输入指纹|
|通过的决策可发布到无关基线渠道|A基线的候选决策，应用到B基线渠道仍增加渠道修订|发布前再次核验决策，检查实验基线与渠道基线一致、复核候选与原候选一致，再按渠道修订更新|
|实验子任务可被独立入口入队|子任务有自己的预算，但没有绑定父预算；独立执行可能绕过共享约束|子任务持久记录parent_experiment_id；独立API在入队前拒绝，底层runner拒绝无父预算入口，独立排程也拒绝子任务|
|调用确认字段接受非布尔输入|字符串"true"被转换为True并触发后台调用|执行请求使用严格类型，只接受布尔true；false、字符串和数字均拒绝执行|
|期限复核与错误类型不明确|运行中期限变化后仍按旧值发布成功；预算耗尽写成通用RUNTIME_SNAPSHOT_UNAVAILABLE|发请求前和返回后核对持久期限及父期限；迟到结果不发布；预算/期限使用EXPERIMENT_BUDGET_EXHAUSTED、EXPERIMENT_DEADLINE_EXCEEDED独立错误码|
|租约到期边界可继续写|时间恰好等于lease_until仍被视为有效|到期使用<=判定，旧执行者不能在边界预扣或发布|

这里的“篡改归档”测试使用有写入能力的模拟提供方验证完整性边界，不宣称外部未授权用户已能读写生产数据库。首轮还修正了测试自身的隔离建表时机与超时预期，不将这些fixture问题计为产品漏洞。

## 测试组成

- `test/test_evaluation_adversarial.py`：使用临时SQLite业务表/P0注册表与模型替身，验证崩溃、标注/版本篡改、跨基线发布、父子预算、原文编号、材料撤回、不可覆盖结果、严格API确认、超期响应、请求前期限、准确错误码、实际固定模型/超时/SDK重试、成功缓存和答案隔离。
- `test/test_evolution_gate_adversarial.py`：fake运行与人工声明验证“有收益且满足条件才给人工发布建议”，来源错误、模型影响改标、同源留出和规则替换均拒绝；不实际发布。
- 现有边界检查补测租约边界、独立排程拒绝实验子任务。
- 前端 `test:evaluation-workspace` 使用API替身验证打开页面无模型调用、执行前确认、结果手动读取及人工语义状态；`typecheck`通过。

## 实际结果与命令

修复后最终相关后端 **72 passed**，耗时约12秒；有5条现有PyMuPDF弃用警告，没有测试失败。

```bash
TEST_DATABASE_URL='' DATABASE_URL=sqlite:// \
LLM_PROVIDER=openai LLM_API_KEY=isolated-audit-key \
LLM_MODEL=isolated-audit-model LLM_BASE_URL=https://example.invalid/v1 \
python -m pytest -q -p test.audit_isolation \
  test/test_evaluation_adversarial.py test/test_evolution_gate_adversarial.py \
  test/test_research_deferred_workflows.py test/test_research_operations_deferred.py \
  test/test_task_quality.py test/test_runtime_snapshots.py --tb=short

cd web
npm run test:evaluation-workspace
npm run typecheck
```

均为受控测试。实际模型请求由替身接收，数据在临时库/目录中；当前`.env`密钥未用于真实调用，生产排程/渠道不变。

## 尚未由本轮证明

- SQLite替身不能证明真实PostgreSQL的SKIP LOCKED、行锁和并发发布无问题，须使用独立测试库进一步验收。
- 未进行真实网络、真实模型、完整浏览器和生产发布演练；不据fake语义标签宣称科研回答正确。
- 没有执行整个仓库全量测试和所有P0–P5真实流程，只报告本轮相关72项；没有保证“已无所有漏洞”。
- 首版仍然是固定材料问答回放、数据策略候选和回放渠道管理，不因此变成科研全流程自动回放或任意补丁的OS沙箱。
