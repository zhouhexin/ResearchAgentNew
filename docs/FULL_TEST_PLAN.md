# ResearchPilot 全部测试与验收方案

适用：当前 P0–P2 研究流程、P3 固定材料任务回放、P4 数据策略实验、P5 回放组件排程与渠道管理。

按 A→H 顺序进行。**每个阶段分别记录结果，不能用自动测试通过代替真实运行或人工语义判断。** 失败先停在该阶段保存日志，再修复重测；不要为使流程通过而降低引用校验、篡改结果或放宽晋级规则。本方案只编写文档和脚本，没有替你运行真实调用、建表、排程或发布。

P3当前不支持科研全流程/实时检索自动回放，P4不执行任意代码补丁，P5不修改在线科研主流程的生产模型。对这些尚未实现的能力记“未实现/不在当前首版范围”，不能记通过。

## 准备：先固定环境与测试范围

1. 在项目根目录使用现有 Python 环境，例如 `conda activate py311`。Node需满足 `web/package.json` 的 `>=22.6.0`。依赖未安装时先按项目说明安装；前端建议先在 `web` 执行 `npm ci`。
2. 保存当前代码状态 `git status --short`、`git rev-parse HEAD`，不要为了测试丢弃未提交代码。**测试期间不要编辑代码/文档、换依赖或提交Git，也不要与测试并行构建**：运行快照严格检查代码内容，变化会导致任务拒绝恢复。
3. 业务人工测试优先使用单独的测试工作区/项目副本及测试 `.env`，不要重置真实论文库。独立副本只复制程序，不复制生产 `.env`、`outputs`、`data`、密钥或业务数据库；连接 `researchpilot_test` 或其他专门人工验收库。项目本来使用哪个环境就保持对应解释器，不照抄不适用的绝对路径。
4. 自动检查 A 不用真实模型、不连生产库。B 用受保护的 `TEST_DATABASE_URL` 临时schema。C–H 涉及真实模型或持久写入，务必在人工验收副本中进行。**环境变量优先于 `.env`**；`DATABASE_URL` 与 `TEST_DATABASE_URL` 是两个参数，前者用于业务CLI/服务，后者用于数据库专项测试。不要因设置了后者就以为业务命令已经切到测试库。
5. 准备2–3篇你有权处理且读过的论文（A/B/C）；验证原文和业务流程不等于授权进入评测集。P3导出还需明确评测授权。尽量有不同方法/实验条件；至少A/B已下载全文，C用于补充材料。控制首次真实调用规模：2篇快读、少量结论和1个问题，不先跑全部长论文。
6. 不把 `.env`、密钥、完整数据库连接串、Cookie或私有论文/原始日志提交Git。日志回传前删除凭据与私人研究内容。

记录格式：`测试ID | PASS/FAIL/BLOCKED/NOT_TESTED | 计划/执行/任务/产物ID | 预期 | 实际 | 日志或截图 | 人工备注`。BLOCKED和NOT_TESTED不计为通过。

## A. 全部隔离自动检查（先做，无真实模型调用）

项目根目录：

```bash
conda activate py311
bash scripts/run_acceptance_checks.sh
```

若 `python` 不是正确环境，可显式指定解释器：

```bash
RESEARCH_TEST_PYTHON=/opt/homebrew/anaconda3/envs/py311/bin/python \
  bash scripts/run_acceptance_checks.sh
```

脚本不改 `.env`；临时设置fake模型、SQLite及空TEST_DATABASE_URL，使用 `test.audit_isolation` 将任务、上传和检查点放在每项测试临时目录。后端全部测试通过后依次跑前端构建、单测及所有现有组件/页面检查。第一个失败处停止，输出目录为 `outputs/acceptance/automated_随机串/`，含版本、工作树文件列表、汇总、各项日志和后端JUnit XML。

预期：后端无failed/error；依赖真实PG的专项可以在A中skip，但须记为未验证，留到B补齐。测试总数以本次输出为准，不要求等于历史数字。前端全部PASS；构建、引用跳页、评审、补读、补检索、验收、评测工作区交互均检查。脚本不安装依赖、不建业务表、不执行实际排程/发布。

若希望先快速定位P3–P5，可先运行下列命令，之后仍需A全量：

```bash
TEST_DATABASE_URL='' DATABASE_URL=sqlite:// \
LLM_PROVIDER=openai LLM_API_KEY=isolated-audit-key \
LLM_MODEL=isolated-audit-model LLM_BASE_URL=https://example.invalid/v1 \
python -m pytest -q -p test.audit_isolation \
  test/test_evaluation_adversarial.py test/test_evolution_gate_adversarial.py \
  test/test_research_deferred_workflows.py test/test_research_operations_deferred.py \
  test/test_task_quality.py test/test_runtime_snapshots.py --tb=short
```

这组此前72项通过，是历史结果，不保证当前修改后的结果；它不等于整个项目全量检查。

## B. 真实 PostgreSQL 专项（只允许测试数据库）

在测试副本 `.env` 配置 `TEST_DATABASE_URL`，数据库名必须为 **researchpilot_test**，用户有创建/删除临时schema的权限。保留真正的驱动和连接参数，不在聊天或文档中贴真实密码。

```bash
python -m pytest -q -p test.audit_isolation \
  test/test_repository.py test/test_document_repository.py \
  test/test_runtime_versions_postgres.py --tb=short
```

用例会为每项创建独立schema，结束后仅删除自己的schema，不清空现有业务表。

|ID|检查|通过条件|
|---|---|---|
|B1|论文存取、DOI/OpenAlex去重|重复保存不多建论文，冲突不误合并|
|B2|事务失败/回滚|失败不留下半条业务结果|
|B3|全文及页码/字符范围|引句对照、文档变化与旧证据失效逻辑正确|
|B4|版本仓库JSONB与并发幂等|同快照并发只保存一份，读回内容/哈希一致|

连接失败/缺权限/skip均不算B通过。P5专属真实PG租约/并发发布还需H的手工演练；现有B不覆盖这些语义。

## C. 启动真实测试服务与模型连接（有实际调用）

以下操作在人工验收副本/专用库中进行，**先确认该副本的DATABASE_URL为验收库**。需要已有业务论文表，业务基础表沿用原项目建库方式；不要对生产执行清库。

```bash
python -m storage.init_db
python -m storage.init_runtime_versions
python manage_research_operations.py init-tables
```

预期：建立缺失的业务表与来源目录，补齐5张运行/溯源/覆盖表，以及2张排程/回放渠道表。不启动调度、不发布模型。不要根据旧文档“四张表”的历史描述漏掉覆盖表。

`.env` 中启用 `RESEARCH_RUNTIME_PINNING=true`；原 `ANALYSIS_RUNTIME_PINNING` 可保留。Codex模式须显式指定本账号可用的 `CODEX_MODEL`；API模式使用有效的 Key/地址/模型。不要在普通命令日志中输出密钥。

后端终端：

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8001
```

前端另开终端：

```bash
cd web
npm run dev
```

打开终端显示的地址（通常 `http://127.0.0.1:5173`），在设置页切换API模式。Vite默认代理后端8001，不要一边启动8000、一边仍代理8001。

```bash
curl --fail http://127.0.0.1:8001/api/v1/ping
curl --fail http://127.0.0.1:8001/api/v1/system/health
python run_paper_analysis.py --check-llm --timeout 60
python run_paper_analysis.py --check-llm --structured --timeout 120
```

预期：ping成功，health的api/database/model_configured正确；普通请求和短结构化/原文检查通过。health只检查模型配置，不证明网络连通；短模型请求通过也不代表长论文批次均能成功。模型服务不可用则C记BLOCKED，先保留错误类型，不继续消耗多次调用。

## D. P0–P2 浏览器完整业务流程（核心验收）

### D0 已有功能回归

以下各项也属于完整验收，不要只测新计划流程。涉及外网或模型时，每项先取少量结果；外部服务拒绝访问应明确记BLOCKED，并保留程序是否正确报告错误的检查结果。

|ID|操作|必须观察到的结果|
|---|---|---|
|D0-1|设置页切换演示/API模式，刷新|演示数据与真实数据明确区分；健康状态可读，浏览器不显示密钥；无效页面有返回入口|
|D0-2|arXiv关键词检索、年份过滤、翻页、空结果|条件与结果一致；保留摘要；无结果不伪造论文，错误不冒充空结果|
|D0-3|顶刊顶会展开/收起、多选、联合检索、取消全部|布局各行一致，已选项清楚；无选择时不提交；换条件后分页重置|
|D0-4|逐个测试当前13个来源，每个先取1条候选|CVPR、ICCV、AAAI、IJCAI、ACL、EMNLP、SIGIR、NeurIPS、Artificial Intelligence、TOIS、ECCV、ICML、ICLR分别记录；没有候选不能证明官方核验成功|
|D0-5|核对主会/Workshop/Findings/期刊与身份过滤|标题、作者、年份及可得DOI与实际官方材料对应；未知保留待核验，期刊不标成主会，预印本不冒充发表|
|D0-6|单条/批量收藏，重复收藏，刷新文献库|新增/已有/失败数真实；重复不增加；标题筛选、状态筛选、分页与详情可用|
|D0-7|生成一条摘要中文翻译，再刷新查看|原摘要保留，译文单独标注；命中同配置缓存不重调模型；无摘要不能发起翻译|
|D0-8|对一篇确实可开放下载的论文获取原文，再次获取|展示查找/下载/解析阶段与实际来源；PDF身份一致；重复不多建同一文件/片段；失败能区分未找到、下载超时、解析失败|
|D0-9|手动上传同一PDF及重命名副本；另用空/非PDF文件测试|同内容复用；错误文件有明确提示，不生成空全文；扫描件不冒充已完成OCR|
|D0-10|独立论文分析页查看快读/抽样/全文、历史和导出|实际范围与页面一致；关键章节优先；切片尽量保留句界，原文位置可核对；历史分析不被覆盖|
|D0-11|综述页推荐研究问题、选择/修改问题、指定分析版本|确认后才生成；问题与所选论文有关；报告确实使用选定版本，失败不发布半份成功报告|
|D0-12|任务中心查看进行中/成功/失败任务并刷新|状态和进度来自后端；结果链接打开正确论文/计划/报告/评测产物；未实现的取消操作不显示为已取消|
|D0-13|桌面和较窄窗口检查搜索、文献库、计划、综述、评测页|按钮/输入不被遮挡，长标题与编号可读，表格可滚动；浏览器控制台无影响操作的异常|

D0-4的逐来源联网检查与D0-5的发表身份核验分别记录。自动用例中的模拟网页不能代替真实来源可用性。现有普通Agent自由问答若仍提示未接入，则记未实现，不把规划工具可用写成自由问答已实现。

### D1–D16 研究计划闭环

新建一个测试计划，不继续用因本轮代码变化已不可恢复的旧计划。建议目标：“根据已有文献库中的两篇论文，比较方法、实验条件和局限，形成带原文的中文综述；先不外部检索。”选与你真实论文一致的主题，不强行让无关论文产生比较结论。

|ID|操作|必须观察到的结果|
|---|---|---|
|D1|文献库上传A/B、重复导入、刷新|论文归属正确；重复不误增；全文有实际页码和原文片段|
|D2|制定/编辑/保存计划|问题、范围、修订可查询；旧执行保留旧计划|
|D3|从已有文献库选A/B，指定有效分析或快读|无需先外部检索；来源显示library；实际分析ID/阅读范围正确，不冒称全文|
|D4|分批分析或复用分析|每批核验保存，引用属于对应论文/文档；复用不冒称本轮生成|
|D5|独立评审一篇分析|显示固定分析版本、逐条状态/理由；支持/矛盾等只是辅助判断；可打开实际原文|
|D6|问题与维度确认、比较和综述|确实综合至少2篇材料，无法比较处明确说明，不造数值|
|D7|导出MD/JSON/CSV|文件实际下载并可打开；MD脚注跳转、完整原文引句、分析/评审ID正确；CSV可读|
|D8|点击站内引用|打开对应PDF及对应页；无PDF时明确降级，不跳到别篇论文|
|D9|生成覆盖报告|区分已回答/部分/未回答/冲突，引用与缺口有依据；未关联评审明确未评审|
|D10|按建议选择真实E编号、少量片段补读|先预览，无模型调用；确认后才补读；保留原分析，另存新版本和范围并集|
|D11|显式用新分析重查覆盖|新执行分支使用新分析，旧报告保留；不继承不属于新分析的旧评审|
|D12|执行内选择完整评审并重查覆盖|只关联本次选择的实际review_id，未选项不算已评审；后续综述关联同版本|
|D13|受限arXiv补检索|确认查询/数量/期限后才检索和初筛；原有来源约束不被扩大；不自动下载/分析|
|D14|收藏一篇真正相关候选C，手动下载/分析后关联|只接受身份匹配且有效的分析；保留原A/B，总数不超5；创建新分支而非覆盖|
|D15|新分支重查覆盖并生成更新综述|材料/评审/执行ID一致；原分支可回看，不能把候选摘要当全文结论|
|D16|研究流程验收入口|程序检查和人工声明分开；默认未检查；通过/失败需理由；可保存未完成记录并导出|

D9–D14需要真实缺口/相关候选；如果合理覆盖报告没有生成对应建议，不改标签或编造缺口强行跑通，应另选适合的测试问题。补检索只支持arXiv首版，原计划若限定正式会议/期刊必须拒绝；此拒绝本身是通过。全文下载如遇网络失败，记录“定位阶段/下载阶段/错误码”，可以单独上传已获准PDF继续其他测试，但下载能力仍记未通过，不能掩盖。

### D的人工质量抽查

选至少10条结论作初始检查，覆盖方法、实验、限制、数字/单位/比较条件；有多少实际检查就写多少，不把数量当固定质量门槛。逐条记录论文/页码/E编号/原文、结论、模型理由和你的判断。特别区分“研究某方法是否适用”与“已证明适用”，相对增幅与百分点、单一条件与所有场景。看过模型后改标要注明背景并保留旧记录。

已有结论级人工标注材料包也需回归：检查标签、理由、人工修订记录、导出和重新载入。先使用其测试副本执行输入检查，再只选一个获准的开发样本验证`references-v2`评审：

```text
python run_claim_evaluation.py --dataset 测试材料包路径 --sample-id 实际开发样本ID --check-only
python run_claim_evaluation.py --dataset 测试材料包路径 --sample-id 实际开发样本ID --quote-protocol references-v2 --output-dir outputs/acceptance/claim_review_001
```

第二条会调用真实模型。不得直接用默认命令重跑历史留出集；没有测试材料包则记NOT_TESTED。原`verbatim-v1`与`references-v2`分别保存比较，不合并历史成绩；引用由程序取回原文，人工改标不覆盖模型原判断。结论级标注与F的任务级要求判定是两套评测，分母分别统计。

## E. 异常、恢复与运行隔离

破坏性故障只在隔离副本制造，不在真实库改原文或删文件。优先使用A中的模拟故障；下面真实演练每项一次即可，不需反复耗费额度。

|ID|场景|通过条件|
|---|---|---|
|E1|分析中断，重启并继续|已核验批次不重跑；未知状态明确中断；固定范围/模型沿用原任务|
|E2|评审某条失败后继续|原review_id不变，只补未完成项；不重分析|
|E3|覆盖/综述失败后继续|复用固定输入与已有分支，不改写成功历史|
|E4|任务A入队后修改新任务默认配置|A继续原参数，新任务B用新参数；以实际请求/固定快照为依据，不只看版本ID|
|E5|原凭据/身份文件或代码artifact不可用|停止并说明不能恢复；不套用当前默认值；不删除原检查点|
|E6|固定原文/评审在隔离库变化|新结果拒绝混用；旧报告可读，标为历史；旧验收声明提示复验|
|E7|补读/补检索预算或期限用尽|明确停止，已核验结果保留；续跑不重置预算，迟到响应不发布|
|E8|重复点击、两个客户端提交同一任务|不重复修改同一执行，活动任务正确去重；已结束任务保留历史|
|E9|把其他论文分析/证据/评审冒充本轮|后端拒绝，而非仅页面隐藏|

E4不要在执行中修改模型实际凭据或网络代理来模拟“默认切换”，那会使旧凭据无法恢复，属于E5。模型/配置固定正确不保证远端模型版本永不漂移，也不保证输出逐字相同。

## F. P3离线评分与真实固定材料回放

### F1 无调用离线评分

```bash
python run_task_evaluation.py \
  --dataset evaluation/examples/task_quality_v1/dataset.json --check-only

python run_task_evaluation.py \
  --dataset evaluation/examples/task_quality_v1/dataset.json \
  --baseline evaluation/examples/task_quality_v1/baseline.json \
  --run evaluation/examples/task_quality_v1/candidate.json \
  --output-dir outputs/acceptance/offline_demo_001
```

目录必须全新；如果已存在，换编号，不删旧结果。预期：模型调用0；4个任务分母；基线2成功/1失败/1缺失；候选3成功/1缺失但有1处引文错误；不自动晋级，未知token/费用为null。JSON/MD均可读且包含逐例差异。这个示例全部合成，不是实际质量结果。

### F2 真实回放（会调用模型，起步只用1例×1轮×最多2次）

在“研究评测”高级页面或CLI操作。最方便先使用合成材料测试执行管道，再另行明确授权真实论文用于质量评测。测试环境版本固定后不编辑代码/依赖。

1. 从示例dataset.json复制一个仅含development案例的包到忽略目录 `outputs/acceptance/dataset_dev.json`，或按 `evaluation/task_export.py` 的ExportCase契约，指定有权评测的真实analysis_id、人工要求和授权说明导出。真实材料不能仅因你能阅读就视为获准进入评测。
2. 先登记材料、配置，记下返回ID：

   ```bash
   python manage_research_evaluation.py register-dataset outputs/acceptance/dataset_dev.json
   python manage_research_evaluation.py register-binding --profile standard --timeout 120 --tokens 1200
   ```

3. 新建 `outputs/acceptance/replay_job.json`，替换两个真实返回ID：

   ```json
   {
     "dataset_artifact_id": "experiment_替换为数据集产物ID",
     "binding_artifact_id": "experiment_替换为配置产物ID",
     "split": "development",
     "repetitions": 1,
     "max_calls": 2,
     "total_seconds": 300,
     "max_attempts": 1,
     "holdout_authorized": false
   }
   ```

4. 准备并明确执行，记下job_id和result_artifact_id：

   ```text
   python manage_research_evaluation.py prepare outputs/acceptance/replay_job.json
   python manage_research_evaluation.py execute JOB_ID --confirm-model-calls
   python manage_research_evaluation.py read RESULT_ID --output outputs/acceptance/replay_result_001.json
   ```

   上面大写ID是占位符，不要直接复制执行。实际模型调用不超过budget；无SDK叠加重试。输出不是所有结论必然正确，Codex的Token硬上限由CLI管理。

5. 预期：产物的job_id正确，实际版本可查，attempts中有真实生效RuntimeSnapshot；run.outcomes中状态/错误明确，人工标签为空，semantic_review为not_collected。模型只选span编号，引用来自后端固定原文。再执行同一job，不重跑已成功案例，不归零预算。
6. 制作一次失败/材料撤回验证：**先用新登记的测试数据包**准备但不执行任务，再执行 `withdraw DATASET_ID --actor 测试人 --reason 撤回测试`，之后执行该job应拒绝且不调用模型。撤回不可用“删除记录”偷偷恢复；需要再授权时另建包/版本。

### F3 人工标注与实际结果比较

- 人工查看固定问题、材料、原回答、引用与要求后填写Judgment。记录assessor/reason/context，未判定不填；不能给模型返回的支持标签冒充人工判断。
- 标注JSON格式：`{"run_fingerprint":"原run对象规范化指纹","items":[{"case_id":"...","repetition":1,"judgments":[{"requirement_id":"R1","verdict":"met|partial|unmet|uncertain","assessor":"姓名","reason":"实际核对依据","context":"independent|after_model_inspection","previous_verdict":null}]}]}`。竖线表示需要选择其中一个实际值，不是可照抄的合法值。
- 指纹可用项目 `agent.runtime_snapshot.fingerprint` 计算，对导出结果的 **run对象** 计算，不是整个文件哈希。

```text
python manage_research_evaluation.py annotate RESULT_ID outputs/acceptance/judgments.json
python manage_research_evaluation.py read ANNOTATION_ID --output outputs/acceptance/annotated_001.json
```

返回的新归档保留原运行，只允许改人工判定。归档不能改answer/status/引用/用量/版本；用fake测试验证篡改被拒绝，不改真实结果。

在网页“比较两个已登记结果”输入 `{"baseline_id":"...","candidate_id":"...","allow_holdout":false}`。也可将两个导出文件各自的run对象另存新JSON，交给 `run_task_evaluation.py --baseline ... --run ...`。同数据集、集合、轮次、模式才能比较；缺失/失败分母不能减少，模型影响后改标与独立判定分开统计。

## G. P4候选实验与晋级拒绝（先小规模，不实际发布）

开发数据包只含development，不含holdout/regression要求。不要把预留论文/答案用于候选调优。使用F登记的baseline binding和数据集。配置文件存 `outputs/acceptance/`，文件中不要放密钥。

1. 冻结规则 `gate_policy.json`：

   ```json
   {
     "revision": "test-v1",
     "author": "测试人",
     "frozen_before_experiment": true,
     "max_success_rate_drop": 0.0,
     "max_independent_met_rate_drop": 0.0,
     "min_success_rate_gain": 0.05,
     "min_met_rate_gain": 0.05,
     "minimum_independent_groups": 2
   }
   ```

2. 候选文件 `candidate.json`：

   ```json
   {
     "hypothesis": "优先核对实验条件可能减少超范围概括错误。",
     "rationale": "依据开发材料中遗漏适用范围的问题，提出待验证策略，不改变引用和评分规则。",
     "parent_binding_id": "替换为BASELINE_BINDING_ID",
     "strategy": {"profile": "conditions_first", "max_input_chars": 60000, "max_output_tokens": 1200},
     "author": "测试人"
   }
   ```

3. 冻结规则、登记候选，记录ID：

   ```bash
   python manage_research_evolution.py freeze-policy outputs/acceptance/gate_policy.json
   python manage_research_evolution.py candidate outputs/acceptance/candidate.json
   ```

4. `experiment.json`：

   ```json
   {
     "baseline_binding_id": "替换为BASELINE_BINDING_ID",
     "candidate_ids": ["替换为CANDIDATE_ID"],
     "policy_id": "替换为POLICY_ID",
     "max_rounds": 1,
     "dataset_artifact_id": "替换为DEV_DATASET_ID",
     "max_calls": 4,
     "total_seconds": 600,
     "author": "测试人"
   }
   ```

5. 明确准备并执行：

   ```text
   python manage_research_evolution.py prepare-experiment outputs/acceptance/experiment.json
   python manage_research_evolution.py execute EXPERIMENT_ID --confirm-model-calls
   python manage_research_evaluation.py read EXPERIMENT_RESULTS_ID --output outputs/acceptance/experiment_result_001.json
   ```

预期：最多1轮，共享4次调用（包含基线与候选的全部调用/失败）；子作业绑定parent_experiment_id，不能通过独立CLI/API/排程绕过。候选仅为数据策略，不接受任意code_patch、holdout_answers、修改gate_policy。实际结果可能失败或不足，仍保存分母与停止原因，不自动变更生产。

`diagnose DEVELOPMENT_RESULT_ID` 可基于失败类型输出待验证假设，不是已证实改进，不会读取留出结果。

### G的晋级测试

1. 给基线和候选的实际结果作独立人工评审，然后执行：

   ```text
   python manage_research_evolution.py compare EXPERIMENT_ID BASELINE_ANNOTATION_ID CANDIDATE_ANNOTATION_ID
   ```

2. 开发集/合成材料、同源留出、缺独立人工判断、无明确收益或出现确定性来源错误，均应返回 `retain_baseline` 及具体原因，production_changed=false。这是通过，不是故障。
3. “允许手动发布”的正向真实验收需另备至少两个独立组的**获授权自然holdout材料**，与开发包无同源文档/论文，使用同一实验登记的baseline/candidate binding，各自明确holdout_authorized=true并执行、独立人工标注后比较。所有结果须同holdout数据版本/重复数。候选binding可从读出的evolution_experiment内candidate_binding_ids取得。
4. 不保证真实候选一定改善；如果没有合格决策，H的正向发布记NOT_TESTED，拒绝发布检查仍可通过。**不要为了测试发布通路放宽规则、强改标签或手写eligible决策。**

## H. P5独立排程、并发与回放渠道（只在专用库）

先确认C的2张P5表已准备。当前尚无全套真实PG并发自动用例，本阶段必须保留运行记录；A中的SQLite替身不能替代这些行锁/租约验收。

### H1 排程默认禁用与总预算

新建 `schedule.json`，job选F准备的**无parent_experiment_id、非holdout**普通测试作业。确认该job最大2次调用，则排程也最多预留2次：

```json
{
  "id": "acceptance_schedule_001",
  "job_id": "替换为普通JOB_ID",
  "interval_seconds": 60,
  "max_runs": 1,
  "max_total_calls": 2,
  "expires_at": 需要替换成未来一小时的浮点Unix时间戳,
  "actor": "测试人",
  "enabled": false
}
```

这是模板，`expires_at`的中文占位符不是合法JSON，必须替换。未来一小时时间可用 `python -c 'import time; print(time.time()+3600)'` 得到。

```text
python manage_research_operations.py schedule outputs/acceptance/schedule.json
python manage_research_operations.py worker-once --worker-id acceptance-a --confirm-model-calls
```

默认应dispatched=false且无模型调用。然后明确启用：

```text
python manage_research_operations.py enable acceptance_schedule_001 --actor 测试人
python manage_research_operations.py worker-once --worker-id acceptance-a --confirm-model-calls
python manage_research_operations.py worker-once --worker-id acceptance-b --confirm-model-calls
python manage_research_operations.py disable acceptance_schedule_001 --actor 测试人
```

预期：仅一轮派发，第二次不再执行；calls_reserved按job最大2次保守预留，不因崩溃归零或返还未知用量。每轮有独立dispatch_identity，不只命中之前普通job的成功缓存。worker返回result_id后读产物核对，包含失败项不能算全成功。

### H2 真PG防重入与崩溃演练

- 在隔离库用新的单轮schedule，两个终端尽量同时执行worker-once；只有一个获得该轮租约，另一个不派发；检查实际调用未双倍。
- 在一次隔离worker执行中终止该worker进程，只终止你手动启动的测试进程；不用 `killall`。记录schedule ID、PID与时间。
- 租约到期前其他worker不应重复接管，过期后的新派发也不能突破总轮数/总预扣上限；旧worker不能覆盖新租约结果。不要手动篡改生产表加速到期，耐心等待测试作业的total_seconds+120秒租约期限。
- 禁用schedule、超过expires_at、超max_runs/max_total_calls、指定holdout或实验子作业，均应不派发/明确拒绝。
- 使用测试库的SQL客户端只读查看 `evaluation_schedules` 的spec、runs_reserved、calls_reserved、history、lease_until、next_at，保存必要ID和计数；不需分享连接串或原始论文内容。

### H3 发布拒绝、灰度与回滚

```text
python manage_research_operations.py init-channel acceptance_replay_channel BASELINE_BINDING_ID --actor 测试人
python manage_research_operations.py route acceptance_replay_channel stable-task-001
```

初始revision=1、candidate=null、灰度0，路由为baseline。

|场景|预期|
|---|---|
|未传--authorize-release|拒绝变更|
|决策为retain_baseline|拒绝发布，revision不增加|
|决策来自其他baseline渠道|拒绝跨基线发布|
|旧revision并发更新|只允许一个修订成功，另一个拒绝|
|同渠道、同new_task_id重复route|未修改渠道时结果一致|
|已prepare的任务随后渠道改变|仍用作业内固定binding/route，不在途中重新分桶|

若G3获得真实合格决策，才执行正向步骤：

```text
python manage_research_operations.py publish acceptance_replay_channel ELIGIBLE_DECISION_ID --revision 1 --actor 测试人 --canary-percent 10 --authorize-release
python manage_research_operations.py route acceptance_replay_channel stable-task-002
python manage_research_operations.py route acceptance_replay_channel stable-task-003
```

revision变2；10%灰度不意味着任意10个ID必有一个候选，按routing_bucket判断：小于10时candidate，否则baseline。需要实际创建新作业验证路由时：

```text
python manage_research_evaluation.py prepare outputs/acceptance/replay_job.json --channel acceptance_replay_channel --new-task-id stable-task-002
```

检查新作业保存channel_revision/binding/routing_bucket；不要另用默认模型冒充候选。

```text
python manage_research_operations.py rollback acceptance_replay_channel --revision 2 --actor 测试人 --authorize-release
python manage_research_operations.py route acceptance_replay_channel stable-task-004
```

预期：revision变3，candidate清空，未来新作业回到兼容baseline；历史路由、候选结果、模型配置和所有检查点保留，已入队任务不改配置。baseline artifact不可恢复时必须拒绝回滚，不假装换个默认模型即恢复成功。只测了拒绝路径就不要把正向灰度/回滚记通过。

## 收尾与整体判定

完成后停用所有测试排程，关闭手动测试服务；不自动删除材料、版本、预算或报告。不要清空正式文献库。需要清理专用库时先保留本轮证据并另行确认删除范围。

|层次|什么时候能写通过|
|---|---|
|自动回归|A无failed/error，全部前端脚本成功；skip列明|
|真实数据库|B和对应H项确实使用PG完成；不以SQLite替代|
|模型连通/结构|C实际请求成功；长流程仍单独判断|
|P0–P2流程|D/E逐项有实际ID/文件/异常恢复记录，未做项保留|
|人工研究质量|逐条对照原文及实际上下文，保留理由；不以模型supported/answered作证明|
|P3–P5首版|F/G/H范围内逐项验收；无独立自然材料或合格候选则晋级/发布正向项仍待验收|

完成A/B后即可先反馈，不需要等所有模型任务结束。反馈建议：

```text
代码HEAD / 未提交修改：
A：后端 passed/failed/skipped；前端哪个脚本失败；outputs/acceptance目录：
B：是否连接researchpilot_test；失败用例（不贴密码）：
C：API/DB/模型检查结果：
D/E：失败的测试ID、task_id、错误码、复现步骤：
F/G/H：job/result/experiment/schedule/channel ID；实际次数；停止原因：
人工质量：核对条数、明确错误/歧义及原文页码：
NOT_TESTED / BLOCKED 项：
```

出现失败时提供最后30–60行脱敏日志和对应ID即可，不要上传整份含私密材料/密钥的运行目录。
