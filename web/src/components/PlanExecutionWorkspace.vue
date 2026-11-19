<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import type {ResearchPlanRecord} from '../types/planning'
import type {ResearchSearchRun} from '../types/research-search'
import type {ExecutionStep,ResearchExecution} from '../types/execution'
import {useTaskStore} from '../stores/tasks'
import TaskProgress from './TaskProgress.vue'
import ReadingCoverage from './ReadingCoverage.vue'
import ExecutionResults from './ExecutionResults.vue'
const props=defineProps<{record:ResearchPlanRecord;searchRun:ResearchSearchRun;selectedIds:string[];disabled?:boolean}>()
const autoReview=ref(false)
const readingMode=ref<'full'|'fast'>('full')
const store=useTaskStore(),runs=ref<ResearchExecution[]>([]),selectedRun=ref(''),loading=ref(false),submitting=ref(false),error=ref('')
const run=computed(()=>runs.value.find(r=>r.id===selectedRun.value)||runs.value[0])
const active=computed(()=>runs.value.some(r=>['queued','running'].includes(r.status)))
const supported=computed(()=>['acquire_fulltext','analyze_papers'].every(a=>props.searchRun.plan.steps.some(s=>s.action===a)))
const canStart=computed(()=>mode()!=='demo'&&!props.disabled&&!submitting.value&&!active.value&&props.searchRun.status==='succeeded'&&props.searchRun.revision===props.record.revision&&supported.value&&props.selectedIds.length>=2&&props.selectedIds.length<=5)
const names={pending:'待执行',queued:'排队中',running:'进行中',succeeded:'已完成',failed:'失败',cancelled:'已取消'}
const label=(s:ExecutionStep)=>names[s.status]+(s.status==='succeeded'&&s.reused?' · 复用已有结果':'')
let version=0,request=0,timer:ReturnType<typeof setTimeout>|undefined
async function load(quiet=false){
 const current=version,ticket=++request
 clearTimeout(timer)
 if(mode()==='demo')return
 if(!quiet)loading.value=true
 try{
  const response=await api.planExecutions(props.record.id,props.searchRun.id)
  if(current!==version||ticket!==request)return
  runs.value=response.items;error.value=''
  if(active.value)timer=setTimeout(()=>void load(true),document.hidden?10000:2500)
 }catch(e){if(current===version&&ticket===request)error.value=(e as Error).message}
 finally{if(current===version&&ticket===request)loading.value=false}
}
async function selectExecution(id:string){selectedRun.value=id;await load(true)}
async function execute(resume=false){
 if(submitting.value||mode()==='demo'||(resume?(!run.value||run.value.status!=='failed'||active.value):!canStart.value))return
 const current=version;submitting.value=true;error.value=''
 try{
  const response=resume?await api.resumePlanExecution(props.record.id,run.value!.id):await api.startPlanExecution(props.record.id,{expected_revision:props.record.revision,search_run_id:props.searchRun.id,candidate_ids:[...props.selectedIds],...(autoReview.value?{review_after_analysis:true}:{}),...(readingMode.value==='fast'?{analysis_scope:'fast' as const}:{})})
  if(current!==version)return
  selectedRun.value=response.execution_id
  await store.track(response.task_id)
  if(current===version)await load(true)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)submitting.value=false}
}
watch(()=>[props.record.id,props.record.revision,props.searchRun.id],()=>{version++;request++;clearTimeout(timer);runs.value=[];selectedRun.value='';error.value='';submitting.value=false;loading.value=false;void load()},{immediate:true})
onBeforeUnmount(()=>{version++;request++;clearTimeout(timer)})
</script>
<template>
 <section class="execution-workspace" aria-label="本轮全文与分析">
  <h3>本轮全文与分析</h3>
  <p>从上方候选中确认 2–5 篇论文，系统将收藏论文、获取并解析全文，再按所选阅读模式逐篇分析与原文引用核验。</p>
  <p class="note">已选 {{selectedIds.length}} 篇；计划建议 {{searchRun.plan.scope.paper_limit}} 篇。本轮以你确认的选择为准。已有有效全文与完整分析会复用，失败可继续。</p>
  <p v-if="!supported" class="note">当前计划未包含全文获取和单篇分析步骤，请修改计划并保存、重新执行检索后再开始。</p>
  <div class="execution-actions"><label>阅读模式 <select :value="readingMode" :disabled="active||submitting" aria-label="计划论文阅读模式" @change="readingMode=($event.target as HTMLSelectElement).value as typeof readingMode"><option value="full">全量分析</option><option value="fast">快速阅读 · 正文关键章节优先</option></select></label>
   <label><input :checked="autoReview" @change="autoReview=($event.target as HTMLInputElement).checked" type="checkbox" :disabled="active||submitting" aria-label="分析后自动辅助评审"/> 分析后自动辅助评审</label>
   <el-button type="primary" :disabled="!canStart" :loading="submitting" @click="execute()">确认所选论文并执行（{{selectedIds.length}}）</el-button>
   <el-button v-if="run?.status==='failed'" :disabled="submitting||active||mode()==='demo'" @click="execute(true)">继续本轮未完成部分</el-button>
   <el-button :disabled="loading||mode()==='demo'" @click="load()">刷新执行进度</el-button>
  </div>
  <p v-if="autoReview" class="note">每篇分析完成后会逐条调用模型辅助评审，增加等待时间和用量。评审失败保留分析，只继续未完成评审；模型判断不代表人工确认。须启用完整运行配置固定。</p>
  <p v-if="readingMode==='fast'" class="note">快速阅读默认最多18个片段，优先覆盖正文关键章节；结果会标明覆盖范围，后续可另选全量模式补读。</p>
  <p v-if="disabled||searchRun.revision!==record.revision" class="note">新执行须使用已保存的当前计划版本；历史执行继续使用其原计划和选文。</p>
  <p v-if="error" role="alert" class="error-text">{{error}}</p>
  <template v-if="run">
   <label v-if="runs.length>1" class="execution-history">执行记录 <select :value="run.id" aria-label="全文分析执行记录" @change="selectedRun=($event.target as HTMLSelectElement).value"><option v-for="item in runs" :key="item.id" :value="item.id">第 {{item.revision}} 版 · {{names[item.status]}} · {{new Date(item.created_at).toLocaleString('zh-CN')}}</option></select></label>
   <ExecutionResults :run="run" @execution-selected="selectExecution"/>
  </template>
  <p v-else class="note">{{loading?'正在读取执行记录…':'尚未开始本轮全文与分析。'}}</p>
 </section>
</template>
<style scoped>
.execution-workspace{border-top:1px solid #e1e7ef;margin-top:24px;padding-top:20px}.execution-workspace>p{font-size:13px;line-height:1.7}.execution-actions{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}.execution-paper{border-top:1px solid #edf0f4;padding:12px 0}.execution-paper h4{font-size:14px;line-height:1.7;margin:8px 0}.execution-step{font-size:13px;line-height:1.7}.execution-step a{margin-left:12px}.execution-history{font-size:13px;display:flex;align-items:center;gap:10px}.execution-history select{max-width:100%;padding:7px;border:1px solid #cbd5e1;border-radius:4px;background:white}
</style>
