<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import type {ResearchExecution} from '../types/execution'
import type {ReviewComparison,ReviewResult,TaskInfo,ReadingSelection} from '../types/research'
import {categories} from '../types/research'
import TaskProgress from './TaskProgress.vue'
import ReadingCoverage from './ReadingCoverage.vue'
import EvidenceCitation from './EvidenceCitation.vue'
import ReviewReportBody from './ReviewReportBody.vue'
const props=defineProps<{execution:ResearchExecution}>()
interface Report {id:string;revision:number;status:string;research_question:string;review_id?:string;task:TaskInfo;review_task?:TaskInfo;error:TaskInfo['error'];comparison?:ReviewComparison;context:{comparison_dimensions:string[];reading_coverage:{paper_id:number;scope:string;selected_chunks:number;total_chunks:number;reading_selection?:ReadingSelection}[]}}
const question=ref(''),dimensions=ref(''),records=ref<Report[]>([]),selected=ref(''),matrix=ref<ReviewComparison|null>(null),review=ref<ReviewResult|null>(null),error=ref(''),busy=ref(false),loading=ref(false)
const report=computed(()=>records.value.find(r=>r.id===selected.value)||records.value[0])
const active=computed(()=>records.value.some(r=>['queued','running'].includes(r.status)))
const supported=computed(()=>['compare_papers','generate_review'].every(a=>props.execution.plan.steps.some(s=>s.action===a)))
let version=0,ticket=0,initialized=false,timer:ReturnType<typeof setTimeout>|undefined
async function load(){
 clearTimeout(timer);const current=version,id=++ticket
 if(mode()==='demo')return
 loading.value=true
 try{
  const response=await api.planReports(props.execution.plan_id,props.execution.id)
  if(current!==version||id!==ticket)return
  records.value=response.items as Report[]
  if(!initialized&&report.value){question.value=report.value.research_question;dimensions.value=report.value.context.comparison_dimensions.join('\n')}
  initialized=true
  if(report.value?.comparison)matrix.value=report.value.comparison
  if(report.value?.review_id&&review.value?.id!==report.value.review_id){
   const value=await api.review(report.value.review_id)
   if(current!==version||id!==ticket)return
   review.value=value
  }
  if(active.value)timer=setTimeout(()=>void load(),document.hidden?10000:2500)
 }catch(e){if(current===version&&id===ticket)error.value=(e as Error).message}
 finally{if(current===version&&id===ticket)loading.value=false}
}
async function compare(){
 if(busy.value||mode()==='demo')return
 const current=version;busy.value=true;error.value=''
 try{const value=await api.planComparison(props.execution.plan_id,props.execution.id);if(current===version)matrix.value=value}
 catch(e){if(current===version)error.value=(e as Error).message}finally{if(current===version)busy.value=false}
}
async function generate(){
 if(busy.value||active.value||!question.value.trim()||!dimensions.value.trim()||mode()==='demo')return
 const current=version;busy.value=true;error.value=''
 try{
  const result=await api.startPlanReport(props.execution.plan_id,props.execution.id,{research_question:question.value.trim(),comparison_dimensions:dimensions.value.split('\n').map(s=>s.trim()).filter(Boolean)})
  if(current!==version)return
  selected.value=result.report_id;review.value=null;await load()
 }catch(e){if(current===version)error.value=(e as Error).message}finally{if(current===version)busy.value=false}
}
function choose(id:string){selected.value=id;review.value=null;matrix.value=null;if(report.value){question.value=report.value.research_question;dimensions.value=report.value.context.comparison_dimensions.join('\n')}void load()}
watch(()=>props.execution.id,()=>{version++;ticket++;initialized=false;clearTimeout(timer);records.value=[];selected.value='';review.value=null;matrix.value=null;error.value='';busy.value=false;question.value=props.execution.plan.research_questions.join('\n');dimensions.value=props.execution.plan.comparison_dimensions.join('\n');void load()},{immediate:true})
onBeforeUnmount(()=>{version++;ticket++;clearTimeout(timer)})
</script>
<template>
 <section class="plan-report">
  <h3>本轮比较与综述</h3>
  <p class="note">固定使用第 {{execution.revision}} 版计划下本轮论文的分析版本。确认研究问题和比较维度后生成综述，完成后可直接导出。</p>
  <p v-if="!supported" class="note">该版本计划未包含比较与综述步骤，暂不能执行报告。</p>
  <template v-else>
   <label>研究问题<textarea :value="question" :disabled="busy||active||loading" maxlength="1000" rows="3" aria-label="本轮综述研究问题" @input="question=($event.target as HTMLTextAreaElement).value"/></label>
   <label>比较维度（每行一项）<textarea :value="dimensions" :disabled="busy||active||loading" rows="2" aria-label="本轮综述比较维度" @input="dimensions=($event.target as HTMLTextAreaElement).value"/></label>
   <div class="report-actions"><el-button :disabled="busy||mode()==='demo'" @click="compare">查看本轮比较矩阵</el-button><el-button type="primary" :disabled="busy||active||!question.trim()||!dimensions.trim()||mode()==='demo'" @click="generate">确认问题并生成本轮综述</el-button><el-button :disabled="loading||mode()==='demo'" @click="load">刷新报告进度</el-button></div>
  </template>
  <p v-if="error" role="alert" class="error-text">{{error}}</p>
  <label v-if="records.length>1">报告记录<select :value="report?.id" aria-label="本轮报告历史" @change="choose(($event.target as HTMLSelectElement).value)"><option v-for="r in records" :key="r.id" :value="r.id">{{r.status}} · {{r.research_question}}</option></select></label>
  <TaskProgress v-if="report&&report.status!=='succeeded'" :task="report.review_task||report.task"/>
  <p v-if="report?.status==='failed'" class="note">比较和单篇分析已保留；使用相同研究问题和维度再次生成，会重试本次报告。</p>
  <details v-if="matrix" open><summary>本轮比较矩阵 · 固定 {{matrix.analysis_ids.length}} 个分析版本</summary><p class="note">下表按现有分析类别展示证据；上方比较维度用于综述中的重点对照，不足以比较的项目须注明证据不足。</p><div class="matrix-scroll"><table><thead><tr><th>研究维度</th><th v-for="p in matrix.matrix" :key="p.paper_id">P{{p.paper_id}} · {{p.title}}</th></tr></thead><tbody><tr v-for="(name,key) in categories" :key="key"><th>{{name}}</th><td v-for="p in matrix.matrix" :key="p.paper_id"><div v-for="(f,i) in p.analysis[key]" :key="i"><p>{{f.statement}}</p><EvidenceCitation v-for="e in f.evidence" :key="e.evidence_id" :ref-id="`P${p.paper_id}:${e.evidence_id}`"/></div><span v-if="!p.analysis[key].length">本次证据不足</span></td></tr></tbody></table></div></details>
  <template v-if="report"><div v-for="c in report.context.reading_coverage" :key="c.paper_id"><p class="note">P{{c.paper_id}} · {{c.scope==='fast'?'快速阅读':c.scope==='full'?'全量分析':'部分阅读'}} · {{c.selected_chunks}} / {{c.total_chunks}} 个片段</p><ReadingCoverage :selection="c.reading_selection"/></div></template>
  <article v-if="review" class="report-body"><h3>{{review.draft.title}}</h3><p>研究问题：{{review.topic}}</p><p class="note">比较维度：{{report?.context.comparison_dimensions.join('、')}}</p><p v-if="review.is_current===false" class="error-text">原文已更新，这是历史报告，请重新分析后生成。</p><div class="report-actions"><a :href="api.reviewFileUrl(review.id,'md')" target="_blank" rel="noopener">导出 Markdown</a><a :href="api.reviewFileUrl(review.id,'csv')" target="_blank" rel="noopener">导出比较矩阵 CSV</a><a :href="api.reviewFileUrl(review.id,'json')" target="_blank" rel="noopener">导出 JSON</a><RouterLink :to="`/reviews?reviewId=${encodeURIComponent(review.id)}`">打开完整报告</RouterLink></div><ReviewReportBody :draft="review.draft" :review="review"/></article>
 </section>
</template>
<style scoped>.plan-report{border-top:1px solid #dfe7ef;margin-top:20px;padding-top:18px}.plan-report>label{display:grid;gap:8px;font-size:13px;margin:14px 0}.plan-report textarea,.plan-report select{font:inherit;padding:9px;border:1px solid #ccd6e2;border-radius:4px;max-width:100%;box-sizing:border-box}.report-actions{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:16px 0}.matrix-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{border:1px solid #e2e8ef;padding:12px;text-align:left;vertical-align:top;min-width:140px}td p{line-height:1.7}.report-body{margin-top:20px;line-height:1.8}</style>
