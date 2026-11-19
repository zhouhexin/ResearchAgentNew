<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import type {CoverageAssessment,CoverageResult} from '../types/coverage'
import type {ResearchSearchRun} from '../types/research-search'
import type {TaskInfo,SearchPaper} from '../types/research'
import TaskProgress from './TaskProgress.vue'
import SupplementReadings from './SupplementReadings.vue'
const emit=defineEmits<{rechecked:[executionId:string]}>()
const props=defineProps<{result:CoverageResult;question:CoverageAssessment;suggestionIndex:number}>()
const queries=ref(''),perQuery=ref(5),calls=ref(8),seconds=ref(300),busy=ref(false),error=ref(''),notice=ref('')
const entries=ref<{task:TaskInfo;request_id:string;budget:{calls_used:number;max_calls:number}|null}[]>([])
const run=ref<ResearchSearchRun|null>(null)
const active=computed(()=>entries.value.some(e=>['queued','running'].includes(e.task.status)))
let epoch=0,ticket=0,timer:ReturnType<typeof setTimeout>|undefined
async function load(){
 const current=epoch,id=++ticket;clearTimeout(timer)
 if(mode()==='demo')return
 try{const response=await api.supplementSearchHistory(props.result.id)
  if(current!==epoch||id!==ticket)return
  entries.value=response.items.filter(e=>e.task.supplement_question_id===props.question.question_id&&e.task.supplement_suggestion_index===props.suggestionIndex)
  if(entries.value[0]){const value=await api.supplementSearch(entries.value[0].request_id);if(current!==epoch||id!==ticket)return;run.value=value.run}
  if(active.value)timer=setTimeout(()=>void load(),2500)
 }catch(e){if(current===epoch&&id===ticket)error.value=(e as Error).message}
}
async function start(requestId?:string){
 if(busy.value||active.value||mode()==='demo')return
 const current=epoch;busy.value=true;error.value=''
 try{if(requestId)await api.resumeSupplementSearch(requestId)
  else await api.startSupplementSearch(props.result.id,{question_id:props.question.question_id,suggestion_index:props.suggestionIndex,queries:queries.value.split('\n').map(q=>q.trim()).filter(Boolean),per_query:Number(perQuery.value),max_calls:Number(calls.value),total_seconds:Number(seconds.value),source:'arxiv'})
  if(current===epoch)await load()
 }catch(e){if(current===epoch)error.value=(e as Error).message}
 finally{if(current===epoch)busy.value=false}
}
async function save(p:SearchPaper){const current=epoch;busy.value=true;error.value='';try{const response=await api.savePapers([p]);if(current===epoch)notice.value=`收藏完成：新增 ${response.created}，已有 ${response.existing}，拒绝 ${response.rejected}。可从文献库人工选择后续下载与分析。`}catch(e){if(current===epoch)error.value=(e as Error).message}finally{if(current===epoch)busy.value=false}}
watch(()=>[props.result.id,props.question.question_id,props.suggestionIndex],()=>{epoch++;ticket++;clearTimeout(timer);queries.value='';entries.value=[];run.value=null;busy.value=false;error.value='';notice.value='';void load()},{immediate:true})
onBeforeUnmount(()=>{epoch++;ticket++;clearTimeout(timer)})
</script>
<template>
 <div class="supplement-search" aria-label="确认受限补检索">
  <p class="note">仅 arXiv 实时检索和摘要初筛；优先补读已有材料。原计划限定其他来源时会拒绝。候选不代表正式发表或已回答问题，不自动下载、分析。</p>
  <label>确认检索词（每行一组，最多2组；建议英文）<textarea :value="queries" rows="2" maxlength="1001" :disabled="busy||active" @input="queries=($event.target as HTMLTextAreaElement).value"/></label>
  <div class="controls"><label>每组候选上限<input type="number" min="1" max="10" :value="perQuery" :disabled="busy||active" @input="perQuery=Number(($event.target as HTMLInputElement).value)"/></label><label>工具＋模型调用上限<input type="number" min="2" max="24" :value="calls" :disabled="busy||active" @input="calls=Number(($event.target as HTMLInputElement).value)"/></label><label>期限（秒）<input type="number" min="10" max="1800" :value="seconds" :disabled="busy||active" @input="seconds=Number(($event.target as HTMLInputElement).value)"/></label><el-button :disabled="busy||active||!queries.trim()||!result.is_current||mode()==='demo'" @click="start()">确认范围与预算，补检索</el-button></div>
  <p class="note">最多一轮、20条原始候选。检索、初筛及失败调用共同扣减；续跑不重置预算与期限。迟到响应不发布，网络请求不保证立即取消。</p>
  <p v-if="error" role="alert" class="error-text">{{error}}</p><p v-if="notice" role="status">{{notice}}</p>
  <div v-for="entry in entries" :key="entry.task.id"><TaskProgress :task="entry.task"/><p v-if="entry.budget">调用 {{entry.budget.calls_used}} / {{entry.budget.max_calls}}</p><el-button v-if="entry.task.status==='failed'&&entry.task.error?.retryable" :disabled="busy||active" @click="start(entry.request_id)">继续原补检索（预算不重置）</el-button></div>
  <SupplementReadings v-if="entries[0]?.task.status==='succeeded'" :request-id="entries[0].request_id" :current="result.is_current" @rechecked="emit('rechecked',$event)"/>
  <template v-if="run"><p>{{run.notice}}</p><p>候选 {{run.candidates.length}} 条，已初筛 {{Object.keys(run.screenings).length}} 条。仅限已确认的查询范围。</p><article v-for="p in run.candidates" :key="p.candidate_id"><strong>{{p.title}}</strong><p>{{run.screenings[p.candidate_id]?.decision==='included'?'建议纳入':run.screenings[p.candidate_id]?.decision==='excluded'?'建议排除':'待人工确认'}}</p><details><summary>原始摘要和初筛依据</summary><p class="abstract">{{p.abstract||'暂无摘要'}}</p><p v-for="c in run.screenings[p.candidate_id]?.checks||[]" :key="c.criterion_id">{{c.verdict}} · {{c.explanation}} <q v-if="c.quote">{{c.quote}}</q></p></details><el-button :disabled="busy||active" @click="save(p)">收藏此候选</el-button></article></template>
 </div>
</template>
<style scoped>.supplement-search{border-top:1px solid #dbe3eb;padding-top:12px}.supplement-search label{display:grid;gap:6px;font-size:13px}.controls{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin:12px 0}.supplement-search input,.supplement-search textarea{font:inherit;border:1px solid #cbd5e1;border-radius:4px;padding:7px;max-width:100%;box-sizing:border-box}.supplement-search article{border-top:1px solid #dbe3eb;padding:12px 0}.abstract{white-space:pre-wrap}</style>
