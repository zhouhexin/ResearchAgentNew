<script setup lang="ts">
import {computed,onBeforeUnmount,ref,toRaw,watch} from 'vue'
import {api,mode} from '../api'
import type {ResearchPlanRecord} from '../types/planning'
import type {AnalysisResult,PaperSummary} from '../types/research'
import type {ResearchExecution} from '../types/execution'
import {useTaskStore} from '../stores/tasks'
import ExecutionResults from './ExecutionResults.vue'
const props=defineProps<{record:ResearchPlanRecord;disabled?:boolean}>()
const store=useTaskStore(),q=ref(''),page=ref(1),total=ref(0),papers=ref<PaperSummary[]>([])
const selection=ref<Record<number,{paper:PaperSummary;analysis_id:string;analyses:AnalysisResult[]}>>({})
const runs=ref<ResearchExecution[]>([]),runId=ref(''),error=ref(''),loading=ref(false),busy=ref(false)
const scope=ref<'full'|'fast'>('full'),autoReview=ref(false)
const selected=computed(()=>Object.values(selection.value)),run=computed(()=>runs.value.find(r=>r.id===runId.value)||runs.value[0])
const active=computed(()=>runs.value.some(r=>['queued','running'].includes(r.status)))
const blocked=computed(()=>props.disabled||mode()==='demo'||busy.value||active.value)
let version=0,listTicket=0,runTicket=0,timer:ReturnType<typeof setTimeout>|undefined
async function loadPapers(){
 const current=version,ticket=++listTicket;loading.value=true
 if(mode()==='demo'){loading.value=false;return}
 try{const response=await api.papers({q:q.value,page:page.value,page_size:20,has_fulltext:true})
  if(current!==version||ticket!==listTicket)return
  papers.value=response.items;total.value=response.total
 }catch(e){if(current===version&&ticket===listTicket)error.value=(e as Error).message}
 finally{if(current===version&&ticket===listTicket)loading.value=false}
}
async function loadRuns(){
 const current=version,ticket=++runTicket;clearTimeout(timer)
 if(mode()==='demo')return
 try{const response=await api.planExecutions(props.record.id)
  if(current!==version||ticket!==runTicket)return
  runs.value=response.items.filter(r=>r.source==='library')
  if(active.value)timer=setTimeout(()=>void loadRuns(),document.hidden?10000:2500)
 }catch(e){if(current===version&&ticket===runTicket)error.value=(e as Error).message}
}
async function selectExecution(id:string){runId.value=id;await loadRuns()}
async function toggle(paper:PaperSummary,checked:boolean){
 if(blocked.value)return
 if(!checked){delete selection.value[paper.id];return}
 if(selected.value.length>=5)return
 const current=version,entry={paper,analysis_id:'',analyses:[] as AnalysisResult[]}
 selection.value[paper.id]=entry
 try{const values=await api.analyses(paper.id)
  if(current!==version||toRaw(selection.value[paper.id])!==entry)return
  selection.value[paper.id].analyses=values.filter(a=>a.is_current!==false)
 }catch(e){if(current===version&&toRaw(selection.value[paper.id])===entry){delete selection.value[paper.id];error.value=(e as Error).message}}
}
async function execute(resume=false){
 if(mode()==='demo'||busy.value||active.value||(resume?!run.value||run.value.status!=='failed':props.disabled||selected.value.length<2||selected.value.length>5))return
 const current=version;busy.value=true;error.value=''
 try{const result=resume?await api.resumePlanExecution(props.record.id,run.value!.id):await api.startLibraryExecution(props.record.id,{
   expected_revision:props.record.revision,papers:selected.value.map(s=>({paper_id:s.paper.id,...(s.analysis_id?{analysis_id:s.analysis_id}:{})})),
   analysis_scope:scope.value,review_after_analysis:autoReview.value})
  if(current!==version)return
  runId.value=result.execution_id;await store.track(result.task_id)
  if(current===version)await loadRuns()
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)busy.value=false}
}
watch(()=>[props.record.id,props.record.revision],()=>{version++;listTicket++;runTicket++;clearTimeout(timer);selection.value={};papers.value=[];runs.value=[];runId.value='';error.value='';busy.value=false;page.value=1;void loadPapers();void loadRuns()},{immediate:true})
onBeforeUnmount(()=>{version++;clearTimeout(timer)})
</script>
<template>
 <section class="panel library-execution" aria-label="已有文献库执行计划">
  <h2>从已有文献库执行</h2>
  <p class="note">选择 2–5 篇已有可读全文的论文，无需外部检索。自动复用符合阅读范围的有效分析，也可指定一个有效分析版本；无合适分析时补齐阅读。选文不代表已通过摘要初筛或满足全部研究条件。</p>
  <p v-if="disabled" class="note">请先保存当前计划修改，再开始新执行；历史执行可按原版本继续。</p>
  <form class="library-controls" @submit.prevent="page=1;loadPapers()"><input :value="q" @input="q=($event.target as HTMLInputElement).value" aria-label="搜索库内论文" placeholder="标题或 DOI"/><el-button native-type="submit" :disabled="loading||mode()==='demo'">查询文献库</el-button></form>
  <p v-if="loading" class="note">正在读取文献库…</p>
  <p v-if="!loading&&!papers.length" class="note">暂无有全文的论文，请先到文献库上传并解析。</p>
  <label v-for="paper in papers" :key="paper.id" class="library-row"><input type="checkbox" :checked="!!selection[paper.id]" :disabled="blocked||(!selection[paper.id]&&selected.length>=5)" :aria-label="`选择库内论文 P${paper.id}`" @change="toggle(paper,($event.target as HTMLInputElement).checked)"/><span>P{{paper.id}} · {{paper.title}}</span></label>
  <div class="library-controls"><el-button :disabled="page<=1||loading" @click="page--;loadPapers()">上一页</el-button><span>第 {{page}} 页 · {{total}} 篇</span><el-button :disabled="page*20>=total||loading" @click="page++;loadPapers()">下一页</el-button></div>
  <div v-for="item in selected" :key="item.paper.id" class="library-row"><span>P{{item.paper.id}} · {{item.paper.title}}</span><select :value="item.analysis_id" @change="item.analysis_id=($event.target as HTMLSelectElement).value" :disabled="blocked" :aria-label="`P${item.paper.id} 分析版本`"><option value="">自动复用有效分析，否则补齐阅读</option><option v-for="analysis in item.analyses" :key="analysis.id" :value="analysis.id">{{analysis.id}} · {{analysis.document.selected_chunks}}/{{analysis.document.total_chunks}} 片段</option></select><el-button :disabled="blocked" @click="toggle(item.paper,false)">移除</el-button></div>
  <div class="library-controls"><label>补齐阅读模式 <select :value="scope" @change="scope=($event.target as HTMLSelectElement).value as typeof scope" :disabled="blocked" aria-label="库内论文阅读模式"><option value="full">全量分析</option><option value="fast">快速阅读</option></select></label><label><input type="checkbox" :checked="autoReview" :disabled="blocked" @change="autoReview=($event.target as HTMLInputElement).checked"/> 分析后自动辅助评审</label></div>
  <p v-if="autoReview" class="note">辅助评审增加逐条模型请求和用量；模型判断供人工核对，失败保留分析。</p>
  <div class="library-controls"><el-button type="primary" :disabled="blocked||selected.length<2||selected.length>5" @click="execute()">确认库内选文并执行（{{selected.length}}）</el-button><el-button v-if="run?.status==='failed'" :disabled="busy||active||mode()==='demo'" @click="execute(true)">继续库内执行未完成部分</el-button><el-button :disabled="mode()==='demo'" @click="loadRuns()">刷新执行记录</el-button></div>
  <p v-if="error" role="alert" class="error-text">{{error}}</p>
  <label v-if="runs.length>1">执行历史 <select :value="runId" @change="runId=($event.target as HTMLSelectElement).value" aria-label="库内执行历史"><option v-for="r in runs" :key="r.id" :value="r.id">第 {{r.revision}} 版 · {{r.status}} · {{r.created_at}}</option></select></label>
  <ExecutionResults v-if="run" :run="run" @execution-selected="selectExecution"/>
 </section>
</template>
<style scoped>.library-controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:16px 0}.library-row{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #edf0f4}.library-row>span{flex:1;min-width:0}.library-row select{max-width:48%}.library-execution input:not([type=checkbox]),select{font:inherit;padding:8px;border:1px solid #cbd5e1;border-radius:4px;max-width:100%}.library-execution>.note{line-height:1.7}</style>
