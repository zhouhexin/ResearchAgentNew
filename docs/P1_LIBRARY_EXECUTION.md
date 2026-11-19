# P1：从已有文献库执行研究计划

实现范围：网页研究计划的 `select_library_papers` 已接通实际执行。复用 P0 配置清单、现有分析/评审任务、比较、综述和导出，不创建虚构检索记录，也不自动进行外部检索或下载。真实流程需另行本机验收。

## 如何使用与验收

1. 保持 `RESEARCH_RUNTIME_PINNING=true`，更新代码、重启后端。本步沿用 P0 四张表，无需新建数据库表。旧任务仍按原代码清单核对，建议用新计划验收。
2. 在科研 Agent 输入，例如“根据已有文献库中的机器人视觉论文，选择两篇比较方法和实验，形成综述；不要外部检索”。确认计划包含 `select_library_papers`、`analyze_papers`，需要报告时再包含 `compare_papers`、`generate_review`。步骤按依赖顺序排列，不把外部检索作为该入口前置动作。
3. 保存计划后，出现“从已有文献库执行”。按标题/DOI 查询有全文的论文，跨页选择 2–5 篇。选文只是人工纳入，不冒称已通过摘要初筛或满足全部研究条件。
4. 默认按阅读范围自动复用有效分析；每篇可手动指定一个当前有效的分析版本。明确指定时尊重该版本实际阅读覆盖，即使它是部分分析，也不会冒称已全量阅读。没有可复用分析时按所选全量/快速模式补齐。
5. 可选“分析后自动辅助评审”，会增加模型请求和等待；勾选后按本轮固定 Review 配置评审，不采用历史分析的模型配置代替。
6. 执行记录应显示 `source=library`、`search_run_id=null`。已选 paper_id、分析版本、全文版本、片段内容及位置固定。材料变化、原版本缺失或凭据不可用时明确停止；继续只补齐原范围的未完成项，不静默改用最新分析。
7. 本轮完成后，在同一页面确认研究问题/比较维度，生成比较矩阵、综述并导出。报告上下文应标记 `selection_source=library`，并关联计划、执行和分析版本；启用评审时保留对应评审编号。

第一版要求所选论文已有可读全文；尚未上传或未解析时先到文献库补齐。为保持固定输入，子任务不会自动把选定 PDF 换成新的解析版本；需要重新解析时另行操作，再明确新建执行。

计划修改后不能用旧 revision 开始新执行；旧执行仍保留原计划。切换选文、指定分析、阅读模式或评审选项会改变去重范围；在途任务不能被另一套选文覆盖。

## 接口

```text
POST /api/v1/agent/plans/{plan_id}/library-executions
{
  "expected_revision": 1,
  "papers": [{"paper_id": 1, "analysis_id": "analysis_..."}, {"paper_id": 2}],
  "analysis_scope": "full",
  "review_after_analysis": false
}
```

复用现有 `/executions` 列表、`/executions/{id}/resume`、比较和报告接口。未设置 analysis_id 时，在入队前固定实际选中的有效分析，或固定需要补读的上下文。成功分析的来源版本保持原样，新任务的运行清单仅描述本轮实际执行。

## 检查

```bash
python -m pytest -q test/test_library_execution.py test/test_research_execution.py test/test_api_analyses.py test/test_plan_reports.py test/test_research_planning.py
cd web
npm run build
npm run test:library-execution
npm run test:execution-review
npm run test:planning
```

受控测试禁止检索、收藏和下载调用，验证真实 HTTP 请求参数、补齐分析、固定旧分析、评审恢复及报告导出；拒绝重复论文、失效/异属分析、不可读全文、错误计划版本。测试使用临时数据库和模拟服务，不代表真实模型或浏览器验收通过。

本步不新增智能文献库推荐、覆盖评估、自动补检索、评测授权或 Evolution。
