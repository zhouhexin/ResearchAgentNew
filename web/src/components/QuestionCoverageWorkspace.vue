<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import type {ResearchExecution} from '../types/execution'
import type {CoverageCitation,CoverageResult,CoverageTask} from '../types/coverage'
import type {ReviewCitation} from '../types/research'
import TaskProgress from './TaskProgress.vue'
import EvidenceCitation from './EvidenceCitation.vue'
import RereadSuggestion from './RereadSuggestion.vue'
import SupplementSearch from './SupplementSearch.vue'
const props=defineProps<{execution:ResearchExecution}>()
const emit=defineEmits<{'execution-selected':[executionId:string]}>()
const branch=ref('')
const executionId=computed(()=>branch.value||props.execution.id)
const questions=ref(''),records=ref<CoverageTask[]>([]),selected=ref(''),result=ref<CoverageResult|null>(null)
const busy=ref(false),loading=ref(false),error=ref('')
const current=computed(()=>records.value.find(v=>v.task.id===selected.value)||records.value[0])
const active=computed(()=>records.value.some(v=>['queued','running'].includes(v.task.status)))
const labels={answered:'已回答',partial:'部分回答',unanswered:'未回答',conflicting:'存在冲突'}
let version=0,ticket=0,timer:ReturnType<typeof setTimeout>|undefined
async function load(){
 const epoch=version,id=++ticket;clearTimeout(timer)
 if(mode()==='demo')return
 loading.value=true
 try{const data=await api.coverageTasks(props.execution.plan_id,executionId.value)
  if(epoch!==version||id!==ticket)return
  records.value=data.items
  const task=current.value?.task
  if(task?.status==='succeeded'&&task.result_id&&task.result_id!==result.value?.id){
   const saved=await api.coverageResult(task.result_id)
   if(epoch!==version||id!==ticket)return
   result.value=saved
  }
  if(active.value)timer=setTimeout(()=>void load(),document.hidden?10000:2500)
 }catch(e){if(epoch===version&&id===ticket)error.value=(e as Error).message}
 finally{if(epoch===version&&id===ticket)loading.value=false}
}
async function start(resume=false){
 if(busy.value||active.value||mode()==='demo')return
 const epoch=version;busy.value=true;error.value=''
 try{const response=await api.startCoverage(props.execution.plan_id,executionId.value,resume?{resume_request_id:current.value?.request_id}:{research_questions:questions.value.split('\n').map(s=>s.trim()).filter(Boolean)})
  if(epoch!==version)return
  selected.value=response.task_id;result.value=null;await load()
 }catch(e){if(epoch===version)error.value=(e as Error).message}
 finally{if(epoch===version)busy.value=false}
}
function citation(c:CoverageCitation):ReviewCitation{
 const index=result.value?.reading_coverage.findIndex(p=>p.paper_id===c.paper_id)??0
 return{ref_id:c.ref_id,paper_id:c.paper_id,paper_number:index+1,paper_title:c.paper_title,page_number:c.page_number,
  source_url:'本轮固定原文',footnote_id:c.ref_id,quotes:c.source_spans.map(s=>s.text),quote_kind:'source_chunk'}
}
function showBranch(id:string){version++;ticket++;clearTimeout(timer);branch.value=id;records.value=[];selected.value='';result.value=null;error.value='';busy.value=false;void load()}
function followBranch(id:string){showBranch(id);emit('execution-selected',id)}
function choose(id:string){selected.value=id;result.value=null;void load()}
watch(()=>props.execution.id,()=>{branch.value='';version++;ticket++;clearTimeout(timer);questions.value=props.execution.plan.research_questions.join('\n');records.value=[];selected.value='';result.value=null;error.value='';busy.value=false;void load()},{immediate:true})
onBeforeUnmount(()=>{version++;clearTimeout(timer)})
</script>
<template>
 <section class="coverage-workspace" aria-label="研究问题覆盖检查">
  <h3>研究问题覆盖检查</h3>
  <p v-if="branch" class="note">当前为补读后的新执行分支：{{branch}} <el-button @click="showBranch('')">返回原覆盖报告</el-button></p>
  <p class="note">仅评估本轮固定分析及引用上下文。模型判断待人工核对；评审中的“支持”不等于问题已充分回答。补读和检索建议不会自动执行。</p>
  <label>本次检查的问题（每行一项，最多6项）<textarea :value="questions" :disabled="busy||active" rows="3" maxlength="6000" aria-label="覆盖检查研究问题" @input="questions=($event.target as HTMLTextAreaElement).value"/></label>
  <div class="coverage-actions"><el-button type="primary" :disabled="busy||active||!questions.trim()||mode()==='demo'" @click="start()">生成覆盖报告与补读建议</el-button><el-button v-if="current?.task.status==='failed'" :disabled="busy||active||mode()==='demo'" @click="start(true)">按原输入重试覆盖检查</el-button><el-button :disabled="loading||mode()==='demo'" @click="load()">刷新覆盖报告</el-button></div>
  <p v-if="error" role="alert" class="error-text">{{error}}</p>
  <label v-if="records.length>1">检查历史<select :value="current?.task.id" aria-label="覆盖报告历史" @change="choose(($event.target as HTMLSelectElement).value)"><option v-for="entry in records" :key="entry.task.id" :value="entry.task.id">{{entry.task.created_at}} · {{entry.task.status}}</option></select></label>
  <TaskProgress v-if="current" :task="current.task"/>
  <template v-if="result">
   <p v-if="!result.is_current" class="error-text">这是历史覆盖报告，输入材料已变化，请勿按当前结果直接执行建议。</p>
   <div class="coverage-actions"><a :href="api.coverageFileUrl(result.id,'md')" target="_blank" rel="noopener">导出覆盖报告 Markdown</a><a :href="api.coverageFileUrl(result.id,'json')" target="_blank" rel="noopener">导出覆盖报告 JSON</a></div>
   <p v-for="p in result.reading_coverage" :key="p.paper_id" class="note">P{{p.paper_id}} · {{p.reading_scope}} · 本轮分析 {{p.selected_chunks}} / {{p.total_chunks}} 片段</p>
   <article v-for="q in result.report.questions" :key="q.question_id" class="coverage-question">
    <h4>{{q.question}} · {{labels[q.status]}}</h4><p v-if="q.answer">{{q.answer}}</p><p>{{q.reason}}</p>
    <ul v-if="q.gaps.length"><li v-for="gap in q.gaps" :key="gap">缺口：{{gap}}</li></ul>
    <div v-for="c in q.citations" :key="c.ref_id"><EvidenceCitation :ref-id="c.ref_id" :citation="citation(c)"/><blockquote v-for="span in c.source_spans" :key="span.span_id">{{span.text}}</blockquote></div>
    <div v-for="(suggestion,i) in q.suggestions" :key="i" class="coverage-suggestion"><strong>待人工确认 · {{suggestion.action==='read_existing'?'优先补读已有论文':'后续检索建议'}}</strong><p>{{suggestion.paper_ids.map(p=>'P'+p).join('、')}} {{suggestion.target}}</p><p>{{suggestion.rationale}}</p><RereadSuggestion v-if="suggestion.action==='read_existing'" :result="result" :question="q" :suggestion-index="i" @rechecked="followBranch"/><SupplementSearch v-if="suggestion.action==='search_later'" :result="result" :question="q" :suggestion-index="i" @rechecked="followBranch"/></div>
   </article>
   <p v-for="limit in result.report.limitations" :key="limit" class="note">限制：{{limit}}</p>
  </template>
 </section>
</template>
<style scoped>.coverage-workspace{border-top:1px solid #dfe7ef;padding-top:18px;margin-top:20px}.coverage-workspace>label{display:grid;gap:8px;margin:14px 0;font-size:13px}.coverage-workspace textarea,.coverage-workspace select{font:inherit;padding:9px;border:1px solid #ccd6e2;border-radius:4px;max-width:100%;box-sizing:border-box}.coverage-actions{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:16px 0}.coverage-question{border-top:1px solid #e5ebf1;padding:14px 0;line-height:1.8}.coverage-question blockquote{white-space:pre-wrap;border-left:3px solid #cbd5e1;margin:12px 0;padding-left:14px}.coverage-suggestion{background:#f4f7fa;padding:12px;margin:12px 0}</style>
