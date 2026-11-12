# 测试说明：怎样知道程序是否正常

说明文字更新：2026-10-22。

测试就是让程序自动做一些检查，例如“重复导入是否增加了重复数据”“不存在的引用会不会被误当成真的”。

**一组测试通过，只能说明它检查的部分通过。**
不连接真实模型的测试，不能证明模型服务可用；不打开浏览器的测试，也不能证明每个按钮都好点。

## 1. 先分清三类检查

| 检查什么 | 需不需要连接外部服务 | 可以发现哪些问题 |
| --- | --- | --- |
| Python 本地逻辑 | 这组命令不连真实数据库或模型 | 分块、页码、引用格式、错误处理等代码问题 |
| 数据库保存和读取 | 需要可用的测试数据库 | 重复数据、保存失败后的撤销、全文更新和证据读取问题 |
| 页面和浏览器操作 | 一部分可本地检查，完整操作还需浏览器和后端 | 页面能否生成、数据是否显示、上传和按钮是否正确 |

测试中的“模拟响应”是事先准备一个返回结果，代替真的请求模型或搜索服务。
这样容易检查代码，但不能代替真实联网操作。

## 2. 检查 Python 本地逻辑

在项目主目录运行：

```bash
conda activate py311
python -m pytest -q test/test_academic_search.py test/test_repository.py::test_normalize_identifiers test/test_document_parser.py test/test_citation_guard.py test/test_paper_analysis.py test/test_analysis_runtime.py test/test_review.py test/test_review_citations.py
```

`pytest` 是运行这些检查的工具，`-q` 表示少打印一些过程信息。

这组检查主要看：

- 学术搜索结果能否被正确整理。
- PDF 页码和原文片段位置是否正确。
- 假引用、改写后的原句能否被拦住。
- 模型返回空内容、超时或错误格式时，程序能否合理处理。
- 综述有没有实际引用不同论文。

其中真实 PDF 检查需要 `data/pdfs/` 中有本地文件；没有样本时，对应检查可能会跳过。

## 3. 检查数据库保存和读取

```bash
python -m pytest -q test/test_repository.py test/test_document_repository.py
```

执行前，在 `.env` 中配置 `TEST_DATABASE_URL`。
它必须连接到名为 `researchpilot_test` 的测试数据库，数据库用户也需要创建临时工作区域的权限。

“临时工作区域”在 PostgreSQL 中叫 schema。每项测试使用自己的区域，结束后清理自己的数据，不清空已有业务表。

这组检查主要看：

- 同一论文保存两次，是否仍只有一篇。
- 一批保存失败后，是否撤销了不该留下的修改。
- 真实 PDF 能否保存，原文能否取回来。
- PDF 内容变化后，旧片段有没有被正确替换。
- 文本不变但页码变化时，位置记录会不会过期。

连不上测试数据库时，这部分不算通过。

## 4. 检查前端页面代码

```bash
cd web
npm run build
npm test
npm run test:render
npm run test:data
```

| 命令 | 通俗解释 | 不能据此证明什么 |
| --- | --- | --- |
| `npm run build` | 检查 TypeScript 文件，并把 Vue 页面编译成浏览器能使用的文件 | 不是完整的 Vue 页面类型检查，也没实际点按钮 |
| `npm test` | 检查引用编号、链接、年份和表格下载文字的处理 | 不代表真实接口返回正确 |
| `npm run test:render` | 不开浏览器，检查页面和组件能否生成 HTML；这也叫 SSR 渲染检查 | 不检查实际屏幕布局、鼠标点击和 PDF 阅读器 |
| `npm run test:data` | 用示例数据检查收藏去重、引用归属、任务状态、综述引用和错误提示 | 不代表已经写入真实数据库或调用真实模型 |

## 5. 真正交付前，还要手动走一遍

后端接口接好后，应在浏览器完成：

1. 搜论文、收藏，刷新后还能在文献库里找到。
2. 给正确论文上传 PDF，看到真实片段和实际页码。
3. 向 Agent 提问，点击引用能打开对应原文。
4. 分析论文，检查结论和引用，再下载结果。
5. 用至少两篇已分析论文生成比较表和综述，下载文件。
6. 刷新页面，确认任务状态没有丢失；失败时能看懂原因。

从头到尾完成这样的操作，通常叫“端到端测试”。

## 6. 已有检查记录

以下是 **2026-10-20 的检查结果**，不是 10 月 22 日修改文档后重新运行的结果：

- Python 本地逻辑：55 项通过；另有 5 条来自 PyMuPDF/Swig 的弃用提醒。
- 前端构建通过，5 项工具函数检查、12 项页面与组件渲染检查通过，演示数据流程检查通过。
- 文档链接和依赖重复项检查通过。
- 当时没有连接真实数据库或模型，不能把它写成在线功能已通过。
- 当时执行环境不允许启动本地监听服务，因此浏览器实际点击、上传和 PDF 翻页尚未验收。

本次只调整说明文字，检查文档链接和命令描述，不重新运行数据库或模型任务。

## PDF 预览与证据跳转（真实 API）

1. 启动后端 `python -m uvicorn api.main:app --reload --port 8000`，在 `web/` 运行 `npm run dev`。
2. 打开前端“设置”，选择“真实 API”，点击“应用模式并刷新”。
3. 在文献库打开一篇已经导入本地 PDF 的论文，进入“全文阅读”。应直接看到 PDF，且可以翻页。
4. 如果论文有多份全文，使用文档下拉框选择 PDF。网页全文没有 PDF 预览属于正常情况。
5. 进入“证据片段”，选择 PDF 对应的文档，点击“在 PDF 中查看”。应保留所选文档，并向浏览器阅读器传入片段页码；是否跳页取决于浏览器内置 PDF 阅读器，不会高亮原句。
6. 调试时可查看 `GET /api/v1/papers/{paper_id}/documents`：可读取的 PDF 应带有 `/api/v1/documents/{document_id}/file` 地址。文件接口应返回 `application/pdf`，支持 Range 分段读取。

如果提示没有可预览的 PDF，请检查原导入文件是否仍在原位置。全文片段已保存到数据库，原文件丢失不会删除这些片段。页面上的“上传并解析全文”已实现；网页链接导入尚未实现。

自动化验证：`python -m pytest -q test/test_api_documents.py`。这里使用 SQLite 验证接口行为和临时 PDF 验证文件传输，不代表真实 PostgreSQL 和浏览器联调已经通过。

## 从页面上传并解析 PDF

1. 使用已有 Python 环境启动 `python -m uvicorn api.main:app --reload --port 8000`，前端在 `web/` 运行 `npm run dev`。上传表单需要 `python-multipart`；它已加入 requirements.txt。
2. 前端设置选择“真实 API”，打开已收藏论文的“全文阅读”。
3. 选择或拖入一份不超过 20 MB、能够提取文字的 PDF。核对论文标题，勾选“我已确认 PDF 对应当前论文”，点击“上传并解析全文”。
4. 任务完成后，临时预览应切换为服务端已保存的 PDF，论文出现“已获取全文”。进入“证据片段”，查看页码、原文和引用。刷新页面后仍可查看 PDF 与片段。
5. 再次上传同一份 PDF，即使重命名文件，全文记录编号和证据编号也应保持不变。内容不同的 PDF 会作为另一个来源保存，原来源保留。
6. “任务中心”应出现全文解析任务，成功后点击“查看结果”进入对应论文的证据页。扫描版无法提取正文时显示需要 OCR 的失败提示，且不写入空全文。

文件保存在 `data/pdfs/uploads/{paper_id}/`，文件名使用上传内容的 SHA-256，不采用浏览器传来的路径。同一论文的相同文件复用保存位置。任务记录保存在 `outputs/api_tasks.sqlite3`；论文、全文及片段仍使用已有 PostgreSQL。

这一阶段使用单进程本地后台任务，运行时不要设置多个 uvicorn worker。重启后未完成的任务标记为中断，可重新上传；完成的记录保留。尚未实现 OCR、任务取消和自动重试。

验证命令：`python -m pytest -q test/test_api_documents.py test/test_api_uploads.py`。测试使用真实生成的 PDF 和隔离 SQLite，覆盖上传、解析、保存、预览、证据位置、重复导入、内容变化及失败反馈；不代表真实 PostgreSQL 与浏览器联调通过。

## 获取开放原文按钮

1. 重启后端并刷新前端，设置选择“真实 API”。
2. 在“文献检索”搜索并收藏一篇有开放 PDF 的论文，然后进入该论文详情页。
3. 点击顶部或“全文阅读”侧栏的“获取原文”。页面应显示查找、下载和解析进度。
4. 完成后应能直接预览 PDF，进入“证据片段”能查看原文和页码。文件保存在 `data/pdfs/uploads/{paper_id}/`。
5. 再点击一次，确认相同 PDF 的全文记录和证据编号没有增加。
6. 没有开放 PDF 时，任务应提示未找到可下载全文；仍可手动上传。扫描版 PDF 下载成功但解析失败时，本地保留 PDF，任务提示需要 OCR。

查找使用 OpenAlex 的论文标识与开放来源，不保证所有开放论文都有可直接下载的 PDF，也不绕过登录、验证码和付费限制。已有 `OPENALEX_API_KEY` 配置会被复用。

测试命令：`python -m pytest -q test/test_open_access_pdf.py test/test_api_fulltext.py test/test_api_uploads.py test/test_api_documents.py`。接口测试使用真实生成的 PDF、隔离 SQLite 和模拟网络响应；覆盖自动获取、保存、解析、重复获取、备用来源、非 PDF、超限及内网跳转拒绝，不代表实际公网下载和 PostgreSQL 联调通过。

## arXiv 备用来源

“获取原文”现在依次尝试 OpenAlex、arXiv；已有按钮无需改变。OpenAlex 没有开放 PDF、服务请求失败或下载地址不可用时，继续尝试 arXiv。

- 若论文来源地址或 DOI 包含明确的 arXiv 编号，按编号查询，并保留明确指定的版本。
- 否则要求有标题和作者：查询 arXiv 后只自动下载标题标准化后一致、作者姓氏至少有一项匹配的记录；返回 DOI 与当前论文冲突时拒绝。不会直接使用搜索结果第一项。
- 页面任务进度显示当前来源，成功后显示下载来源；arXiv 可能是预印本，与出版社排版或最终版本存在差异。
- 没有匹配结果时仍提示手动上传；不是所有期刊论文都会出现在 arXiv。

手动验证可使用已有来源为 `https://arxiv.org/abs/1706.03762` 的论文记录，或标题、作者完整且可在 arXiv 找到的记录。点击“获取原文”后观察任务是否切换到 arXiv，成功后查看 PDF、原文片段与下载来源。联网失败时检查 arxiv.org 和 export.arxiv.org 的连通性。

自动化：`python -m pytest -q test/test_arxiv_pdf.py test/test_api_fulltext.py test/test_open_access_pdf.py test/test_api_uploads.py test/test_api_documents.py`。仍为模拟网络与隔离 SQLite 测试，不代表真实公网和 PostgreSQL 联调通过。

实现参考：[arXiv 官方 API 手册](https://info.arxiv.org/help/api/user-manual.html)。查询使用 Atom API，并为本地请求设置至少 3 秒的间隔。

## DOI 原站备用查询和失败明细

“获取原文”依次尝试 OpenAlex、DOI 原站、arXiv。DOI 原站查询会跟随公开 HTTPS 跳转，只读取页面明确标注的 `citation_pdf_url` 等 PDF 元数据，不抓取引用区的其他论文链接。这个渠道可用于机构仓储页面提供 PDF、但 OpenAlex 没有下载地址的情况；动态页面或没有 PDF 元数据的网站仍可能无法自动取得全文。

新任务会保存 `source_results`，论文详情页和任务中心分别显示每个来源的结果。arXiv 会区分检索无结果、标题不同、作者不匹配及 DOI 冲突。已有历史任务没有这些诊断数据，需要重新点击“获取原文”创建新任务。

补充测试：`python -m pytest -q test/test_doi_pdf.py test/test_api_fulltext.py`。机构仓储跳转与下载使用模拟网络响应；没有在线确认任意特定 DOI 的全文开放状态。

## DOI 页面里的下载按钮与 ETH 文件接口

DOI 查询现在除了读取 PDF 元数据，也能识别同站的 PDF 下载按钮、`/bitstreams/{id}/download`、旧版 `/bitstream/handle/...` 等文件链接。优先正文链接，过滤参考文献、推荐论文和补充材料区域。

ETH Research Collection 的 `/entities/publication/{uuid}` 页面如果没有直接暴露下载链接，会读取该条目的公开文件接口：核对条目 UUID 与 DOI，只读取 ORIGINAL 文件组内的 PDF，再交给既有下载与解析流程。若保存的论文来源已经是 ETH 条目页面，也可直接查文件接口。

验证：`python -m pytest -q test/test_eth_pdf.py test/test_doi_pdf.py test/test_api_fulltext.py`。覆盖可见下载按钮、JavaScript 页面外壳、已有来源地址、DOI 不一致拒绝，以及完整下载/解析/保存流程。远程响应使用测试样本；执行环境连接实际 ETH 页面失败，未验证指定条目真实下载成功。

## 网络错误定位与已确认来源

新任务的 `source_results[].diagnostic` 保存 HTTP 状态码、站点域名、异常类型和底层异常类别；不会保存完整 URL、请求头、密钥或异常原文。可区分 HTTP 401/403/429、超时、代理、证书和 DNS 错误。历史任务只有通用错误时，无法事后还原原始异常。

用户提供的 ETH 页面可登记在 `data/fulltext_sources.json`，格式为 `{ "规范化DOI": "HTTPS ETH条目地址" }`。获取全文时可直接从该条目查询文件，不必先经过 DOI 跳转服务；实际下载前仍核对仓储条目的 DOI。文件属于本地数据，不修改论文元数据。测试通过 `RESEARCHPILOT_FULLTEXT_SOURCES_FILE` 指定隔离的来源配置。

## arXiv 检索替换 OpenAlex

重启后端并刷新前端，在“真实 API”模式进入“文献检索”：

1. 页面只显示 arXiv 数据来源。输入 monocular depth 或 Attention Is All You Need 检索。
2. 检查标题、作者、摘要、arXiv 编号，测试首次提交年份、相关性/最新提交/最近更新排序和分页。页面不显示引用量排序。
3. 收藏一篇未保存论文，再次检索应显示“已收藏”；重复保存及 arXiv 版本更新应复用收藏编号。
4. 打开详情，点击“获取原文”，任务应优先尝试 arXiv，成功后查看 PDF 和证据。
5. 旧的 OpenAlex 文献仍保留在文献库。旧书签中的 source=openalex 会在前端重新提交时规范为 arxiv；直接请求旧 API 参数会返回 422。

演示模式只提供明确标注的 arXiv 示例记录，不再将 OpenAlex 快照当作 arXiv 搜索结果。Agent、academic_demo.py 和 sync_papers.py 的检索调用已同步改为 arXiv。

新增验证：`python -m pytest -q test/test_arxiv_search.py`，包括检索参数、真实分页、稳定编号、收藏去重、新版本识别、旧文献收藏识别，以及搜索→收藏→arXiv PDF→证据的完整接口流程。网络响应使用模拟数据，数据库使用隔离 SQLite，不代表公网或 PostgreSQL 实测成功。

## 从页面发起单篇 AI 分析

1. 确认 .env 已配置 LLM_MODEL、LLM_API_KEY，以及服务需要的 LLM_BASE_URL。沿用现有本地 Python 环境启动后端，前端选择“真实 API”。
2. 打开一篇已经导入 PDF、能看到真实证据片段的论文。点击“分析论文”，或从侧栏进入“论文分析”选择论文。
3. 阅读片段上限先用默认 6；点击“开始分析”。应出现任务进度，模型请求阶段可能持续数分钟。
4. 成功后自动显示研究问题、方法、贡献、实验和局限；证据不足的分类允许为空。检查阅读片段数量，点击引用编号核对原文，并下载分析 JSON。
5. 刷新页面仍能看到报告；论文详情“AI 分析”和文献库“已分析”状态应同步。“任务中心”能进入对应分析结果。
6. 再次分析成功后旧报告仍在历史中；再次分析失败也不删除旧成功报告。全文或所读片段更新后，旧报告显示为历史原文，并且不再计入当前“已分析”。

没有全文时先导入 PDF。超时可减少阅读片段或检查模型连接；模型引用错误时不会放宽原文核验，详细诊断在 outputs/api_analysis_errors/。若后端重启打断分析，任务会显示中断，需手动重新发起。

验证：`python -m pytest -q test/test_api_analyses.py test/test_paper_analysis.py test/test_api_documents.py test/test_api_uploads.py`。API 测试调用真实的上下文采样、结构化响应解析、引文修正/核验、版本检查、结果保存和查询流程；仅模型响应可控、数据库隔离为 SQLite，不代表已完成实际 PostgreSQL、真实模型和浏览器联调。

## 页面生成多篇比较与综述

1. 至少准备两篇已经完成页面 AI 分析的论文，确认其引用可打开、文献库显示“已分析”。
2. 重启后端、刷新前端，设置选择“真实 API”，进入“文献综述”。
3. 选择 2–5 篇相关论文，点击“查看比较矩阵”。检查五类研究维度和引用；这一步只读取已有分析，不调用模型。
4. 输入与所选论文相关的主题，点击“生成综述”。任务应显示核验输入、生成矩阵、请求模型、核验引用的阶段。
5. 成功后查看报告并点击 P编号:E编号核对原文。报告包含实际分析版本，不能把当前报告理解为完整领域查新。
6. 分别下载 CSV、JSON、Markdown，检查中文编码、比较内容、引用和版本信息。刷新页面或从任务中心“查看结果”再次打开，应能读到同一报告。
7. 再次生成会保留历史。若引用错误或模型超时，新任务失败但旧报告仍可打开。全文更新后旧报告标为历史，需重新分析后再生成新报告。

缺少有效分析、重复选择、版本与论文不对应或无可用证据结论，会在调用模型前拒绝。主题不受材料支持时，模型可能无法生成满足至少两篇引用要求的综述；系统不会为凑数量放宽校验。

自动化：`python -m pytest -q test/test_api_reviews.py test/test_review.py test/test_review_citations.py test/test_api_analyses.py`。覆盖矩阵预览、固定版本、任务进度、来源归属、结构与引用失败、原文变化、任务去重、历史保留及三种文件下载。模型回复可控，PostgreSQL 使用隔离 SQLite 替代；不表示真实模型、公网或浏览器端到端联调完成。

## 分批分析检查

先读[分批分析使用说明](BATCH_ANALYSIS_GUIDE.md)。页面默认全文、每批 2 个片段。可先选择抽样 6 个片段来试跑，应显示约 3 批；每批完成后计数增加，全部完成才出现新报告。

失败后刷新页面，确认任务进度仍在，再点“继续未完成批次”。模型请求应从失败批开始，已成功部分显示复用。换原文、模型或分组方式后，旧进度应被拒绝混用；只修改超时可继续。

验证命令：`python -m pytest -q test/test_batch_analysis.py test/test_analysis_runtime.py test/test_api_analyses.py`。包含 API 和命令行的失败后续跑、全文/抽样覆盖、仅重试当前批、缓存再核验、服务中断后的进度保留和合并引用。测试使用隔离 SQLite 与受控模型，不表示真实模型与 PostgreSQL 联调完成。
