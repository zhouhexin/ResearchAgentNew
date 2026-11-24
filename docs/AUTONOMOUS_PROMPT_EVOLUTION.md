# HarnessDev 方法适配：模型驱动的 Prompt 演化

本次补齐“自主研究 → 受控演化”的工具循环，依赖已有执行版本、固定材料、证据校验、实验产物与持久预算。不是从弱种子重写项目，也不把论文中的基准环境命令直接移植成生产权限。

参考用户已提供的 HarnessDev PDF：附录 E.1/E.2，PDF 第26–31页，特别是第29–31页的反馈、工具、预算、真实产物及诊断后修改合同。提示词使用本项目的中文改写；保留原有 CLI、PostgreSQL、任务与引用协议。论文允许任意代码编辑的实验环境不等于本项目已具备任意候选代码的操作系统隔离。

本机阅读版本PDF SHA-256：`1231454f0ae5ccffdb3d031dc8f42eae0cbbaeb61dcd25dddb1855074697dfc0`。未把论文中的基准名称、配额、无限本地试验或自由发布规则复制为本项目权限。

## 本轮实际执行路径

`run_evolution_agent.py` → 固定开发材料与运行配置 → 基线回答及独立调用辅助评审 → 控制器读取反馈/材料/技能 → 提出新 Prompt 候选 → 候选回答及独立调用辅助评审 → 比较/继续或停止 → 保存不可变结果与轨迹。

三种模型职责使用独立指令、请求和版本内容，首版沿用同一固定提供方/模型配置。控制器选择工具，普通程序执行；同模型独立调用不等于独立事实证明。

|工具|实际能力|
|---|---|
|capabilities|读取真实工具和技能目录|
|read_case / read_material|读取获准开发案例及固定原文编号，无任意文件路径|
|read_skill|读取版本化技能；候选使用技能前必须实际读取|
|read_feedback|默认完整统计和逐题状态；指定case_id展开回答与评审，不提供人工标签/要求|
|propose_candidate|登记通用指令、技能和策略；另存候选版本|
|run_probe|真实执行本轮候选和独立辅助评审，共享预算|
|compare_probe|固定分母比较，区分来源、完成和辅助语义判断|
|finish|保存停止依据；不发布生产版本|

内置技能：证据溯源、实验条件、数值比较、冲突证据。技能是版本化指令，不是自动安装的外部插件；内容真的进入候选请求并纳入执行合同。已有论文检索、解析和身份核验工具继续保留，本轮冻结材料实验无需增加检索网站、插件或新的Agent框架。

候选不再限于三个固定 profile：可由LLM提出通用 `instructions` 和选择 `skills`，与原有 profile/输入输出预算一起保存到Strategy。禁止在候选指令里写具体数值、URL、代码、案例/材料身份或复制长原文；这不能证明不存在所有语义过拟合，因此仍需要独立材料验证。不存在模型可调用的任意shell、文件读写、评分器修改或生产发布工具。

本自主演化入口要求候选沿用基线输入/输出上限，不允许模型靠扩大上下文或生成预算制造收益。其他原有显式人工实验仍按其冻结Strategy执行。

## 本机运行

使用已有四题的固定材料；不修改旧轮结果。**此次源码和Prompt有新版本，旧实验不可按新代码静默续跑。** 新入口在你运行命令的同一终端捕获当前模型/CLI/PATH，避免把助手环境的PATH作为你的初始运行环境。

先确认 `.env` 的 `TEST_DATABASE_URL` 是 `researchpilot_test`，且当前模型可用。入口只连接该测试库，在独立schema中创建版本/实验必要表，不使用生产 `DATABASE_URL`，不启用排程或发布。数据库环境切换仅在独立CLI进程中完成；不通过修改模型环境变量切换角色。

```bash
python run_evolution_agent.py \
  --dataset outputs/acceptance/evolution_round1_frozen_d58juwof/dataset.json \
  --output-dir outputs/experiments/autonomous_prompt_002 \
  --confirm-model-calls
```

默认：控制器最多8步、1个候选、共享24次模型调用、总期限1200秒；所有请求（回答、辅助评审、控制器、失败）计入预算。每次程序请求不额外重试。4题时基线回答+评审最多8次、候选最多8次、控制器最多8次。Codex CLI内部可能存在供应方重试，不把供应方内部传输次数或未知token/费用虚报为精确计费。

控制器返回无效/截断的结构时，本步不执行工具，错误进入下一步观察，由模型在剩余步数和共同调用预算内纠正；未知中断、网络失败及权限错误不通过重新发同一个请求隐藏。

只登记、不调用模型可将最后一行换成 `--prepare-only`。同目录再次执行复用原登记版本和检查点；预算、期限与尝试不会归零，已完成请求不重发，未知中断请求不会偷偷再发一次。配置变化需新目录/新版本，不删除旧检查点绕过预算。

新增输入字段使用类型校验；输入仅接受获准development集合（最多20题），不把holdout/regression要求放入改进者上下文。`requirements`、`human_judgments` 和原始gold标签不送控制器、回答模型或辅助评审；程序持有完整材料仅做完整性核验、固定分母统计。

## 产物与结果解释

- `registration.json`：实际任务、角色版本、固定参数和测试schema。
- `<result_id>.json`、`result_<result_id>.json`：不可变执行内容及摘要；`result.json`只是最新内容索引。
- `trajectory_<result_id>.jsonl`：实际模型工具选择、参数、观察、失败、拒绝与停止依据。
- probe JSON：每题实际回答、引用与辅助模型评审，失败及缺失保留。
- 结果的`usage_by_role`统计控制器/回答/评审的预扣请求、已知开始和未知中断；token/费用未知为null。probe内普通任务指标的calls只统计任务回答请求，不冒充全轮模型用量。
- `checkpoints.sqlite3`：预扣、调用开始/结束、租约、工具动作与结果；崩溃恢复沿用。
- `attempts/`：入口失败/恢复记录，不保存凭据和原始供应方异常文本。

`completed`表示模型结束了开发流程，不保证提出候选、不保证候选胜出、更不表示科研结论已人工核实。`partial`保留真实进度和预算/期限/失败原因，不能作为实验成功率的成功任务。应分别查看逐题运行状态、来源检查、辅助评审和最终停止原因。

真实联调发现的提前结束已修复：Codex桥接明确区分“禁用CLI原生工具”和“输出宿主JSON动作”。控制器必须实际读取基线反馈才能finish；提出的候选必须先probe。基线辅助评审不完整时结束为partial，不报完整成功。终端同时显示workflow_status、candidate_count、comparison_count和baseline_auxiliary_failures，区分仅有基线与已实际对照。

read_feedback默认只返回整体统计和全部逐题状态，cases为空表示总览；须传case_id展开单例，分母始终保留全部任务。超长观察使用结构化摘要和明确标记的回答/理由预览，不截断JSON；完整原始观察仍保存在轨迹。控制器另收到持久progress，旧history裁剪后也能知道已经读取和验证的内容。新请求保留脱敏供应方诊断，便于区分桥接问题与评审请求自身失败；旧记录未采集的信息不伪造回填。

旧`autonomous_prompt_001`轮保留原样：10次请求，4个基线回答、3个成功辅助评审和1个评审失败，控制器只调用capabilities/finish，未生成候选。此记录不是已完成候选演化，也不能据空的候选列表推断比较后没有收益。修复后的新版本应使用新的输出目录，例如`outputs/experiments/autonomous_prompt_002`，不修改旧预算/轨迹后冒充续跑。

`autonomous_prompt_002`已真实完成诊断分支：4题回答、来源检查及4次辅助评审成功，控制器读取了总览、单例反馈和冲突技能，主动保留基线；14次请求，无候选/对照。这次是合理的`model_no_change_after_diagnosis`，不是提前退出，也不代表候选链路已真实验收。

为单独验收候选路径，新增显式模式。它只要求LLM提出并probe一个实际不同的通用Prompt/技能候选，不要求胜出、不修改分母、评分或生产晋级门槛；模式和预算一起固定。自然优化模式默认仍可在真实诊断后结束，不强迫无意义改动。流程验收结果不能冒充策略收益。

```bash
python run_evolution_agent.py \
  --dataset outputs/acceptance/evolution_round1_frozen_d58juwof/dataset.json \
  --output-dir outputs/experiments/autonomous_prompt_004 \
  --exercise-candidate --confirm-model-calls
```

新模式仍为最多24次请求、8步、1候选。候选与基线完全相同会被程序拒绝；若未能在预算内完成probe，则保持partial，不能伪报流程通过。旧轮不改写，代码/模式改变时使用新目录。

2026-11-24收尾修复：旧`autonomous_prompt_003`真实执行了基线和候选各四题，来源检查及辅助评审均通过，指标无增益，维持基线；重复读取反馈消耗步骤，第八步执行probe后因step_limit结束为partial。此记录保留原样，不追改为成功。新版本按摘要/单例分开反馈，提供持久进度和调用预算提示；首次候选probe启动前检查回答与评审之外是否还留有一步、一次控制器请求用于finish，不足则以`AUTONOMOUS_FINISH_BUDGET_INSUFFICIENT`保持partial。完整比较仍由程序保存，不强制多一次compare_probe。上限仍为八步和二十四次，不保证所有模型决策都能在预算内完成。使用新目录`autonomous_prompt_004`验证修复；旧目录不重置预算，旧版本不按新源码静默续跑。

有来源不退步、运行不退步且辅助“支持并回答”项增加的候选，只标为“值得独立验证”。缺失/失败的辅助评审不算正确，所有任务始终在分母。辅助判断不填写为人工gold，不授予生产晋级。本入口始终`production_decision=retain_baseline`；之后仍须已有独立材料、人工判断及冻结GatePolicy验收。

## 验收

```bash
TEST_DATABASE_URL='' DATABASE_URL=sqlite:// \
LLM_PROVIDER=openai LLM_API_KEY=isolated-test-key \
LLM_MODEL=isolated-test-model LLM_BASE_URL=https://example.invalid/v1 \
python -m pytest -q -p test.audit_isolation \
  test/test_evolution_agent.py test/test_evaluation_adversarial.py \
  test/test_evolution_gate_adversarial.py test/test_research_deferred_workflows.py \
  test/test_research_operations_deferred.py test/test_task_quality.py test/test_runtime_snapshots.py
```

重点断言实际角色请求收到原模型/超时/零程序重试，候选Prompt和技能进入真实调用，review不继承候选指令；工具/候选/权限约束、共同预算、未知中断、成功恢复和gold隔离均由程序测试。受控测试不等于真实模型或PG联调，真实结果单独记录。

2026-11-23本轮验证：新循环及相关回放、版本、分析、引用、分批、溯源和综述接口共213项受控回归通过，5条现有PyMuPDF弃用警告。时限用例曾因登记前取时间导致模拟超时不稳定，已改用实际持久deadline，原断言不变。真实新入口尝试停在`connect_test_database / TEST_DATABASE_UNAVAILABLE`，当前沙箱TCP权限受限，新增真实模型调用0次；未据此宣称新自主演化已真实联调通过。
