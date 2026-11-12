<script setup lang="ts">
import {ref,onMounted,computed,watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {ElMessage,ElMessageBox} from 'element-plus'
import {api} from '../api'
import {categories} from '../types/research'
import type {PaperSummary,AnalysisResult,TaskInfo} from '../types/research'
import {download} from '../api/utils'
import {useTaskStore} from '../stores/tasks'
import {useResource} from '../composables/useResource'
import AsyncState from '../components/AsyncState.vue'
import AnalysisCard from '../components/AnalysisCard.vue'
import TaskProgress from '../components/TaskProgress.vue'
import ReadingCoverage from '../components/ReadingCoverage.vue'
const route=useRoute(),router=useRouter(),papers=ref<PaperSummary[]>([]),paperId=ref<number>(Number(route.query.paperId)||0),maxChunks=ref(6),scope=ref<'full'|'sample'|'fast'>('full'),fastMaxChunks=ref(18),batchSize=ref(2),history=ref<AnalysisResult[]>([]),result=ref<AnalysisResult|null>(null),busy=ref(false),taskId=ref(''),tasks=useTaskStore()
const {loading,error,load}=useResource(null)
const task=computed(()=>tasks.tasks[taskId.value])
const canResume=computed(()=>task.value?.status==='failed'&&!!task.value.batch_run_key&&task.value.analysis_scope===scope.value&&task.value.batch_size===batchSize.value&&(scope.value==='full'||task.value.requested_max_chunks===(scope.value==='fast'?fastMaxChunks.value:maxChunks.value))&&task.value.error?.code!=='DOCUMENT_CHANGED')
let selectionVersion=0
async function refresh(){
 await load(async()=>{
  papers.value=(await api.papers({has_fulltext:true,page_size:100})).items
  if(route.query.analysisId){
   const selected=await api.analysis(String(route.query.analysisId))
   paperId.value=selected.paper.id
   history.value=await api.analyses(selected.paper.id)
   result.value=selected
   await restoreProgress(await api.analysisProgress(selected.paper.id))
  }else if(paperId.value){await changePaper()}
  return null
 })
}
async function restoreProgress(latest:TaskInfo|null){
 if(latest&&['queued','running','failed'].includes(latest.status)){
  scope.value=latest.analysis_scope||'full';batchSize.value=latest.batch_size||2;maxChunks.value=latest.requested_max_chunks||6;fastMaxChunks.value=latest.requested_max_chunks||18
  taskId.value=latest.id;await tasks.track(latest.id)
 }
}
async function changePaper(){
 const current=++selectionVersion,selectedId=paperId.value
 taskId.value='';result.value=null;history.value=[]
 if(!selectedId)return
 void router.replace({query:{paperId:String(selectedId)}})
 try{
  const [records,latest]=await Promise.all([api.analyses(selectedId),api.analysisProgress(selectedId)])
  if(current!==selectionVersion||paperId.value!==selectedId)return
  history.value=records;result.value=records.find(a=>a.is_current!==false)||records[0]||null
  await restoreProgress(latest)
 }catch(e){if(current===selectionVersion)ElMessage.error((e as Error).message)}
}
async function start(continueFailed=false){
 if(!paperId.value||busy.value)return
 const selectedId=paperId.value,current=selectionVersion
 if(result.value&&!continueFailed){try{await ElMessageBox.confirm('将重新处理所有批次，已有报告会保留。是否继续？','重新分析',{confirmButtonText:'创建任务',cancelButtonText:'取消'})}catch{return}}
 if(selectedId!==paperId.value||current!==selectionVersion)return
 busy.value=true
 try{
  const response=await api.startAnalysis(selectedId,maxChunks.value,{scope:scope.value,fast_max_chunks:fastMaxChunks.value,batch_size:batchSize.value,force_restart:!!result.value&&!continueFailed,...(continueFailed?{resume_task_id:taskId.value}:{})})
  if(response.reused)ElMessage.info('这篇论文已有进行中的分析，已切换到该任务')
  if(response.source_reparsed)ElMessage.info('已更新 PDF 解析与分片，本次将按新片段分析，旧批次进度保留但不混用')
  if(current===selectionVersion&&paperId.value===selectedId)taskId.value=response.task_id
  await tasks.track(response.task_id)
 }catch(e){ElMessage.error((e as Error).message)}finally{busy.value=false}
}
watch(()=>task.value?.status,async status=>{
 const finished=task.value
 if(status!=='succeeded'||!finished?.result_id)return
 const selectedId=paperId.value,current=selectionVersion
 if(finished.paper_id!==undefined&&finished.paper_id!==selectedId)return
 try{
  const [selected,records]=await Promise.all([api.analysis(finished.result_id),api.analyses(selectedId)])
  if(current!==selectionVersion||paperId.value!==selectedId)return
  result.value=selected;history.value=records
  void router.replace({query:{paperId:String(selectedId),analysisId:selected.id}})
  ElMessage.success('分析和原文证据核验已完成')
 }catch(e){if(current===selectionVersion)ElMessage.error((e as Error).message)}
})
onMounted(refresh)
</script>
<template><div class="page-heading"><div><div class="eyebrow">READ WITH EVIDENCE</div><h1>论文分析</h1><p>从原文提取研究问题、方法与发现，让结论有迹可循。</p></div><el-button v-if="result" @click="download(JSON.stringify(result,null,2),`paper_analysis_${result.paper.id}_${result.document.id}.json`)">下载分析 JSON ↓</el-button></div><AsyncState :loading="loading" :error="error" @retry="refresh"><section class="panel analysis-controls"><label>选择论文<el-select v-model="paperId" placeholder="请选择已获取全文的论文" filterable @change="changePaper"><el-option v-for="p in papers" :key="p.id" :label="p.title" :value="p.id"/></el-select></label><label>阅读范围<el-select v-model="scope" aria-label="阅读范围"><el-option label="全文分批分析" value="full"/><el-option label="快速阅读（正文关键章节优先）" value="fast"/><el-option label="抽样分批分析" value="sample"/></el-select></label><label>每批片段数<el-input-number v-model="batchSize" :min="1" :max="10" aria-label="每批片段数"/></label><label v-if="scope==='fast'">快速阅读片段上限<el-input-number v-model="fastMaxChunks" :min="8" :max="50" aria-label="快速阅读片段上限"/></label><label v-if="scope==='sample'">抽样片段上限<el-input-number v-model="maxChunks" :min="2" :max="50" aria-label="阅读片段上限"/></label><el-button type="primary" :disabled="!paperId||['queued','running'].includes(task?.status||'')" :loading="busy" @click="start(false)">{{result?'重新分析':'开始分析'}} ↗</el-button></section><p class="note">默认按顺序分批处理全文，每批通过核验后保存。长论文会调用模型多次，时间和费用会增加；抽样模式只分析选中的片段；快速阅读优先关键章节，默认最多18个片段，参考文献和附录不主动纳入。合并报告不再额外调用模型。</p><TaskProgress v-if="task" :task="task"/><ReadingCoverage v-if="task?.reading_selection&&task.status!=='succeeded'" :selection="task.reading_selection"/><el-button v-if="canResume" type="primary" plain :loading="busy" @click="start(true)">继续未完成批次</el-button><p v-if="task?.status==='failed'" class="note">已通过核验的批次保留。继续时跳过已完成部分；原文、模型或分批设置改变后，需要重新开始。</p><div v-if="result"><div class="result-toolbar"><div><strong>{{result.paper.title}}</strong><span class="muted">阅读 {{result.document.selected_chunks}} / {{result.document.total_chunks}} 个片段</span></div><span class="status-chip blue"><i/>结构化分析</span></div><p v-if="result.is_current===false" class="note warning" role="alert">这份报告对应的原文或证据片段已更新，属于历史分析。请重新分析后用于当前论文。</p><ReadingCoverage :selection="result.reading_selection"/><div class="version-bar"><code>文档 {{result.document.id}}</code><span>SHA256 {{result.document.content_sha256.slice(0,20)}}…</span></div><div class="analysis-grid"><AnalysisCard v-for="(label,k,i) in categories" :key="k" :number="i+1" :title="label" :findings="result.analysis[k]" :paper-id="result.paper.id"/></div><div v-if="history.length>1" class="panel prose-panel"><h3>分析历史</h3><el-button v-for="a in history" :key="a.id" @click="result=a">{{a.created_at?new Date(a.created_at).toLocaleString('zh-CN'):a.id}}{{a.batch_summary?.scope==='fast'?' · 快速阅读':''}}{{a.is_current===false?' · 历史原文':''}}</el-button></div></div><AsyncState v-else-if="!task" empty title="选择论文，开始深入阅读" description="分析结果会保留原文证据编号，点击即可回到来源。"><template #action><RouterLink to="/library">打开文献库 →</RouterLink></template></AsyncState></AsyncState></template>

<style scoped>
.analysis-controls{flex-wrap:wrap}.analysis-controls>label{min-width:135px}
</style>
