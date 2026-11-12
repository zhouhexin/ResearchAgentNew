# 接口说明：网页怎样向 Python 提要求

这份文档前半部分解释工作方式，后半部分保留给开发时查阅的地址和字段。

**目前这些网页接口还没有实现。这里是约定，不是可直接调用的服务清单。**

## 1. 什么是接口

比如你在页面里选中论文 2，点击“开始分析”。
页面不能直接去执行数据库或模型代码，而是向 Python 后端发送一条要求：

> 请分析论文 2，这次最多读 6 个片段。

Python 后端接收要求后，检查参数，再安排实际工作。
这条要求发送到哪个地址、包含哪些字段、结果怎样返回，就是“接口约定”。

## 2. 一个具体例子

开始分析时，页面发送：

```http
POST /api/v1/analyses
```

携带的数据：

```json
{
  "paper_id": 2,
  "max_chunks": 6
}
```

- `paper_id`：论文在本项目数据库中的编号。
- `max_chunks`：这次最多读取几个原文片段。
- `POST`：通常表示提交一项操作。

分析需要时间，后端不必一直让页面等到报告出来，而是先返回：

```json
{
  "task_id": "tsk_analysis_01"
}
```

这只是说明格式的例子，并不是真实任务。
`task_id` 是这项工作的编号，页面之后用它问进度。
这类“已接收，稍后处理”的回复状态叫 `202 Accepted`。

查询进度时：

```http
GET /api/v1/tasks/tsk_analysis_01
```

`GET` 通常表示查询。完成后，任务里会给出结果编号 `result_id`，页面再取回分析结果。

## 3. 传数据时的基本规则

| 规则 | 为什么 |
| --- | --- |
| 使用内部论文编号 `paper_id` | DOI、OpenAlex 编号和本项目论文编号不是一回事 |
| 列表每次只取一页 | 不用把整个数据库都塞到浏览器 |
| 片段返回所属论文和文档 | 点击 `P2:E21` 时，可以确认归属没有错 |
| 返回清楚的错误原因 | 用户知道是没全文、额度不足还是连接失败 |
| 下载用报告编号，不用本地路径 | 避免网页请求任意服务器文件 |
| 密钥和密码不传给页面 | 页面只需知道服务是否已配置 |

“每页结果”在代码里叫 `PageResult`，形式是：

```json
{
  "items": [],
  "total": 40,
  "page": 1,
  "page_size": 20
}
```

`items` 是本页内容，`total` 是符合条件的总条数，`page` 是当前页，`page_size` 是每页条数。
以上数字仅用于解释格式。

失败时统一返回：

```json
{
  "error": {
    "code": "FULLTEXT_REQUIRED",
    "message": "请先导入这篇论文的全文",
    "request_id": "example-request"
  }
}
```

`code` 方便程序识别错误类型，`message` 给人看，`request_id` 方便查找服务端的对应记录。
这些也是格式示例，不代表现有后端已经会返回它们。

## 4. 开发查阅：地址与功能

下表地址都以 `/api/v1` 开头，为了简短省略这个共同前缀。
字段的完整格式以 `src/types/research.ts` 为准；页面调用代码在 `src/api/index.ts`。

| 请求 | 通俗用途 | 发送 / 返回的关键内容 |
| --- | --- | --- |
| `GET /system/health` | 看服务是否就绪 | 返回 api/database/model_configured；可选 model_name 和 stats 统计。没有统计时页面显示破折号 |
| `GET /papers/search` | 搜论文 | 发送 q/source/start_year/end_year/sort/page/page_size，返回一页 SearchPaper |
| `POST /papers/batch` | 收藏所选论文 | 发送 `{papers}`，返回 created/existing/rejected 数量，也就是新增/已有/未入库 |
| `GET /papers` | 查自己的文献库 | 按 q/year/has_fulltext/has_analysis/page/page_size 查询，返回一页 PaperSummary |
| `GET /papers/{id}` | 看一篇论文资料 | 返回 PaperSummary，可含 abstract 摘要和 url 来源 |
| `GET /papers/{id}/documents` | 看这篇论文的全文记录 | 返回 DocumentSummary 列表；可预览 PDF 时提供 pdf_file_url，否则为 null |
| `POST /papers/{id}/documents` | 上传 PDF | 用 multipart 表单提交 file 和 confirmed=true；后端检查文件及论文关联，返回 202 和 task_id |
| `POST /papers/{id}/documents/from-url` | 导入网页全文 | 发送 `{url}`，返回 202 和 task_id；后端防止借此访问不该访问的内部地址 |
| `GET /documents/{id}/chunks` | 看原文片段 | 发送页码参数，返回一页 EvidenceRecord |
| `GET /evidence/{E编号}` | 打开具体引用 | 返回 EvidenceRecord，必须含 paper_id、document_id、原文和位置 |
| `POST /agent/chat` | 向助手提问 | 发送 session_id 和 message；返回会话编号、answer、citations、tools_used、citation_status |
| `POST /analyses` | 开始单篇分析 | 发送 paper_id/max_chunks，返回 202 和 task_id |
| `GET /analyses?paper_id=...` | 查某论文的分析历史 | 返回 AnalysisResult 列表，最新的放前面 |
| `GET /analyses/{id}` | 看某一次分析 | 返回 AnalysisResult，在现有 Python 分析格式上增加独立运行编号 id |
| `POST /reviews` | 开始多篇综述 | 发送 paper_ids 和 topic，2–5 个不同论文编号；后端记清楚所用的分析版本，返回任务编号 |
| `GET /reviews/{id}` | 取得综述和比较表 | 返回 ReviewResult：id/paper_ids/topic/draft/matrix |
| `GET /reviews/{id}/files/{format}` | 下载报告 | format 只允许 csv/json/md；返回文件内容及正确文件名，不返回本地绝对路径 |
| `GET /tasks` | 看任务列表 | 按 status/page/page_size 查询，返回一页 TaskInfo |
| `GET /tasks/{id}` | 看一项任务进度 | 返回 TaskInfo；成功后 result_id 指向实际的分析或综述结果 |

`{id}` 是要替换的位置，例如 `/papers/2`，不是把花括号原样发出去。
`null` 表示没有值，例如网页全文没有 PDF 页码，不应该拿 0 或 1 来代替。

### 返回数据的名字怎样理解

| 名称 | 通俗意思 |
| --- | --- |
| SearchPaper | 一条搜索结果 |
| PaperSummary | 文献库里一篇论文的简要资料 |
| DocumentSummary | 一份 PDF 或网页全文的简要资料 |
| EvidenceRecord | 一个原文片段及其所属论文、位置 |
| AnalysisResult | 一次论文分析的结果 |
| ReviewResult | 一次多篇综述的结果 |
| TaskInfo | 一项后台工作的状态 |

ReviewResult 的 `draft` 是现有的 ReviewDraft 综述结构。
`matrix` 中每篇材料包含 paper_id/title/analysis，页面再按研究维度摆成表格。

## 5. 核验和文件处理不能省略

`location_verified` 的意思是“原文位置已对上”，不是“科学结论已经证实”。

收到网页提交的 PDF 或地址后，后端还要自己检查格式、大小、归属和访问范围，不能只相信浏览器的检查。
后端还需保存任务和结果编号，才能让刷新页面后的查询继续有效。

如果后端最终返回的格式与这里不同，需要同步修改前端 `src/api/index.ts` 和数据类型。
页面不能直接读取 Python 本地文件或连接 PostgreSQL 来绕过这些约定。

## 已接通：PDF 上传与任务查询

- `POST /papers/{id}/documents`：PDF 限制 20 MB，需要 `confirmed=true`；仅支持可解析且不需要密码的 PDF。
- `GET /tasks/{id}`：查看解析进度。成功时 `result_id` 为全文记录编号，`paper_id` 为所属论文；失败时返回可展示的 `error.message`。
- `GET /tasks?status=&page=1&page_size=20`：任务列表；不筛选状态时省略 status 参数。
- 成功后重新查询论文详情、全文列表和片段列表；前端清除临时文件预览，改用服务端 PDF 地址。
- 当前后台任务用于单进程本地服务，任务状态保存在 SQLite，正文与片段仍保存在 PostgreSQL。重启会将未完成任务标为失败，不自动恢复执行。

## 已接通：获取开放原文

`POST /papers/{id}/documents/fetch` 无需请求体，返回 `202 {"task_id":"..."}`。页面切换到“全文阅读”，通过任务接口显示查找、下载、解析和入库状态。

后端按 DOI/OpenAlex ID 查询 OpenAlex，核对返回的论文标识，只使用开放来源的 PDF 地址。不使用标题模糊匹配，不把论文介绍页当成 PDF。一个来源失败时尝试其他开放来源。

任务成功后 `result_id` 指向全文记录。PDF 下载成功后另有 `downloaded=true`、`download_source_url`；后续解析失败时，这两个字段仍保留，且本地 PDF 不删除。找不到开放 PDF 时，任务失败并通过 `error.message` 提示手动上传。

自动下载与手动上传使用相同的内容哈希保存规则，相同文件重复获取复用已有全文与片段。演示模式不执行联网下载。

### arXiv 备用来源

获取接口保持不变，后台依次尝试 OpenAlex、arXiv。明确的 arXiv 编号按编号查询；否则核对完整标题、作者姓氏及返回的 DOI。下载失败也会尝试下一来源。任务成功后可带 `download_provider`（OpenAlex/arXiv），页面展示来源及预印本提示。仅有标题而没有作者的记录不会自动用 arXiv 标题检索绑定全文。

### 来源诊断与 DOI 原站

获取流程在 OpenAlex、arXiv 之外增加 DOI 原站页面的 PDF 元数据查询。新任务可返回 `source_results: [{provider,status,message}]`，由详情页和任务中心显示；旧任务无此字段时正常显示原有错误信息。当前自动尝试顺序为 OpenAlex → DOI 原站 → arXiv。

DOI 原站查询现支持正文里的同站 PDF 下载按钮；ETH Research Collection 页面还支持读取公开 ORIGINAL 文件列表。接口地址不变，仍由“获取原文”触发，任务记录中的 `download_provider` 为“DOI 原站”。

## 当前论文检索：仅 arXiv

`GET /api/v1/papers/search?q=monocular%20depth&source=arxiv&page=1&page_size=20`

- source 默认 arxiv；显式传 openalex 或 scholar 返回 422。
- start_year/end_year 按 arXiv 首次提交日期筛选。sort 支持 relevance/newest/updated，不支持 cited。
- 真实分页使用 arXiv 的 start/max_results 和 totalResults；最多展示前 30000 条。
- SearchPaper.source 为 arxiv；新增 arxiv_id、arxiv_version、pdf_url 和 published_doi。
- doi 使用稳定的基础编号 DOI（10.48550/arxiv.…），published_doi 为 arXiv 记录中的期刊 DOI，可能为空；openalex_id 为 null，citation_count 为 null，不伪造引用量。
- 批量收藏 source 为 arxiv，citation_count 可为 null。保存到既有 papers 表，无需迁移数据表。收藏后获取全文优先使用 arXiv。
- 已有期刊 DOI 或同一 arXiv 来源的文献会标记“已收藏”；标识冲突时禁止在页面选中。

编号依据：[arXiv 官方 DOI 说明](https://info.arxiv.org/help/doi.html)。

## 已接通：页面发起单篇 AI 分析

### 发起任务

`POST /api/v1/analyses`

```json
{"paper_id": 2, "max_chunks": 6}
```

max_chunks 范围为 2–50。论文不存在返回 404；没有全文、没有文本片段或模型配置无效返回 422。正常返回 `202 {"task_id":"...","reused":false}`；同一论文已有排队或执行中的分析时复用该任务，reused 为 true。

### 查询结果

- `GET /api/v1/tasks/{task_id}`：任务 type=paper_analysis，成功时 result_id 为分析编号；错误包含超时、连接/HTTP 错误、结构化输出、引用不匹配和原文版本变化。
- `GET /api/v1/analyses?paper_id=2`：按创建时间倒序返回分析结果数组。
- `GET /api/v1/analyses/{analysis_id}`：读取单份报告，不存在返回 404。
- AnalysisResult 保留既有 paper/document/analysis/evidence_index，另含 id、created_at、model_name、selected_evidence_ids、is_current。
- is_current=false 表示原文或所读片段已变，页面作为历史分析展示。文献库 has_analysis 筛选只计当前可追溯的成功报告。

API 复用原有分析函数、一次引用修正和严格原文校验。只对所选片段作分析；模型返回空响应、格式错误或引用核验失败时，不保存为可用报告。成功结果和任务完成状态在同一 SQLite 事务提交，重新分析保留历史。

本地引文恢复允许空白差异及一层常见 JSON 转义，保存值必须是对应源片段的精确连续子串。
合法编号引用错片段时，只在本批原文中有唯一匹配才纠正编号；指定片段可匹配时优先保留。
未知编号、歧义匹配、改写、合并断词和跨片段拼接仍被拒绝；恢复不改变提示词、Schema 和批次计划，不使已核验检查点失效。

论文与全文仍在 PostgreSQL；页面产生的分析历史和任务记录存放在 outputs/api_tasks.sqlite3。页面支持下载 JSON；原有 CLI 的 JSON 文件不会自动导入页面历史。结构或引用失败的详细诊断保存在 outputs/api_analysis_errors/{task_id}.json，仅用于服务端排查。

服务端配置沿用 LLM_MODEL、LLM_API_KEY、LLM_BASE_URL、LLM_READ_TIMEOUT、LLM_MAX_RETRIES、ANALYSIS_MAX_CHARS 和 STRUCTURED_OUTPUT_METHOD。任务仍适用于单进程本地服务；重启会标记未完成任务中断，不自动重新调用模型。

## 已接通：多篇论文比较与综述

### 比较矩阵（不调用模型）

`POST /api/v1/reviews/compare`

```json
{"paper_ids": [2, 3]}
```

返回 paper_ids、analysis_ids 和 matrix。每行包含 paper_id、title、doi、analysis_id、document_id、content_sha256、analysis。默认选择每篇论文最新且仍可追溯的分析；analysis_ids 固定矩阵预览使用的具体版本。

### 研究问题推荐

`POST /reviews/questions` 接受与比较矩阵相同的 `{paper_ids,analysis_ids?}`，推荐固定版本分析可共同讨论的最多三个问题。
前端在取得比较矩阵后自动发起，并显式传入矩阵的 `analysis_ids`；返回 `202 {task_id,reused}`。
任务类型 `review_questions`，通过 `/tasks/{id}` 查询；成功任务在 `question_suggestions` 中保存
`[{question,rationale,evidence_refs}]`，并保留 `paper_ids`、`analysis_ids`、模型、传输和时限。
每个问题须包含至少两篇输入论文的合法证据编号；推荐前后复核原文和分析快照。证据不足可返回空列表。
空白/重复问题、非法引用、结构错误、截断或期间原文变化均不发布推荐。
活动任务按固定分析版本、模型、提示词和输出结构去重；重启按通用任务恢复机制标记中断。
推荐本身不创建正式综述，也不自动选定主题。用户选择或编辑文字后，仍向 `POST /reviews` 提交 `topic` 和相同的分析版本。
页面可从任务中心 `/reviews?questionsTask=任务编号` 恢复，并重新核对对应分析；浏览器也保存对应版本的任务编号和编辑草稿。
切换论文或分析版本后不沿用旧推荐；推荐失败可重新推荐或自行填写。演示模式明确拒绝模型推荐请求。

### 发起综述

`POST /api/v1/reviews`

```json
{"paper_ids": [2, 3], "analysis_ids": ["analysis_版本一", "analysis_版本二"], "topic": "所选论文支持的研究主题"}
```

- 需要 2–5 篇不同论文，每篇均有当前有效且带证据结论的分析。
- analysis_ids 可省略；传入时必须按 paper_ids 顺序一一对应。前端会把矩阵预览返回的版本一起提交。
- topic 为 1–1000 字符，不能为空白。
- 返回 202、task_id、reused。相同论文、分析版本及主题已有执行中任务时复用；不同输入不混用任务。
- task.type=review_generation；任务包含 paper_ids、analysis_ids、topic，成功后的 result_id 指向综述。

### 结果与文件

新综述的 `writing_profile` 为 `detailed-v1`。`draft` 保留原五个分类，并新增 `synthesis`（围绕研究问题的综合回答）。
每个主题点保留 `claim`、`evidence_refs`，新增 `heading` 与 `paragraphs:[{text,evidence_refs}]`。
每段解释均执行合法引用、去重和非空检查；跨论文比较的主题句仍须引用至少两篇，报告整体仍须实际引用至少两篇。
段落中使用的引用也进入 Markdown 原文证据索引，网页按小标题、主题句、解释段落分别展示引用。
旧报告可能缺少新增字段，客户端继续兼容；不会改写已保存的旧报告或下载文件。
详细写作使用更完整、均衡且有 60000 字符预算的分析材料，每类最多八条，保留完整引文并标记实际材料覆盖。
API 模式下 `REVIEW_MAX_OUTPUT_TOKENS` 默认 10000，仅对综述生成生效；Codex 沿用 CLI 输出管理。

- `GET /api/v1/reviews/{review_id}`：返回 id、created_at、paper_ids、analysis_ids、topic、model_name、draft、matrix、evidence_index、is_current。不存在返回 404。
- `GET /api/v1/reviews/{review_id}/files/json`：下载包含综述、矩阵和版本信息的完整 JSON。
- `GET /api/v1/reviews/{review_id}/files/md`：下载 Markdown 综述，包含原文证据索引和分析版本。
- `GET /api/v1/reviews/{review_id}/files/csv`：下载比较矩阵，带 UTF-8 BOM 并防止 CSV 公式注入。

所有引用必须来自本次提供的 P编号:E编号；跨论文比较至少引用两篇，整份报告也必须实际引用至少两篇。模型结构错误、非法引用、原文版本变化或网络错误均不发布报告。失败诊断在 outputs/api_review_errors/{task_id}.json。

综述与下载内容、任务完成状态保存在 outputs/api_tasks.sqlite3 的同一事务中；失败不会覆盖历史成功报告。原文变化后的已生成综述仍可读取，is_current=false，页面标为历史报告。任务中心可以重新打开每次成功生成的综述。

仍使用单进程本地后台任务；模型配置复用服务端 LLM 配置。原有 run_literature_review.py 仍可独立运行。

## 当前单篇分析：分批核验、保存与继续

`POST /analyses` 新增：

```json
{"paper_id":23,"scope":"full","batch_size":2,"max_chunks":6,"force_restart":false}
```

- scope 默认 full，顺序读取全部片段；sample 才使用 max_chunks（范围仍为 2–50）。
- batch_size 默认 2，范围 1–10；每批字符预算用 ANALYSIS_BATCH_MAX_CHARS，默认 4000。不会截断或跳过放不下的片段。
- 每批先核验再写入检查点；额外尝试次数用 ANALYSIS_BATCH_RETRIES，默认 1，范围 0–3。SDK 自动重试关闭，避免叠加。
- 正常发起时可复用相同输入和配置的已完成批次；force_restart=true 创建全新的分批进度。
- 继续失败任务时传 resume_task_id，原文、模型或分组配置不同则返回 409；只调整超时或重试次数不会改变批次身份。
- `GET /analyses/progress/{paper_id}` 返回最近的分析任务或 null，用于刷新后恢复页面。
- `GET /analyses/tasks/{task_id}/batches` 返回每批状态、累计尝试次数及错误类型，不作为完成的报告发布。
- 任务新增 total_batches、completed_batches、current_batch、batch_attempt、resumed_batches、failed_batch、analysis_scope 等字段。AnalysisResult 新增 batch_summary。

### 段落与句子窗口分片

- 新解析使用 `pymupdf-native-sentence-v2`：默认每片上限 1200 字符，优先选择段落、句子边界；相邻窗口尽量复用不超过 200 字符的完整句子。
- PDF 不跨页；超长句和无标点内容回退到词/字符边界。片段允许有声明上限内的重叠，但必须持续向前、完整覆盖正文，并且每片都与全文的字符范围精确一致。
- 重叠不会降低 `quote` 核验要求。引用仍须属于对应 E 编号的原文子串；模型改写或错误归属的引文仍会失败。
- 分析前会核对可读取的旧版 `pymupdf` / `pymupdf-native-v1` 本地 PDF，并在相同原文件上重新分片；服务端返回 `source_reparsed=true`，页面提示已更新解析与分片。
- 仅改变分片时全文哈希可能不变，但片段布局和计划指纹会改变。已失败任务在这次自动迁移后可以建立新计划，旧检查点不能用于新切片；已有运行中任务仍直接复用。

全部批次完成后直接按五类合并结果，完全相同的结论合并证据；不额外调用模型做整篇总结。最终再次核验全部来源，才提交正式报告。检查点与结果在 outputs/api_tasks.sqlite3 中保存，服务重启不会删除已通过核验的批次。

ANALYSIS_MAX_CHARS 不再限制页面分析的整篇输入；之前 API 文档中的单次字符预算描述由本节取代。使用说明见[分批分析说明](../docs/BATCH_ANALYSIS_GUIDE.md)。

## 已接通：页面模型检测与连接诊断

- `GET /system/health` 新增 `model_http_backend` 与 `model_configuration_error`，只检查配置，不发送模型请求。
- `POST /system/model-check` 显式发起一次检测，返回 202 和 `{task_id,reused}`。请求字段：`mode` 为 `chat` 或 `structured`（默认），`http_backend` 可省略以使用当前配置，API 模式可选 `sdk` / `requests`；Codex 模式仅接受省略或 `codex`，不允许检测参数切换计费方式。`timeout` 为 1–120 秒，默认 60；API 为读取超时，Codex 为单次调用总时限。
- 检测不访问 PostgreSQL。结构化检测复用 CLI 与论文分析的五类结构、原文引用核验及两个内置样例；普通对话只发送一句简短问题。SDK 自动重试关闭。
- 任务类型为 `model_diagnostic`，通过已有 `/tasks/{id}` 查询状态。任务包含 `check_mode`、`model_name`、`http_backend`、`read_timeout`。
- 成功后 `model_check_result` 包含 `seconds`、`response_received`、`structure_validated`、`evidence_validated`；结构检查另外返回 `findings` 和 `quotes` 数量，不返回模型回复正文。
- 失败时 `error` 包含代码、提示和安全的 `diagnostic`；连接诊断可含目标域名、异常类型链与实际 TCP/TLS 阶段，不含密钥、请求头或异常原文。论文分析及综述的连接失败也会返回这一诊断字段。
- `GET /system/model-check?mode=structured&http_backend=requests` 返回对应模式、传输和当前模型配置的最近检测任务，或 null。省略传输时使用当前配置；模型、密钥、地址或输出配置改变后，不会把旧成功记录作为当前结果返回。
- 同一输入的进行中检测会复用任务。服务重启后未完成检测标为 `SERVICE_RESTARTED`，不会伪装成完成。任务中心通过 `/settings?modelCheck=任务编号` 重新打开历史检测。
- 请求不接受浏览器提供的密钥、模型地址或其他额外字段；选择传输仅影响这次检测，不改写 `.env`。

### 本机 Codex 模式

服务端 `LLM_PROVIDER=codex` 时，分析、综述和检测调用官方 Codex CLI，复用同一系统用户的 ChatGPT 登录。
此模式不要求 API Key 或 `LLM_MODEL`，`CODEX_CLI_PATH` 指定 CLI，`CODEX_MODEL` 可选，留空使用 CLI 内置默认模型。
健康检查仅核对配置和可执行文件，不调用 Codex；登录与账号额度须通过显式检测验证。
`http_backend` 显示 `codex`，`model_name` 显示所指定模型或 `Codex（CLI 默认模型）`；任务的 `read_timeout` 在此模式表示总调用时限。
`STRUCTURED_OUTPUT_METHOD` 的两种现有取值均映射为 CLI `--output-schema`，仍执行相同的结构和原文/跨论文引用核验。
安全错误码包括 `CODEX_AUTH_REQUIRED`、`MODEL_QUOTA_EXCEEDED`、`CODEX_PERMISSION_DENIED`、`CODEX_EXEC_FAILED`、
`CODEX_EMPTY_RESPONSE`、`MODEL_CONNECTION_ERROR` 和 `MODEL_TIMEOUT`。错误不包含 CLI 异常原文或令牌。
`CODEX_MODEL_UNAVAILABLE` 表示模型标识不受支持或账号无权使用；GPT 模型标识会统一为小写。
Codex 模式默认读取系统代理，`CODEX_USE_SYSTEM_PROXY=false` 可关闭；检测请求不能覆盖服务端代理设置。
CLI JSON 失败事件提供安全的 `reason`、`phase`、`host`、`http_status`、`cli_exit_code`、`system_proxy_enabled` 和
`environment_proxy_names`（仅变量名称）；缺少底层信息时明确显示原因未知，不把所有错误归为本机网络故障。
账号登录和额度错误不自动重试；可重试连接/超时错误仍由现有批次重试机制控制，已完成批次保留。
切换 API/Codex 或更改 Codex 模型会改变计划指纹；旧检查点不会混入新分析。

## 已接通：研究目标理解与计划草拟

- `POST /agent/plans`：请求 `{goal}`，去除两端空白后至少2字符，最多6000字符；返回202 `{task_id,reused}`。
- 任务类型 `research_planning`，元数据含 `research_goal`、模型、传输、时限。通过 `/tasks/{id}` 轮询；成功后 `result_id` 指向计划。
- `GET /agent/plans/{id}`：返回 `{id,goal,plan,revision,status,planning_date,created_at,updated_at,model_name,source_task_id,capabilities}`；状态为 `draft`。
- `PUT /agent/plans/{id}`：请求 `{plan,expected_revision}`；重新校验后保存，版本号加一。版本不一致返回409，不覆盖其他页面的编辑；无效计划返回422。

`plan` 包含主题、研究目的、研究问题、`scope`、检索词、比较维度、交付物、假设、待澄清问题及步骤。
`scope` 保留起止年份、年份依据与原话摘录、建议本轮篇数（2–5）、用户原始篇数及摘录、语言、指定来源和纳入/排除条件。
明确年份与原始篇数须对应用户原话；推测时间范围标为 `assumed` 并说明，用户修改年份后标为 `edited`。
超出本轮篇数限制或要求其他来源时，保留原需求并提示能力差异，不能静默改写。
每步包含连续编号、白名单动作、名称、目的、计划输入、预期产物和前序依赖；拒绝循环、前向依赖、重复动作和与交付物不匹配的步骤。

规划 LangGraph 含“理解目标并草拟计划”和“验证计划”节点，通过一次结构化模型请求完成规划。
生成计划接口本身不执行论文工具、不访问论文 PostgreSQL；用户确认保存后，可另行调用下节的检索与初筛接口。
计划和任务完成状态在同一 SQLite 事务发布；同日相同目标、模型与规划配置的活动任务去重，服务重启时标记中断。

## 已接通：按计划检索与摘要初筛

- `POST /agent/plans/{id}/search`：`{expected_revision,per_query?:10}`，返回202 `{task_id,run_id,reused}`。每词1–20条，使用计划中全部检索词（最多8个），仅第一页，相关性排序，按 arXiv 首次提交年份过滤。
- 计划必须包含先检索后筛选步骤；不支持的先决动作或非 arXiv 指定来源返回422；版本变化/配置不同的同版本活动任务返回409。不会静默替换指定来源。
- `GET /agent/plans/{id}/search?run_id=...`：返回 `{run,history}`，不带 run_id 时返回当前保存版本的最新记录或null；历史摘要包含编号、版本、状态、模型、预算和创建时间。其他计划的 run_id 返回404。
- `run` 包含原目标与完整计划快照、revision、每词查询状态/total/items、候选论文（`candidate_id`,`query_numbers`）、去重/跳过计数、按候选编号索引的 `screenings`、任务状态及错误。候选具有既有 `SearchPaper` 字段，可用 `/papers/batch` 收藏。
- 任务类型 `research_search`，含 `plan_id,plan_revision,search_run_id`，成功时 result_id 为 run_id；任务中心可进入该计划的对应检索记录。
- LangGraph 顺序执行 `search_and_deduplicate → screen_and_verify`。复用现有 arXiv 客户端和共享请求节流；按基础 arXiv 编号/正式 DOI 去重，不凭相似标题合并。
- 初筛每批最多5篇，根据条件数量降低批量；当前模型使用结构化 `ScreeningBatch`，每篇逐一返回所有条件的 `meets/fails/unknown` 及解释。引用必须来自对应论文实际发送的标题或摘要，未知条件无引文。拒绝未知/重复编号、遗漏条件、错配或编造引文、截断和拒绝响应，整批不发布。
- 后端根据纳入/排除条件和年份计算 `included/excluded/uncertain`，不能由模型覆盖；未提供摘要或元数据不足保留待确认，明确不符合条件可排除。标题最多1500字符、摘要最多12000字符进入模型，截断会标注；完整检索摘要仍保留。
- 每个检索词成功、每批初筛核验成功后持久化到任务 SQLite 的 `research_search_runs`。同计划版本、模型/端点、提示词/schema、方法、预算对应独立指纹；相同配置的成功运行复用，失败运行创建新任务继续同一运行，已保存部分跳过。修改计划版本、模型或预算建立新运行，不混用检查点。
- 排队任务与运行记录原子创建，最终结果与成功状态原子发布；通用重启恢复使任务变为失败，检查点保留，读取时以任务状态为准。旧任务不能向已替换或结束的运行发布检查点。
- 页面编辑未保存/目标不一致时禁用开始；支持分类查看、历史记录、刷新、继续和手动收藏（每次最多100篇）。演示模式不发起或伪造检索初筛。

本阶段不自动执行计划后续全文获取、分析或报告步骤。摘要初筛与后续原文 E 编号证据核验分开；本轮结果有来源、分页与预算限制，不能宣称完成全面查新。
规划沿用 Codex/API 两种模式，API 输出预算由 `RESEARCH_PLAN_MAX_OUTPUT_TOKENS` 控制，默认6000。
网页“科研 Agent”提供规划入口和草稿编辑；任务中心可用 `/agent?planId=...` 或 `/agent?planningTask=...` 恢复。
演示模式不生成、伪造或保存模型规划结果。结构、约束、模型连接、额度和存储错误分别显示。


## 已接通：CVPR 单会议扩展

- `GET /venues`：当前目录仅 CVPR，ranking=null，目录等级待导入。没有接入其他会议或通用 OpenAlex/DBLP/Semantic Scholar 搜索。
- `GET /venues/cvpr/papers`：query（1–300字符）、start_year/end_year、page、page_size（默认5，上限20）、sort（relevance/newest/cited）、verified_main_only（默认false）。返回原 PageResult 结构，加 notice、source_ids、skipped_count、publication_counts。total 是上游索引候选总数；官方主会过滤在当前页执行，不是全局核验后的总数。
- 候选使用 source=openalex、venue_slug=cvpr、openalex_id、doi、abstract_source、venue、publication；arxiv_id=null。publication 含 track（main/workshop/unknown）、verification_status（indexed_unverified/official_verified）、verification_url、verified_at、reason。identity_verified 只表示标识一致性，不能表示官方主会身份。
- OpenAlex Source ID 来自真实来源查询或经核对的环境配置，名称与 conference 类型必须匹配；不把占位 S 编号写入生产配置。年份筛选是正式发表年份；CVF 年度路径必须与候选年份一致。
- 新增 PostgreSQL 表 venues、venue_identifiers、venue_rankings、paper_publications；现有论文记录不修改结构。CVPR 目录及真实 OpenAlex 映射在使用时保存；CCF 目录尚未导入，不显示假定等级。
- `POST /papers/batch` 扩展接受 source=openalex、venue_slug=cvpr。服务端忽略客户端核验声明，在收藏时重新核对官方身份并存储发表关系；重复收藏复用论文，临时网络故障不降级官方已确认记录。
- `GET /papers/{id}/publication` 返回 `{items:[{venue,year,track,verification_status,verification_url,verified_at,reason}]}`，由论文详情展示。主会和 Workshop 分开存储。
- 计划 scope 增加可选 venue_slug（null/cvpr，旧计划默认null）、verified_main_only（默认false）。CVPR 允许 requested_sources 为空或包含 openalex/cvf/openalex_cvpr；未指定 CVPR 时仍执行 arXiv。页面切换范围后需保存新版本。
- 计划检索沿用原 `/agent/plans/{id}/search`。CVPR 运行记录含 venue_slug、source、verified_main_only，使用独立指纹；按 OpenAlex ID/DOI 去重，发表元数据随候选保存。arXiv 指纹格式保持，旧检查点不因接入 CVPR 而失效。
- POST 可选 `refresh:true`（仅 CVPR）显式重新检索并核验，创建新的运行记录，保留旧快照；同版本已有活动任务仍去重。普通重复请求继续复用成功结果或恢复失败检查点。
- `GET /papers/search` 仍为原 arXiv 接口。手动 CVPR 页面与计划执行调用相同服务，不互相通过 HTTP 调用；演示模式拒绝 CVPR 联网与伪造核验结果。
- 访问明确 CVF 论文来源的全文获取任务会先核对 CVF 标题/作者/DOI，再取其官方 PDF；失败后复用既有开放全文来源，PDF 安全验证与解析流程保持。

HTTP 504 表示候选来源超时，502 表示上游连接/映射/协议问题，503 表示数据库或缓存存储故障，422 表示条件无效。上游错误不返回含 Key 的请求地址或异常原文。


## 已接通：摘要中文翻译

- `POST /translations/abstract`：请求 `{title?:string,abstract:string}`；title最多2000字符，abstract为非空原文、最多24000字符，保留原始空白与换行。禁止客户端传入密钥、模型地址或额外字段。返回202 `{task_id,translation_id,reused}`。
- `POST /translations/abstract/lookup`：相同请求体，仅读取当前输入/模型对应缓存，返回 `{record,task}` 或null，不调用模型。
- `GET /translations/abstract/{id}`：读取指定记录，返回 `{record,task}`，不存在404。record包含 id、title、original_abstract、source_sha256、translation_zh（待完成时null）、model_name、task_id、created_at、completed_at。
- 翻译在后台运行，任务类型 `abstract_translation`，带 translation_id、paper_title、模型/连接方式/时限。原文与中文译文独立存储在同一 SQLite 的 abstract_translations 表，完成时与任务成功状态原子发布。
- 缓存键绑定标题、逐字原文、提示词/Schema/版本、模型/端点与结构化方法；相同活动任务或成功结果复用，失败后创建新任务重试，旧任务不能覆盖新结果。正文或模型变化不会复用旧译文。
- 当前 Codex/API 连接配置沿用现有模型运行时，API 输出预算8000 Token；Codex沿用CLI输出管理。拒绝空白/非中文输出、无效结构、截断、拒绝和过滤响应，失败时不保存部分译文，不返回异常原文或密钥。
- 页面挂载仅查询缓存；点击后才调用模型。后台任务完成自动显示译文，可从任务中心 `/search?translationId=...` 重新打开。切换论文/摘要时清除旧状态并阻止迟到响应覆盖当前摘要。
- arXiv/CVPR 页面及计划候选展示同一翻译组件；演示模式不调用或伪造翻译。模型翻译仅辅助阅读，引用仍以原文为准。

本功能不修改候选的 abstract、原文证据、发表核验状态、文献库摘要或分析检查点。


## 顶刊顶会统一入口：CVPR、ICCV、AAAI、IJCAI

- `GET /venues` 返回13个已支持会议/期刊（详见 README 的扩展目录），前端以同一页面的多选框展示名称和官方网站。
- 新增 `GET /venues/papers?venues=cvpr,iccv&query=...`：venues为逗号分隔的非空、不重复支持范围，其他年份/排序/分页/主会过滤参数与原接口一致。空值、未知或重复会议返回422；一场已选择会议来源失败时明确报错，不静默只返回另一场。
- 原 `/venues/cvpr/papers` 保留，`/venues/{slug}/papers` 支持目录内所有场所的单独调用；不另增独立前端页面。新前端使用统一接口。
- 联合检索先解析各会议 Source ID，再用同一个 OpenAlex works 查询 OR 过滤，保证同一索引集合排序和分页。每个候选的 venue_slug/venue 表示实际来源；映射冲突停止查询，不能将一个来源 ID 默认为两场会议。
- CVF 核验严格核对会议路径、年份与论文身份，覆盖现代和旧版 CVPR/ICCV 路径。ICCVW 下 Findings 单独标为 findings，不能通过主会过滤。publication_counts 增加 findings；PublicationInfo.track 扩展支持此值。
- 同页同会议候选按 DOI/OpenAlex ID 去重；不同会议的发表记录保持独立，不用一场会议的官方证明替代另一场。
- `/papers/batch` 的 venue_slug 扩展为 cvpr/iccv/aaai/ijcai；后端按实际会议重新核验和保存关系；`GET /papers/{id}/publication` 根据记录真实 venue_id 返回会议名称，避免统一误标 CVPR。
- 计划 scope 的旧 venue_slug 支持 cvpr/iccv/aaai/ijcai；venue_slugs（默认[]、最多13个、不可重复）表达联合范围。旧单会议字段继续兼容；两字段有冲突拒绝保存。联合计划的候选编号按会议区分，同论文标识的不同发表记录不互相覆盖。
- 原 CVPR 与 arXiv 执行指纹格式保留，ICCV/联合范围有独立执行指纹；原有成功记录与检查点不迁移、不混用。摘要翻译、初筛、收藏、重试与历史记录仍使用现有机制。
- 明确的 ICCV 官方页面全文获取同样优先使用 CVF 的、经身份核验的 PDF；失败继续原有来源，不改变 PDF 验证或解析规则。

- AAAI 使用 OJS `/index.php/AAAI/article/view/{id}` 元数据及卷期标签；IJCAI 使用年度 `/proceedings/{year}/{id}` 本文 BibTeX/目录 Track。精确核对标题、作者、年份和可取得的 DOI；非主会类别标为 other（Survey/Demo/Special Track 等），未知类别为 unknown。只有 official_verified + main 可通过主会过滤。
- publication 增加 verification_provider（CVF/AAAI Proceedings/IJCAI Proceedings），publication_counts 增加 other；不把所有官方核验统一标成 CVF。官方访问校验或未知格式不会升级发表身份，拦截页面不缓存。
- AAAI 来源允许 conference/journal 类型，但名称须对应 AAAI 主会议，不包含 Spring Symposium、AIIDE 等独立会议。可选 `OPENALEX_AAAI_SOURCE_IDS`、`OPENALEX_IJCAI_SOURCE_IDS` 仍核对身份；联合来源 OR 上限100个，超出须显式报错。
- AAAI/IJCAI 计划接受 openalex/openalex_venues/openalex_{slug} 和对应 aaai_proceedings/ijcai_proceedings；CVF 仅适用于 CVPR/ICCV。新会议使用额外核验版本指纹，旧 CVPR/ICCV/arXiv 运行格式保持。
- 明确 AAAI/IJCAI 官方页面的全文获取，优先采用已核对身份且由官方页提供的 PDF 链接；不猜测 PDF 地址，失败沿用其他开放来源回退与下载进度诊断。

API 仍以索引候选总数分页，主会核验过滤只作用于当前候选页；不会将索引命中视为官方主会录用。CCF 等级本轮继续保持未导入。


## PDF 下载时限、去重与诊断

- `/papers/{id}/documents/fetch` 地址不变，202响应新增 reused。同一论文的活动全文获取任务按独立 dedup_key 复用，不与上传解析任务混用。
- 新环境参数 PDF_DOWNLOAD_TIMEOUT=600（每地址总时限）、PDF_READ_TIMEOUT=60（等待数据）、PDF_CONNECT_TIMEOUT=10（连接），最大分别3600/600/120秒，必须为有限正数，创建任务前验证，错误422。模型时限与PDF下载无关。
- 流式传输每16KB检查大小/总预算，约每2秒更新一次进度；请求使用连接/读取时限，重定向仍逐跳检查公共HTTPS地址。20MB限制和有效PDF解析规则保留。非PDF文件头及早拒绝，不等待整个错误页面。
- task.download_progress 含 provider、host、phase、received_bytes、total_bytes（未知为null）、elapsed_seconds、average_bytes_per_second、total_timeout、read_timeout、connect_timeout。
- PDF_DOWNLOAD_TIMEOUT、PDF_READ_TIMEOUT、PDF_CONNECT_TIMEOUT 分别代表总时限、等待响应数据、建立连接超时；error.diagnostic 与 source_results[].diagnostic、download_attempts 保存安全的传输信息，只有主机名，不返回完整请求URL、查询参数、密钥或异常原文。普通网络失败继续保留安全的HTTP状态和异常类型链。
- 同一获取任务会记住已尝试的PDF地址和重定向地址，其他来源返回重复地址时标记 skipped，不重复等待。失败临时文件删除，成功原文继续保存哈希并解析。
- 页面将PDF诊断与模型连接诊断分别显示；旧任务缺少新字段仍正常读取，不能从旧错误文本反推当时的速度和下载主机。


PDF 下载诊断现增加三层证据：candidate_found（仅有候选地址）、response_received/http_status/content_type（收到HTTP响应）、pdf_header_verified（已从实际响应字节确认 `%PDF-` 文件头）。Content-Type 为 application/pdf 不足以确认PDF；文件头确认也不表示完整文件已下载或解析成功。
source_results 保留 lookup_status（found/unavailable/failed）及 candidate_count，下载失败后仍可看出是否取得过候选地址。页面分别显示未取得地址、HTTP响应但未确认PDF、确认文件头但未下载完；旧任务缺少这些字段时明确保持未知，不反推文件已找到。


## 九个新增会议/期刊

`VenueSlug` 扩展为 `acl/emnlp/sigir/neurips/artificial-intelligence/tois/eccv/icml/iclr` 及已有四项。`GET /venues` 同时提供 venue_type；两个期刊为 journal，其他为 conference。`/papers/batch`、计划 scope、手动检索均使用相同目录。

`PublicationInfo.track` 增加 journal，`publication_counts.journal` 表示本页已正式核验的期刊数量。保留旧参数 `verified_main_only`：通过条件是 official_verified 且 track 为 main 或 journal。期刊显示“确认期刊发表”。Crossref 出版记录链接保持明确，不冒充 publisher 页面；候选 URL 保留正式 DOI 链接。

核验适配见 `tools/venue_search/extended.py`。ACL Anthology、PMLR、ECVA、NeurIPS/ICLR proceedings 和 OpenReview API 采用严格来源/身份规则；Crossref 仅用于出版方 DOI 元数据核验，期刊需额外核对名称、ISSN 和文献类型。未录用 OpenReview 投稿、其他会议的 PMLR 卷和身份不一致数据不得升级。

研究计划能力增加 venue_platforms，requested_sources 最多16项；所选场所最多13项，平台取所选场所并集。新场所执行指纹额外含核验版本；旧四会议/arXiv 指纹计算不变。未知或重复范围拒绝，已选来源故障不静默忽略。

全文获取沿用原下载/验证/解析与诊断；新增正式来源解析只返回该论文页面明确提供的匹配 PDF，网络受限时沿用开放来源回退。


## 计划选文、全文与分析执行

- `POST /agent/plans/{plan_id}/executions`，返回202：`{expected_revision, search_run_id, candidate_ids:[2–5个不同编号]}`。只接受当前计划版本已完成检索/初筛的候选，且 identity_verified=true。额外字段拒绝；客户端无法提交正文或替换候选元数据。人工可选择 included/uncertain/excluded，原筛选结果随执行快照保存。
- 计划须按顺序包含 acquire_fulltext、analyze_papers；分析之前不能存在尚未执行的工具动作。检索已执行，人工选择负责本轮选文。
- 返回 `{task_id, execution_id, reused}`。SQLite `research_executions` 保存原计划、版本、目标、检索记录、选文及候选/初筛快照、计划步骤编号、子任务/全文/分析关联。任务类型 research_execution。每计划活动执行去重，执行中换选文返回409；相同完成记录可直接复用。
- `GET /agent/plans/{plan_id}/executions?search_run_id=...` 返回 `{items:[...]}`，按新到旧排列；每项含 papers，每篇有 fulltext、analysis 步骤（status、task_id、完整 task 进度、document_id/content_sha256 或 analysis_id、reused、error）。父状态以通用任务表为准，重启中断可见。只读请求不会启动执行。
- `POST /agent/plans/{plan_id}/executions/{execution_id}/resume` 返回202，只继续该固定快照；可继续旧版本，不切换到新计划。外部活动任务只关联，不重复运行。原文失效会重新核查；当前有效全量分析复用，sample/过期分析不算完成。
- 收藏、全文获取与分析使用原有服务函数和证据核验；子任务关联先保存，再运行后台动作。主任务每篇逐步保存，一篇失败继续其他篇；失败保留安全错误码/诊断。分析重试遵循原接口默认指纹恢复已完成批次。
- 完成结果代表本轮全文/分析闭环，比较、综述及其他步骤未执行；规划能力仍声明不支持整个计划自动执行（execution_available=false），executable_actions 增加 acquire_fulltext、analyze_papers。


## 正文关键章节快速阅读

`POST /analyses` 的 scope 增加 fast；fast_max_chunks 默认18，范围8–50，原 max_chunks 仍用于 sample，full 行为不变。
快速模式采用既有文档的原始窗口，按章节优先选择，不重新解析/切片，也不重写正文。任务与结果增加 reading_selection：scope/policy_version/budget/strategy/selected_chunks/total_chunks/selected_pages/sections/missing_sections/note。
sections 每项含 category、label、available_chunks、selected_chunks；跨章节窗口可计入多类。未识别标题时 strategy=body_fallback 或 missing_sections 明确提示；不能声明已读全文。
快速模式的指纹额外包含选片策略版本和实际覆盖信息，full/sample 原指纹计算不变；不同模式或快速预算的活动任务返回409，不静默复用。快速任务可在相同策略、原文和配置下恢复。
`POST /agent/plans/{id}/executions` 增加 analysis_scope=full|fast，缺省full；新快速执行使用独立 selection_key，固定18片段。结果含实际复用分析的 scope 与 reading_selection。已有有效全量分析可满足快速执行，fast/sample 不能满足 full。


## 本轮计划比较与报告

- `GET /agent/plans/{plan_id}/executions/{execution_id}/comparison`：仅允许本轮分析成功且计划包含比较/综述步骤；用执行快照内固定 paper_ids/analysis_ids 调用已有比较核验，返回矩阵与计划/覆盖上下文。不使用浏览器提交或最新分析替代本轮版本。
- `POST .../reports`：`{research_question,comparison_dimensions}`，问题1–1000字符，维度1–10个、不重复、每项1–150字符；额外字段拒绝。返回202 `{task_id,report_id,reused}`。同一执行的活动报告去重，运行中修改输入409；成功的相同输入复用，失败输入重试同一记录。
- `GET .../reports`：返回报告历史，含固定选择、计划版本、确认的问题/维度、比较结果、父任务和综述子任务、review_id、错误及阅读覆盖。状态以任务表为准，服务重启中断可见。
- SQLite新增 plan_reports 表。父任务类型 plan_report，任务中心可返回对应计划。比较在模型前保存；报告使用原有综述生成和跨论文证据核验，最终生成前/发布前检查原文。失败不发布 review_id，不重跑单篇分析。
- 综述 payload 新增可选 research_context，含 plan_id/plan_revision/execution_id/goal/comparison_dimensions/reading_coverage；Markdown 附同样的计划与覆盖说明。独立综述原接口保持兼容。
- 导出继续使用 `/reviews/{review_id}/files/md|csv|json`，下载时仍核对报告当前原文状态，并保留历史结果标记。报告完成是“可导出”，不表示浏览器已保存文件。
