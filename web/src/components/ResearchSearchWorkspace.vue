<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import {useTaskStore} from '../stores/tasks'
import type {ResearchPlanRecord} from '../types/planning'
import type {ResearchSearchResponse,ResearchSearchRun,ScreeningDecision} from '../types/research-search'
import {statusNames} from '../types/research'
import {planVenues} from '../types/planning'
import TaskProgress from './TaskProgress.vue'
import PublicationStatus from './PublicationStatus.vue'
import AbstractTranslation from './AbstractTranslation.vue'
import PlanExecutionWorkspace from './PlanExecutionWorkspace.vue'

const props=defineProps<{record:ResearchPlanRecord;disabled?:boolean;initialRunId?:string}>()
const store=useTaskStore(),run=ref<ResearchSearchRun|null>(null),history=ref<ResearchSearchResponse['history']>([])
const taskId=ref(''),perQuery=ref(10),creating=ref(false),loading=ref(false),error=ref(''),notice=ref(''),saving=ref(false)
const filter=ref<ScreeningDecision|'pending'|'all'>('all'),selected=ref<string[]>([])
const labels={included:'建议纳入',excluded:'排除',uncertain:'待确认',pending:'尚未初筛'}
const verdicts={meets:'符合',fails:'不符合',unknown:'信息不足'}
const task=computed(()=>store.tasks[taskId.value])
const active=computed(()=>['queued','running'].includes(task.value?.status||run.value?.status||''))
const currentRun=computed(()=>run.value?.revision===props.record.revision)
const count=(key:string)=>run.value?.candidates.filter(p=>(run.value?.screenings[p.candidate_id]?.decision||'pending')===key).length||0
const visible=computed(()=>run.value?.candidates.filter(p=>filter.value==='all'||(run.value?.screenings[p.candidate_id]?.decision||'pending')===filter.value)||[])
const allowed=computed(()=>props.record.plan.steps.some(s=>s.action==='search_papers')&&props.record.plan.steps.some(s=>s.action==='screen_papers'))
const blocked=computed(()=>props.disabled||creating.value||loading.value||active.value||mode()==='demo'||!allowed.value)
const selectedRun=ref('')
let version=0,requestVersion=0
async function load(id?:string,quiet=false){
 const current=version,request=++requestVersion,planId=props.record.id
 if(!quiet)loading.value=true
 try{
  const response=await api.planSearch(planId,id)
  if(current!==version||request!==requestVersion)return
  run.value=response.run;history.value=response.history;selectedRun.value=response.run?.id||''
  if(!quiet){selected.value=[];error.value='';notice.value='';if(response.run)perQuery.value=response.run.per_query}
  if(response.run&&taskId.value!==response.run.task_id){taskId.value=response.run.task_id;await store.track(taskId.value)}
 }catch(e){if(current===version&&request===requestVersion)error.value=(e as Error).message}
 finally{if(current===version)loading.value=false}
}
async function start(refresh=false){
 if(blocked.value)return
 const current=version;creating.value=true;error.value='';notice.value=''
 try{
  const result=await api.startPlanSearch(props.record.id,props.record.revision,perQuery.value,refresh)
  if(current!==version)return
  taskId.value=result.task_id;selected.value=[]
  await store.track(result.task_id)
  if(current===version)await load(result.run_id,true)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)creating.value=false}
}
function criterion(id:string){
 const plan=run.value?.plan;if(!plan)return id
 if(id==='T')return '与研究主题相关'
 if(id==='Y')return planVenues(plan.scope).length?'正式发表年份范围':'首次提交年份范围'
 if(id==='L')return '文献语言：'+plan.scope.languages.join('、')
 return (id[0]==='X'?'排除条件：':'纳入条件：')+(id[0]==='X'?plan.scope.exclusion_criteria:plan.scope.inclusion_criteria)[Number(id.slice(1))-1]
}
function toggle(id:string,checked:boolean){if(checked&&selected.value.length>=100)return;selected.value=checked?[...new Set([...selected.value,id])]:selected.value.filter(value=>value!==id)}
async function saveSelected(){
 if(!run.value||!selected.value.length||saving.value)return
 const current=version,papers=run.value.candidates.filter(p=>selected.value.includes(p.candidate_id))
 saving.value=true;error.value='';notice.value=''
 try{
  const result=await api.savePapers(papers)
  if(current===version)notice.value=`已新增 ${result.created} 篇，已有 ${result.existing} 篇，未收藏 ${result.rejected} 篇。可在论文库查看并继续全文分析。`
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)saving.value=false}
}
watch(()=>[props.record.id,props.record.revision,props.initialRunId],()=>{
 version++;requestVersion++;run.value=null;history.value=[];taskId.value='';selected.value=[];error.value='';notice.value='';creating.value=false;saving.value=false
 if(mode()!=='demo')void load(props.initialRunId)
},{immediate:true})
watch(()=>[task.value?.id,task.value?.status,task.value?.stage,task.value?.updated_at],()=>{
 if(task.value&&task.value.search_run_id)void load(task.value.search_run_id,true)
})
onBeforeUnmount(()=>{version++;requestVersion++})
</script>

<template>
 <section class="panel search-workspace">
  <h2>按计划检索与初步筛选</h2>
  <p class="muted">使用已保存的第 {{record.revision}} 版计划，在 {{planVenues(record.plan.scope).length?planVenues(record.plan.scope).map(v=>v.toUpperCase()).join('、')+'（OpenAlex + 官方论文集）':'arXiv'}} 检索、去重，再由当前模型根据标题和摘要逐项初筛。</p>
  <p v-if="disabled" class="note warning">请先保存计划修改，并确保上方目标与计划一致，再开始检索。</p>
  <p v-if="!allowed" class="note warning">该计划未同时包含检索与筛选步骤。请调整研究目标后重新制定计划。</p>
  <div class="search-actions"><label>每个检索词最多返回 <el-input-number v-model="perQuery" :min="1" :max="20" :disabled="active||creating||loading" aria-label="每个检索词结果数"/> 篇</label>
   <el-button type="primary" :disabled="blocked" :loading="creating||active" @click="start()">{{currentRun&&run?.status==='failed'&&run.per_query===perQuery?'继续未完成检索与初筛':'开始检索与初筛'}}</el-button>
   <el-button v-if="currentRun&&run?.status==='succeeded'&&planVenues(record.plan.scope).length" :disabled="blocked" @click="start(true)">重新检索并核验</el-button>
  </div>
  <p class="note">{{record.plan.search_queries.length}} 个检索词，本轮最多返回 {{record.plan.search_queries.length*perQuery}} 条后去重。这与后续深入分析 {{record.plan.scope.paper_limit}} 篇的数量分开；相同计划与配置重复点击会复用已完成结果。</p>
  <p v-if="error" class="error-text" role="alert">{{error}}</p><p v-if="notice" class="search-notice" role="status">{{notice}}</p>
  <TaskProgress v-if="task" :task="task"/>
  <p v-else-if="run?.error" class="error-text">{{run.error.code}} · {{run.error.message}}</p>
  <p v-if="store.error&&taskId" class="error-text">任务进度更新失败：{{store.error}}。可刷新结果或到任务中心查看。</p>
  <div class="search-actions" v-if="history.length"><label>检索记录 <select :value="selectedRun" aria-label="检索记录" @change="load(($event.target as HTMLSelectElement).value)"><option value="">当前保存计划的最新结果</option><option v-for="item in history" :key="item.id" :value="item.id">第 {{item.revision}} 版 · {{statusNames[item.status]}} · {{item.model_name}} · {{new Date(item.created_at).toLocaleString('zh-CN')}}</option></select></label><el-button :disabled="loading" @click="load(selectedRun||undefined)">刷新结果</el-button></div>
  <p v-if="loading" class="note">正在读取检索记录…</p>
  <template v-if="run">
   <p class="scope-note">{{run.notice}}</p>
   <p v-if="!currentRun" class="note warning">当前查看的是第 {{run.revision}} 版计划的历史结果；上方按钮将使用第 {{record.revision}} 版计划。</p>
   <details class="query-log" open><summary>检索记录 · 已完成 {{run.queries.filter(q=>q.status==='succeeded').length}}/{{run.queries.length}} 个检索词</summary><ol><li v-for="q in run.queries" :key="q.number"><code>{{q.query}}</code> · {{q.status==='succeeded'?`匹配 ${q.total} 条，本轮取 ${q.items.length} 条`:'待检索'}}</li></ol></details>
   <p>去重后 {{run.candidates.length}} 篇 · 合并重复 {{run.duplicate_count}} 条 · 跳过无效元数据 {{run.skipped_count}} 条 · 初筛完成 {{Object.keys(run.screenings).length}} 篇</p>
   <div class="search-actions"><label>显示 <select :value="filter" aria-label="筛选状态" @change="filter=($event.target as HTMLSelectElement).value as typeof filter"><option value="all">全部候选（{{run.candidates.length}}）</option><option v-for="(label,key) in labels" :key="key" :value="key">{{label}}（{{count(key)}}）</option></select></label><el-button :disabled="!selected.length||saving" :loading="saving" @click="saveSelected">收藏所选论文（{{selected.length}}）</el-button><RouterLink to="/library">前往论文库 ↗</RouterLink></div>
   <p v-if="!run.candidates.length&&run.status==='succeeded'" class="note">{{planVenues(run.plan.scope).length?'本轮没有符合显示条件的会议论文。若已启用主会过滤，可关闭后查看待核验候选；官方页面暂不可用也可能导致无法确认主会身份。':'本轮检索没有找到候选论文。请检查年份与检索词，修改并保存计划后重新检索。'}}</p>
   <p v-else-if="!visible.length" class="note">当前筛选状态下没有论文。</p>
   <article v-for="paper in visible" :key="paper.candidate_id" class="candidate">
    <div class="candidate-heading"><label><input type="checkbox" :checked="selected.includes(paper.candidate_id)" :disabled="selected.length>=100&&!selected.includes(paper.candidate_id)" :aria-label="`选择论文 ${paper.title}`" @change="toggle(paper.candidate_id,($event.target as HTMLInputElement).checked)"/> 选择</label><el-tag>{{labels[run.screenings[paper.candidate_id]?.decision||'pending']}}</el-tag></div>
    <h3><a :href="paper.url||undefined" target="_blank" rel="noopener noreferrer">{{paper.title}} ↗</a></h3><p class="note">{{paper.year??'年份未知'}} · {{paper.authors.join('、')}} · {{paper.venue_slug?paper.venue_slug.toUpperCase():`arXiv ${paper.arxiv_id}`}} · 来自检索词 {{paper.query_numbers.join('、')}}</p>
    <PublicationStatus :publication="paper.publication"/>
    <details><summary>摘要与逐项筛选依据</summary><AbstractTranslation :title="paper.title" :abstract="paper.abstract"/>
     <p v-if="run.screenings[paper.candidate_id]?.abstract_truncated" class="note">初筛使用摘要前 12000 个字符，剩余内容需人工检查。</p>
     <ul v-if="run.screenings[paper.candidate_id]"><li v-for="check in run.screenings[paper.candidate_id].checks" :key="check.criterion_id"><strong>{{criterion(check.criterion_id)}} · {{verdicts[check.verdict]}}</strong><p>{{check.explanation}}</p><blockquote v-if="check.quote">{{check.quote}}</blockquote></li></ul>
    </details>
   </article>
   <PlanExecutionWorkspace :record="record" :search-run="run" :selected-ids="selected" :disabled="disabled||!currentRun"/>
  </template>
 </section>
</template>

<style scoped>
.search-workspace{padding:26px;margin-bottom:22px}.search-actions{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin:18px 0}.search-actions label{display:flex;align-items:center;gap:8px;font-size:13px;max-width:100%}select{max-width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:5px;background:white;color:#40546d}.scope-note{padding:12px;background:#f4f7fa;border-radius:7px;font-size:13px}.query-log li{margin:10px 0;overflow-wrap:anywhere}.candidate{border-top:1px solid #e5ebf1;padding:20px 0}.candidate-heading{display:flex;justify-content:space-between;align-items:center}.candidate h3{font-size:16px;line-height:1.6}.candidate details{font-size:14px;line-height:1.7}.candidate li{margin-top:16px}.candidate li p{margin:6px 0}.abstract{white-space:pre-wrap}blockquote{margin:8px 0;padding:8px 14px;background:#f8fafc;border-left:3px solid #a7bacb;white-space:pre-wrap}.search-notice{color:#357258}.candidate-heading label{font-size:13px}summary{cursor:pointer}
@media(max-width:650px){.search-workspace{padding:18px}.search-actions label{flex-wrap:wrap}}
</style>
