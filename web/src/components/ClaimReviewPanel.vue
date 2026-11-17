<script setup lang="ts">
import {ref,computed,watch} from 'vue'
import {api,mode} from '../api'
import type {AnalysisResult,ReviewCitation} from '../types/research'
import {categories} from '../types/research'
import type {ClaimReviewRecord} from '../types/claim-review'
import {claimVerdicts} from '../types/claim-review'
import {useTaskStore} from '../stores/tasks'
import EvidenceCitation from './EvidenceCitation.vue'
import TaskProgress from './TaskProgress.vue'
const props=defineProps<{analysis:AnalysisResult;initialReviewId?:string}>()
const emit=defineEmits<{selected:[reviewId:string]}>()
const records=ref<ClaimReviewRecord[]>([]),selected=ref(''),loading=ref(false),starting=ref(false),error=ref('')
const tasks=useTaskStore(),record=computed(()=>records.value.find(r=>r.id===selected.value))
const task=computed(()=>record.value?(tasks.tasks[record.value.task.id]||record.value.task):null)
const active=computed(()=>['queued','running'].includes(task.value?.status||''))
let version=0,detailVersion=0
async function refresh(){
 const current=++version,analysisId=props.analysis.id
 detailVersion++;loading.value=true;error.value=''
 if(mode()==='demo'){records.value=[];selected.value='';loading.value=false;return}
 try{
  const response=await api.claimReviews(analysisId)
  if(current!==version||analysisId!==props.analysis.id)return
  records.value=response.items
  if(!records.value.some(r=>r.id===selected.value))selected.value=records.value.find(r=>r.id===props.initialReviewId)?.id||records.value[0]?.id||''
  const running=record.value?.task
  if(running&&['queued','running'].includes(running.status))await tasks.track(running.id)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)loading.value=false}
}
async function refreshDetail(){
 const current=++detailVersion,id=selected.value,analysisId=props.analysis.id
 if(!id||mode()==='demo')return
 try{
  const value=await api.claimReview(id)
  if(current!==detailVersion||id!==selected.value||analysisId!==props.analysis.id)return
  records.value=records.value.map(r=>r.id===id?value:r)
  error.value=''
 }catch(e){if(current===detailVersion)error.value=(e as Error).message}
}
async function start(){
 if(starting.value||active.value||mode()==='demo'||props.analysis.is_current===false)return
 const analysisId=props.analysis.id,current=version
 starting.value=true;error.value=''
 try{
  const response=await api.startClaimReview(analysisId)
  if(analysisId!==props.analysis.id||current!==version)return
  selected.value=response.review_id
  await refresh()
  if(analysisId===props.analysis.id)await tasks.track(response.task_id)
 }catch(e){if(analysisId===props.analysis.id)error.value=(e as Error).message}
 finally{starting.value=false}
}
watch(()=>props.analysis.id,()=>{records.value=[];selected.value='';void refresh()},{immediate:true})
watch(()=>task.value?.updated_at,()=>{void refreshDetail()})
watch(selected,async()=>{if(selected.value)emit('selected',selected.value);const t=record.value?.task;if(t&&['queued','running'].includes(t.status))await tasks.track(t.id)})
const categoryName=(key:string)=>categories[key as keyof typeof categories]||key
function citation(evidenceId:string):ReviewCitation|undefined{
 const value=record.value,source=value?.items.flatMap(item=>item.evidence).find(e=>e.evidence_id===evidenceId)
 if(!value||!source)return
 return{ref_id:`P${value.paper_id}:${evidenceId}`,paper_id:value.paper_id,paper_number:1,paper_title:props.analysis.paper.title,
  page_number:source.page_number,source_url:props.analysis.document.source_url,footnote_id:`claim-${evidenceId}`,
  quotes:[source.context],quote_kind:'source_chunk'}
}
</script>

<template>
 <section class="panel claim-panel" aria-label="辅助语义评审">
  <div class="flex-between claim-heading"><div><h3>辅助语义评审</h3><p class="note">逐条核对结论是否得到所给原文支持。模型判断供人工核查，原分析保持不变。</p></div>
   <div class="button-row"><el-button :disabled="loading||starting" @click="refresh">刷新评审</el-button><el-button type="primary" :loading="starting" :disabled="loading||active||mode()==='demo'||analysis.is_current===false" @click="start">{{task?.status==='failed'?'继续未完成评审':'辅助语义评审'}}</el-button></div>
  </div>
  <p class="note">支持带页码的 PDF 分析。新评审只让模型选择原文编号，引用由后端直接提取。每条结论联合核对全部证据；快速阅读的评审仍只覆盖本次分析范围。</p>
  <p v-if="mode()==='demo'" class="note">切换到真实 API 数据后，可发起评审。</p>
  <p v-if="analysis.is_current===false" class="note warning">当前为旧版分析，请重新分析后发起评审。历史评审仍可查看。</p>
  <p v-if="error" class="inline-error" role="alert">{{error}}</p>
  <p v-if="loading" class="note" role="status">正在读取评审记录…</p>
  <el-select v-if="records.length>1" v-model="selected" aria-label="选择评审历史"><el-option v-for="r in records" :key="r.id" :value="r.id" :label="`${new Date(r.created_at).toLocaleString('zh-CN')} · ${r.model_name} · ${r.completed}/${r.total} · ${r.quote_protocol==='references-v2'?'原文提取':'旧版引用'}`"/></el-select>
  <template v-if="record">
   <p class="note">已完成 {{record.completed}} / {{record.total}} 条 · {{record.model_name}}</p>
   <p v-if="record.legacy_reused" class="note">复用旧版已核验的 {{record.legacy_reused}} 条结果，保留原判断，未重新调用模型；其余项目采用原文编号评审。</p>
   <p v-if="!record.is_current" class="note warning" role="alert">原文、证据或分析版本已变化，这份评审属于历史记录。</p>
   <TaskProgress v-if="task" :task="task"/>
   <article v-for="item in record.items" :key="item.sample_id" class="claim-item">
    <div class="claim-label"><span class="muted">{{categoryName(item.category)}} · {{item.finding_index+1}}</span><el-tag v-if="item.status==='succeeded'&&item.review" :type="item.review.verdict==='supported'?'success':item.review.verdict==='contradicted'?'danger':'warning'" effect="plain">模型评审：{{claimVerdicts[item.review.verdict]}}</el-tag><el-tag v-else type="info" effect="plain">{{item.status==='not_attempted'?'尚未评审':'评审未完成'}}</el-tag></div>
    <p>{{item.statement}}</p>
    <p v-if="item.quote_protocol==='legacy-import'" class="note">来自已核验的旧版评审。</p>
    <template v-if="item.status==='succeeded'&&item.review"><p class="claim-reason">{{item.review.reason}}</p><div v-for="assessment in item.review.evidence_assessments" :key="assessment.evidence_id" class="claim-source"><EvidenceCitation v-if="record.is_current&&analysis.is_current!==false" :ref-id="`P${record.paper_id}:${assessment.evidence_id}`" :citation="citation(assessment.evidence_id)"/><code v-else>{{assessment.evidence_id}} · 历史引文</code><p>{{assessment.reason}}</p><blockquote v-for="(quote,i) in assessment.quoted_spans" :key="i">{{quote}}</blockquote></div></template>
    <p v-if="item.error" class="error-text">{{item.error.code}} · {{item.error.message}}</p>
    <details><summary>查看送评引文与上下文</summary><div v-for="source in item.evidence" :key="source.evidence_id" class="claim-source"><EvidenceCitation v-if="record.is_current&&analysis.is_current!==false" :ref-id="`P${record.paper_id}:${source.evidence_id}`" :citation="citation(source.evidence_id)"/><code v-else>{{source.evidence_id}} · 历史引文</code><span class="muted"> · 第 {{source.page_number}} 页</span><p class="note">送评引句（同片段多条引句时使用完整原文片段）</p><blockquote>{{source.quote}}</blockquote><p class="note">送评原文片段</p><pre>{{source.context}}</pre></div></details>
   </article>
  </template>
  <p v-else-if="!loading&&!error" class="note">尚未发起辅助评审。将保存每条已完成结果，失败后可继续，不重做论文分析。</p>
 </section>
</template>

<style scoped>
.claim-panel{padding:22px;margin:22px 0}.claim-heading{gap:18px;flex-wrap:wrap}.claim-heading h3{margin:0}.claim-heading .note{margin-bottom:0}.claim-item{border-top:1px solid var(--border,#e5eaf0);padding:20px 0}.claim-item:last-child{padding-bottom:0}.claim-label{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.claim-item p{line-height:1.8}.claim-reason{color:#54657a}.claim-source{margin:12px 0}.claim-source p{margin:8px 0}blockquote{margin:10px 0;padding:10px 14px;border-left:3px solid #d8e3ed;background:#f8fafc;white-space:pre-wrap;font-size:13px;line-height:1.8}summary{cursor:pointer;color:#596b82;font-size:13px}pre{font:inherit;font-size:13px;line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;padding:14px}
</style>
