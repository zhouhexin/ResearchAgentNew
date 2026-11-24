<script setup lang="ts">
import type {ExecutionStep,ResearchExecution} from '../types/execution'
import TaskProgress from './TaskProgress.vue'
import ReadingCoverage from './ReadingCoverage.vue'
import PlanReportWorkspace from './PlanReportWorkspace.vue'
import QuestionCoverageWorkspace from './QuestionCoverageWorkspace.vue'
import ExecutionReviewLinks from './ExecutionReviewLinks.vue'
import ExecutionAcceptance from './ExecutionAcceptance.vue'
defineProps<{run:ResearchExecution}>()
defineEmits<{'execution-selected':[executionId:string]}>()
const names={pending:'待执行',queued:'排队中',running:'进行中',succeeded:'已完成',failed:'失败',cancelled:'已取消'}
const readingNames:Record<string,string>={full:'全量论文分析',fast:'快速论文阅读',sample:'抽样论文分析',supplemental:'问题导向补读',unknown:'分析范围待核对'}
const label=(s:ExecutionStep)=>names[s.status]+(s.status==='succeeded'&&s.reused?' · 复用已有结果':'')
</script>
<template>
   <p class="note">第 {{run.revision}} 版计划 · {{run.selection_kind==='review_versions'?'评审版本选择':run.selection_kind==='supplement_readings'?'补充阅读版本选择':run.supplement_round?'问题导向补读':run.analysis_scope==='fast'?'快速阅读':'全量分析'}} · 固定 {{run.papers.length}} 篇 · {{names[run.status]}} · {{run.papers.filter(p=>p.analysis.status==='succeeded').length}} / {{run.papers.length}} 篇分析完成</p>
   <p v-if="run.source_execution_id" class="note">补充研究后的分析选择分支，原执行 {{run.source_execution_id}} 保留；各分析与评审版本以本分支记录为准，模型判断仍需人工核对。</p>
   <p v-if="run.error" class="error-text">{{run.error.code}} · {{run.error.message}}</p>
   <article v-for="paper in run.papers" :key="paper.candidate_id" class="execution-paper">
    <h4><RouterLink v-if="paper.paper_id" :to="`/papers/${paper.paper_id}`">{{paper.title}}</RouterLink><span v-else>{{paper.title}}</span></h4>
    <p class="note">{{run.source==='library'?'文献库人工选文 · 非摘要初筛':'人工确认纳入 · 初筛'}}：{{run.source==='library'?'按固定材料执行':paper.screening.decision==='included'?'建议纳入':paper.screening.decision==='excluded'?'排除':'待确认'}}</p>
    <div v-for="step in ['fulltext','analysis'] as const" :key="step" class="execution-step">
     <p><strong>{{step==='fulltext'?'全文获取与解析':(readingNames[paper.analysis.scope||run.analysis_scope||'full']||'分析范围待核对')}}</strong> · {{label(paper[step])}} <RouterLink v-if="step==='fulltext'&&paper.fulltext.status==='succeeded'&&paper.fulltext.document_id&&paper.paper_id" :to="`/papers/${paper.paper_id}?tab=evidence`">查看原文</RouterLink><RouterLink v-if="step==='analysis'&&paper.analysis.status==='succeeded'&&paper.analysis.analysis_id" :to="`/analysis?analysisId=${encodeURIComponent(paper.analysis.analysis_id)}`">查看分析</RouterLink></p>
     <TaskProgress v-if="['running','failed'].includes(paper[step].status)&&paper[step].task&&['queued','running','failed'].includes(paper[step].task!.status)" :task="paper[step].task!"/>
     <p v-if="paper[step].error" class="error-text">{{paper[step].error!.code}} · {{paper[step].error!.message}}</p>
    </div>
    <div v-if="(run.review_after_analysis||run.review_selection_id)&&paper.review" class="execution-step">
     <p><strong>辅助语义评审</strong> · {{label(paper.review)}} <RouterLink v-if="paper.review.review_id&&paper.analysis.analysis_id" :to="`/analysis?analysisId=${encodeURIComponent(paper.analysis.analysis_id)}&claimReviewId=${encodeURIComponent(paper.review.review_id)}`">查看评审与原文</RouterLink></p>
     <TaskProgress v-if="paper.review.task&&paper.review.status!=='succeeded'" :task="paper.review.task"/>
     <p v-if="paper.review.error" class="error-text">{{paper.review.error.code}} · {{paper.review.error.message}}</p>
     <p v-if="paper.review.status==='succeeded'" class="note">已核验 {{paper.review.completed_claims}} / {{paper.review.total_claims}} 条。模型判断供人工核对，分析结论保留。</p>
     <p v-if="paper.review.status==='succeeded'&&paper.review.verdict_counts" class="note">支持 {{paper.review.verdict_counts.supported||0}} · 矛盾 {{paper.review.verdict_counts.contradicted||0}} · 证据不足 {{paper.review.verdict_counts.insufficient||0}} · 需人工核对 {{paper.review.verdict_counts.needs_review||0}}</p>
    </div>
    <ReadingCoverage :selection="paper.analysis.reading_selection||paper.analysis.task?.reading_selection"/>
   </article>
   <p v-if="run.status==='succeeded'" class="note">本轮全文、分析{{run.review_after_analysis?'与辅助评审':''}}已完成。可在下方继续比较、综述和导出。</p>
   <ExecutionReviewLinks v-if="run.papers.every(p=>p.analysis.status==='succeeded')" :execution="run" @rechecked="$emit('execution-selected',$event)"/>
   <PlanReportWorkspace v-if="run.status==='succeeded'" :key="run.id" :execution="run"/>
   <QuestionCoverageWorkspace v-if="run.papers.every(p=>p.analysis.status==='succeeded')" :key="'coverage-'+run.id" :execution="run" @execution-selected="$emit('execution-selected',$event)"/>
   <ExecutionAcceptance :execution="run"/>
</template>
<style scoped>.execution-paper{border-top:1px solid #edf0f4;padding:12px 0}.execution-paper h4{font-size:14px}.execution-step{font-size:13px;line-height:1.7}.execution-step a{margin-left:12px}</style>
