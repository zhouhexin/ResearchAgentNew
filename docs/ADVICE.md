# ResearchPilot 项目代码审查：是否偏离最初目标？

我已经打开并检查了你的 GitHub 仓库：

[zhouhexin/ResearchAgentNew](https://github.com/zhouhexin/ResearchAgentNew?utm_source=chatgpt.com)

这次审查以仓库 `main` 分支的实际代码为依据，对照我们最初确定的目标：

> 构建一个能够自主规划、检索、阅读、分析、比较学术论文，并生成具有真实文献证据支持的研究报告的 AI 科研 Agent。

## 一、总体判断

目前没有发生根本性的方向偏离。相反，你已经从最初的论文检索与分析工具，推进到了能够执行多阶段研究流程的系统。

我认为现在最值得关注的不是继续增加功能数量，而是三个问题：

1. 研究自主性：Agent 能否根据中途发现的信息调整研究计划，而不只是按一次性计划执行？
2. 科研可靠性：能否从“引用确实存在”提升到“引用确实支持结论”，同时保证检索覆盖范围合理？
3. 系统可维护性：功能越来越多，是否有统一的数据模型、任务执行机制和质量评测体系？

我也发现了一些需要优先处理的具体问题，下面会区分已实现的能力、尚不完整的能力和建议调整的方向。

## 二、对照最初的六项目标

| 原始目标       | 仓库中的对应实现                                              | 初步判断            |
| ---------- | ----------------------------------------------------- | --------------- |
| 自主文献检索     | `research_planner.py`、`research_search.py`、Venue 检索模块 | 已有较完整的分阶段流程     |
| 论文全文阅读     | PDF 获取、解析、快速阅读、分批分析                                   | 已实现核心功能         |
| 科研知识管理     | PostgreSQL、论文去重、全文和证据存储                               | 已实现核心功能         |
| 单篇论文智能分析   | Pydantic、分批分析、证据校验                                    | 已实现且有所增强        |
| 多论文比较与综述   | `review/`、比较矩阵、综述报告                                   | 已实现主要流程         |
| Agent 自主决策 | LangGraph 工具调用、研究规划及执行                                | 部分达到，仍可增强自主反馈循环 |

仓库中还有相应的 Vue、FastAPI、任务管理和测试代码，因此项目已经不只是命令行原型。

这里的“已实现”指我确认了对应实现代码，并不等于我已经在你的 PostgreSQL、模型 API 和浏览器环境中验证所有流程成功运行。

## 三、代码中做得比较好的地方

我认为有四项设计值得保留。

1\. 研究流程已经开始真正串联起来

你增加了研究目标解析、计划保存、检索初筛、选文、全文获取、单篇分析、比较和综述生成。

其中 [agent/research_planner.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/agent/research_planner.py?utm_source=chatgpt.com) 使用 LangGraph 构建研究规划，[api/research_execution.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/api/research_execution.py?utm_source=chatgpt.com) 负责执行已确认的论文研究任务。

这比单纯让模型调用搜索工具更接近最初的 Research Agent 目标。

2\. 证据溯源没有被牺牲

[agent/analysis_validator.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/agent/analysis_validator.py?utm_source=chatgpt.com) 明确检查证据编号和逐字引用；[review/db_guard.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/review/db_guard.py?utm_source=chatgpt.com) 进一步核对论文、文档哈希、Chunk 归属和字符位置。

你还增加了 PDF 断词、换行情况下的候选引文修正，并没有简单放宽为模糊匹配。

这是 ResearchPilot 最重要的技术基础之一，应该持续保留。

3\. 已经考虑长论文与失败恢复

目前存在分批分析、检查点、失败重试、快速阅读和全文版本检查。

相关实现包括 [agent/batch_analysis.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/agent/batch_analysis.py?utm_source=chatgpt.com) 和 [agent/fast_reading.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/agent/fast_reading.py?utm_source=chatgpt.com)。

这比一次性将整篇 PDF 放进模型上下文更适合真实的科研应用。

4\. 顶刊顶会检索方向正确

你已经实现了多个会议、期刊的候选检索和官方来源核验，并区分主会、Workshop 和其他 Track。

[tools/venue_search/catalog.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/tools/venue_search/catalog.py?utm_source=chatgpt.com) 维护了目前支持的 13 个 Venue。

这种“先发现候选，再核验发表状态”的设计值得保留。

## 四、我发现的七个值得调整的问题

这里按对最初目标的影响排序，而不是单纯按代码风格排序。

### 1. 文献发现能力开始偏向 arXiv

优先处理

在 [agent/research_tools.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/agent/research_tools.py?utm_source=chatgpt.com) 中，`search_academic_papers()` 目前主要调用 `search_arxiv()`。

README 也明确说明：普通文献检索已经从 OpenAlex 切换为 arXiv，而顶刊顶会检索则通过 OpenAlex 和官方来源进行补充。

使用 arXiv 没有问题，但不建议让它成为唯一的通用学术发现入口。

原因是我们最初希望 ResearchPilot 能够发现高质量的、正式发表的科研成果，而不仅仅是预印本。

尤其是你现在重点关注 AI、ML、CV 和 NLP，可能出现：

- 一篇论文已发表于顶会，但通用搜索没有覆盖到。
- 系统没有充分利用引用关系发现相关研究。
- 对同一研究领域的检索覆盖率无法评估。

建议改成统一的 `AcademicSearchService`：

```
AcademicSearchService
├── ArxivAdapter
├── OpenAlexAdapter
├── DBLPAdapter
├── SemanticScholarAdapter
└── VenueProceedingsAdapter
```

各适配器负责发现候选，统一服务负责去重、排序、来源记录及正式发表核验。

这不是推翻现有检索模块，而是把各个独立入口统一起来。

### 2. Agent 已经能执行计划，但还缺少研究反馈循环

优先处理

你目前的研究规划和执行流程比较清晰：

`制定计划 → 检索与初筛 → 人工选文 → 全文分析 → 比较 → 综述`

但从 [agent/research_search.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/agent/research_search.py?utm_source=chatgpt.com) 和 [api/research_execution.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/api/research_execution.py?utm_source=chatgpt.com) 的流程来看，主要还是按既定阶段向前执行。

还没有形成一个完整的“发现证据不足后，重新调整检索方案”的研究循环。

例如：

用户要求比较 LLM Agent 的规划方法。系统初步找到五篇论文，但其中只有两篇讨论了规划，其余主要讨论工具调用。

理想情况下，Agent 应该能够发现覆盖不足，进一步搜索 Planning、Task Decomposition 或 Agent Reasoning 相关研究，再决定是否需要补充材料。

我建议增加一个 `ResearchEvaluator` 节点：

\#chatgpt-mermaid-\_r_165\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(237, 237, 237);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_165\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_165\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_165\_ .error-icon{fill:rgb(48, 48, 48);}#chatgpt-mermaid-\_r_165\_ .error-text{fill:rgb(237, 237, 237);stroke:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_165\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_165\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_165\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_165\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_165\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_165\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_165\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_165\_ .marker.cross{stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_165\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_165\_ p{margin:0;}#chatgpt-mermaid-\_r_165\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_165\_ .cluster-label text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_165\_ .cluster-label span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_165\_ .cluster-label span p{background-color:transparent;}#chatgpt-mermaid-\_r_165\_ .label text,#chatgpt-mermaid-\_r_165\_ span{fill:rgb(237, 237, 237);color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_165\_ .node rect,#chatgpt-mermaid-\_r_165\_ .node circle,#chatgpt-mermaid-\_r_165\_ .node ellipse,#chatgpt-mermaid-\_r_165\_ .node polygon,#chatgpt-mermaid-\_r_165\_ .node path{fill:rgb(9, 23, 44);stroke:rgb(31, 78, 148);stroke-width:1px;}#chatgpt-mermaid-\_r_165\_ .rough-node .label text,#chatgpt-mermaid-\_r_165\_ .node .label text,#chatgpt-mermaid-\_r_165\_ .image-shape .label,#chatgpt-mermaid-\_r_165\_ .icon-shape .label{text-anchor:middle;}#chatgpt-mermaid-\_r_165\_ .node .katex path{fill:#000;stroke:#000;stroke-width:1px;}#chatgpt-mermaid-\_r_165\_ .rough-node .label,#chatgpt-mermaid-\_r_165\_ .node .label,#chatgpt-mermaid-\_r_165\_ .image-shape .label,#chatgpt-mermaid-\_r_165\_ .icon-shape .label{text-align:center;}#chatgpt-mermaid-\_r_165\_ .node.clickable{cursor:pointer;}#chatgpt-mermaid-\_r_165\_ .root .anchor path{fill:rgb(175, 175, 175)!important;stroke-width:0;stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_165\_ .arrowheadPath{fill:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_165\_ .edgePath .path{stroke:rgb(175, 175, 175);stroke-width:1px;}#chatgpt-mermaid-\_r_165\_ .flowchart-link{stroke:rgb(175, 175, 175);fill:none;}#chatgpt-mermaid-\_r_165\_ .edgeLabel{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_165\_ .edgeLabel p{background-color:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_165\_ .edgeLabel rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_165\_ .labelBkg{background-color:rgba(0, 0, 0, 0.5);}#chatgpt-mermaid-\_r_165\_ .cluster rect{fill:rgb(48, 48, 48);stroke:rgba(255, 255, 255, 0.15);stroke-width:1px;}#chatgpt-mermaid-\_r_165\_ .cluster text{fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_165\_ .cluster span{color:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_165\_ div.mermaidTooltip{position:absolute;text-align:center;max-width:200px;padding:2px;font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:12px;background:rgb(48, 48, 48);border:1px solid rgba(255, 255, 255, 0.15);border-radius:2px;pointer-events:none;z-index:100;}#chatgpt-mermaid-\_r_165\_ .flowchartTitleText{text-anchor:middle;font-size:18px;fill:rgb(237, 237, 237);}#chatgpt-mermaid-\_r_165\_ rect.text{fill:none;stroke-width:0;}#chatgpt-mermaid-\_r_165\_ .icon-shape,#chatgpt-mermaid-\_r_165\_ .image-shape{background-color:rgb(0, 0, 0);text-align:center;}#chatgpt-mermaid-\_r_165\_ .icon-shape p,#chatgpt-mermaid-\_r_165\_ .image-shape p{background-color:rgb(0, 0, 0);padding:2px;}#chatgpt-mermaid-\_r_165\_ .icon-shape .label rect,#chatgpt-mermaid-\_r_165\_ .image-shape .label rect{opacity:0.5;background-color:rgb(0, 0, 0);fill:rgb(0, 0, 0);}#chatgpt-mermaid-\_r_165\_ .label-icon{display:inline-block;height:1em;overflow:visible;vertical-align:-0.125em;}#chatgpt-mermaid-\_r_165\_ .node .label-icon path{fill:currentColor;stroke:revert;stroke-width:revert;}#chatgpt-mermaid-\_r_165\_ .node .neo-node{stroke:rgb(31, 78, 148);}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_165\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_165\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_165\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_165\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].node .neo-line path{stroke:rgb(31, 78, 148);filter:none;}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_165\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_165\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_165\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_165\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_165\_ .node text{font-size:14px;font-weight:600;letter-spacing:normal;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_165\_ .edgeLabels text{font-size:13px;font-weight:600;letter-spacing:-0.08px;fill:rgb(153, 206, 255);}#chatgpt-mermaid-\_r_165\_ .node tspan[font-weight="normal"],#chatgpt-mermaid-\_r_165\_ .edgeLabels tspan[font-weight="normal"]{font-weight:600;}#chatgpt-mermaid-\_r_165\_ .edgeLabel .label rect{opacity:1;rx:13px;ry:13px;fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-width:1px;}#chatgpt-mermaid-\_r_165\_ .node rect,#chatgpt-mermaid-\_r_165\_ .node circle,#chatgpt-mermaid-\_r_165\_ .node ellipse,#chatgpt-mermaid-\_r_165\_ .node polygon,#chatgpt-mermaid-\_r_165\_ .node path{fill:rgb(0, 40, 77);stroke:rgba(255, 255, 255, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_165\_ .node rect{rx:16px;ry:16px;}#chatgpt-mermaid-\_r_165\_ .mermaid-decision .label-container{fill:rgb(0, 14, 26);stroke:rgb(26, 62, 95);stroke-dasharray:2,2;}#chatgpt-mermaid-\_r_165\_ .edgePaths .flowchart-link{stroke:rgb(175, 175, 175);stroke-width:1px;stroke-linecap:round;stroke-linejoin:round;}#chatgpt-mermaid-\_r_165\_ .marker{fill:rgb(175, 175, 175);stroke:rgb(175, 175, 175);}#chatgpt-mermaid-\_r_165\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}理解研究目标制定检索计划检索与筛选全文分析研究覆盖与证据评估证据是否充分?调整检索词与检索范围多论文比较生成综述与报告否是

需要设置最大迭代次数、检索预算和人工确认节点，避免 Agent 无限搜索或自行扩大研究范围。

这是从“自动化研究工作流”升级为“自主科研 Agent”的关键一步。

### 3. 原文验证很强，但语义证据验证还不够

优先处理

目前 `validate_analysis()` 可以证明：

`模型引用的字符串确实出现在指定论文原文中。`

但它还不能证明：

`这段原文确实支持模型提出的结论。`

举个例子：

原文说：

> Our method did not outperform the baseline.

但模型可能生成：

> 该方法显著优于基线模型。

只要引文被逐字复制，当前的编号和字符串检查并不能自动识别这种语义矛盾。

建议增加 `ClaimVerifier`：

| 验证结果         | 含义          |
| ------------ | ----------- |
| Supported    | 原文明确支持结论    |
| Contradicted | 原文与结论矛盾     |
| Insufficient | 原文不足以证明结论   |
| Needs Review | 存在歧义，需要人工核查 |

可以先利用第二个模型进行语义校验，再用人工标注的数据集评估它的准确性。不要将模型评审结果直接当作事实判定。

这项能力比继续扩充会议目录更直接地提升 ResearchPilot 的科研可信度。

### 4. 研究计划与实际执行存在一个接口不一致

我注意到 [agent/research_planner.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/agent/research_planner.py?utm_source=chatgpt.com) 的规划动作中包含：

```
select_library_papers
```

并且 Prompt 建议在已有文献时优先使用这个动作。

但是 [api/research_execution.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/api/research_execution.py?utm_source=chatgpt.com) 的执行入口仍然依赖已完成的检索记录和候选论文 ID，前置步骤校验也没有将 `select_library_papers` 纳入可执行动作。

这意味着：研究计划可以表达“使用已有文献库”，但对应的完整执行路径似乎尚未接通。

建议增加一个复用已有文献的入口，而不是要求每次研究都重新执行外部检索。

此外，[agent/research_planner.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/agent/research_planner.py?utm_source=chatgpt.com) 中 `planning_capabilities()` 仍返回 `execution_available=False`，需要核对这个字段是否已经和实际执行能力脱节。

这是值得直接修复的具体接口一致性问题。

### 5. 顶刊顶会目录还没有真正实现 CCF 分级

你的 Venue 检索代码已经有一定规模。

但在 [tools/venue_search/catalog.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/tools/venue_search/catalog.py?utm_source=chatgpt.com) 中，目录项的 `ranking` 目前仍设为 `None`，并明确提示等级尚未导入。

所以现阶段可以说：

系统支持指定会议和期刊的检索与发表来源核验。

但还不能说：

系统已经能够完整按 CCF A/B/C 等级过滤正式发表的论文。

建议继续完成官方目录导入，并将分级版本与发表核验分开存储。这个方向与你最近希望检索 AI 顶刊顶会的目标一致。

### 6. 任务系统与数据存储存在两个数据库体系

我们第三课选择 PostgreSQL，是希望使用一个便于长期维护的数据库架构。

但现在 [api/tasks.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/api/tasks.py?utm_source=chatgpt.com) 使用：

```
sqlite3
```

并将任务状态保存在：

```
outputs/api_tasks.sqlite3
```

这不是程序错误。对本地单用户开发来说，这种设计可以工作。

但如果你打算将来部署成多用户系统，我建议将研究任务、计划版本和执行记录逐步迁移到 PostgreSQL，并使用独立 Worker 执行耗时操作。

目前 FastAPI `BackgroundTasks` 和 SQLite 的组合，更适合单进程原型，不适合直接作为多进程生产任务调度架构。

### 7. 项目文档与实际实现已经出现不同步

这个问题不影响科研算法，却会明显增加后续维护成本。

例如：

[docs/FEATURE_MAP.md](https://github.com/zhouhexin/ResearchAgentNew/blob/main/docs/FEATURE_MAP.md?utm_source=chatgpt.com) 仍在说明页面主要是演示模式、`/api/v1` 尚未实现。

但 [api/main.py](https://github.com/zhouhexin/ResearchAgentNew/blob/main/api/main.py?utm_source=chatgpt.com) 已经注册了大量真实 API，当前 README 也描述了完整研究计划和综述流程。

建议后续以当前代码为准，重新维护一份统一的能力矩阵，区分：

`已实现代码 / 模拟测试通过 / 真实环境验证通过 / 生产可用`

这样可以防止未来新增模块时出现文档和实际功能相互矛盾的情况。

## 五、从产品目标来看，哪些事情应该暂时停止？

我建议暂时降低新增 UI 页面、继续扩充模型服务兼容方案、以及增加更多零散论文来源的优先级。

它们仍有价值，但目前最需要的是把科研能力做深。

尤其是你的系统已经支持大量论文来源回退、网络故障诊断、PDF 下载超时、检查点恢复和页面状态维护。

这些属于必要的工程工作，但如果持续投入，而核心的研究质量评估和自主反馈没有同步提高，项目确实可能逐渐变成一个复杂的“文献管理与任务平台”，而不是我们最初希望的科研智能体。

## 六、我建议接下来的开发优先级

| 优先级 | 开发任务                         | 对原始目标的价值         |
| --- | ---------------------------- | ---------------- |
| P0  | 建立研究质量评测集                    | 知道系统生成的研究结论是否可信  |
| P0  | 实现 ClaimVerifier 语义证据验证      | 降低证据与结论不匹配的风险    |
| P0  | 增加研究覆盖评估与二次检索                | 提升 Agent 的自主研究能力 |
| P1  | 统一 OpenAlex、arXiv、DBLP 等检索服务 | 提高论文检索覆盖率        |
| P1  | 打通已有文献库直接执行研究计划              | 避免重复检索、提高可复用性    |
| P1  | 完成 CCF Venue 分级导入            | 满足顶刊顶会筛选需求       |
| P2  | 统一任务持久化、补充 CI 和文档            | 提升部署与长期维护能力      |

其中，ResearchPilot 的评测集至少应覆盖三类指标：

检索质量：相关论文是否被找到、重复论文是否合并正确、顶会主会身份是否误判。

分析质量：研究方法和实验指标提取是否准确、引文是否支持结论、遇到证据不足是否会拒绝猜测。

任务质量：用户给出一个研究目标后，系统能否完整执行、失败恢复、补充检索并生成可核验的报告。

先建立一个包含真实研究问题、论文和人工核验答案的小型测试集，比单纯继续增加功能更能帮助你判断 ResearchPilot 的实际水平。

## 七、如果让我重新定义 ResearchPilot v1.0 的验收标准

我不会要求你实现更多页面，而是要求系统真正完成一次有质量保障的科研任务。

例如输入：

> 请研究 2024—2026 年 LLM Agent 在任务规划和工具调用方面的发展，重点关注 ACL、ICLR、NeurIPS 等正式发表论文，比较主要方法、实验评测与研究局限，并指出值得进一步验证的问题。

合格的 ResearchPilot 应当：

1. 解析研究范围，生成可修改的研究计划。
2. 从多个学术来源检索并去重，核实会议和发表状态。
3. 选择相关论文，获取足够的原文材料。
4. 提取研究问题、方法、贡献、实验结果和局限性。
5. 评估现有文献是否覆盖用户要求；不足时补充检索。
6. 生成跨论文比较和综述，并逐条检查引用与结论的支持关系。
7. 对证据不足、来源无法核验或未经系统检索证实的研究假设明确标注限制。

只要能够稳定完成这条链路，ResearchPilot 就真正接近我们最初设定的产品目标。

## 八、关于这次审查的范围

这次我检查了 GitHub `main` 分支中的仓库结构、关键后端代码、LangGraph 工作流、证据验证器、Venue 模块、Vue 页面和测试布局。

我确认仓库中有大量单元测试及接口测试，但没有在你的真实 PostgreSQL、模型服务或浏览器环境运行完整测试，因此以上结论主要属于代码级架构与功能审查，而不是生产环境安全审计或性能测试。

仓库目前的 Web Agent 更偏向“规划和执行科研任务”，而不是第五课那种自由聊天问答。两种交互都可以保留，但需要在产品定位上讲清楚；研究工作流应当是核心，聊天是辅助入口。

## 最终结论

ResearchPilot 目前的路线是正确的，不需要推倒重来，也不需要大规模重构。

你已经解决了“如何把多个科研工具连接起来”这个基础问题。下一阶段最重要的是解决：

> Agent 如何知道自己的研究是否充分、证据是否真的支持结论，以及什么时候应该继续检索而不是直接生成报告？

我建议你把下一阶段命名为 ResearchPilot v1.0：可信自主科研 Agent。

把重点从“实现更多功能”转移到“提高研究过程的自主性与结果可靠性”，这会让项目更接近最初目标。