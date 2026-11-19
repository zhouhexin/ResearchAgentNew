<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import type {CoverageAssessment,CoverageResult} from '../types/coverage'
import type {RereadPreview,RereadTask} from '../types/reread'
import TaskProgress from './TaskProgress.vue'
const props=defineProps<{result:CoverageResult;question:CoverageAssessment;suggestionIndex:number}>()
const emit=defineEmits<{rechecked:[executionId:string]}>()
const suggestion=computed(()=>props.question.suggestions[props.suggestionIndex]!)
const paper=ref(0),locator=ref(''),preview=ref<RereadPreview|null>(null),selected=ref<string[]>([])
const calls=ref(8),seconds=ref(600),busy=ref(false),error=ref(''),records=ref<RereadTask[]>([])
const active=computed(()=>records.value.some(v=>['queued','running'].includes(v.task.status)))
const chosenChars=computed(()=>preview.value?.candidates.filter(c=>selected.value.includes(c.evidence_id)).reduce((sum,c)=>sum+c.characters,0)||0)
let epoch=0,timer:ReturnType<typeof setTimeout>|undefined
async function load(){
 const version=epoch;clearTimeout(timer)
 if(mode()==='demo')return
 try{const response=await api.rereadHistory(props.result.id)
  if(version!==epoch)return
  records.value=response.items.filter(v=>v.task.paper_id===paper.value&&v.task.reread_question_id===props.question.question_id&&v.task.reread_suggestion_index===props.suggestionIndex)
  if(active.value)timer=setTimeout(()=>void load(),2500)
 }catch(e){if(version===epoch)error.value=(e as Error).message}
}
async function locate(){
 const version=epoch;busy.value=true;error.value=''
 try{const value=await api.rereadPreview(props.result.id,{question_id:props.question.question_id,suggestion_index:props.suggestionIndex,paper_id:paper.value,locator:locator.value})
  if(version===epoch){preview.value=value;selected.value=[]}
 }catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
function toggle(id:string,checked:boolean){selected.value=checked?[...selected.value,id]:selected.value.filter(v=>v!==id)}
async function confirm(){
 if(!preview.value||!selected.value.length||active.value||busy.value)return
 const version=epoch;busy.value=true;error.value=''
 try{await api.startReread({preview_id:preview.value.id,evidence_ids:selected.value,max_calls:Number(calls.value),total_seconds:Number(seconds.value)});if(version===epoch)await load()}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function resume(entry:RereadTask){
 const version=epoch;busy.value=true;error.value=''
 try{await api.resumeReread(entry.request_id);if(version===epoch)await load()}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function recheck(entry:RereadTask){
 const version=epoch;busy.value=true;error.value=''
 try{const response=await api.recheckReread(entry.task.id);if(version===epoch)emit('rechecked',response.execution_id)}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
watch(()=>[props.result.id,props.question.question_id,props.suggestionIndex],()=>{epoch++;clearTimeout(timer);paper.value=suggestion.value.paper_ids[0]||0;preview.value=null;selected.value=[];records.value=[];error.value='';busy.value=false;void load()},{immediate:true})
watch(paper,()=>{epoch++;preview.value=null;selected.value=[];busy.value=false;void load()})
onBeforeUnmount(()=>{epoch++;clearTimeout(timer)})
</script>
<template>
 <div class="reread-panel" aria-label="确认问题导向补读">
  <p class="note">先定位并选择原文。关键词排序只辅助定位，不能证明相关；已读片段也可重新核对。每次仅处理一篇论文、一轮。</p>
  <div class="reread-controls">
   <label>论文<select :value="paper" :disabled="busy||active" @change="paper=Number(($event.target as HTMLSelectElement).value)"><option v-for="p in suggestion.paper_ids" :key="p" :value="p">P{{p}}</option></select></label>
   <label>原文定位词（可填英文术语）<input :value="locator" maxlength="500" :disabled="busy" @input="locator=($event.target as HTMLInputElement).value"/></label>
   <el-button :disabled="busy||active||!result.is_current||mode()==='demo'" @click="locate">定位原文片段</el-button>
  </div>
  <template v-if="preview">
   <p>原文档 {{preview.document_id}} · 固定分析 {{preview.analysis_id}}。展示 {{preview.candidates.length}} / {{preview.total_chunks}} 片段；用定位词可重新排列。</p>
   <div class="reread-candidates"><details v-for="c in preview.candidates" :key="c.evidence_id">
    <summary><label @click.stop><input type="checkbox" :checked="selected.includes(c.evidence_id)" :disabled="busy||active||c.characters>preview.per_batch_max_chars||(!selected.includes(c.evidence_id)&&selected.length>=12)" @change="toggle(c.evidence_id,($event.target as HTMLInputElement).checked)"/>{{c.evidence_id}} · 第 {{c.page_number??'未知'}} 页 · {{c.already_read?'已读，可重核':'未读'}} · {{c.characters}} 字</label> {{c.section_hint||'章节待核对'}}</summary>
    <pre>{{c.text}}</pre><p v-if="c.characters>preview.per_batch_max_chars">超过固定的每批字符预算，请重新分片后新建计划。</p>
   </details></div>
   <p>已选 {{selected.length}} / 12 片段，共 {{chosenChars}} 字。发送完整片段，不截断原文。</p>
   <div class="reread-controls"><label>最多模型调用（1–24）<input type="number" min="1" max="24" :value="calls" :disabled="busy||active" @input="calls=Number(($event.target as HTMLInputElement).value)"/></label><label>执行期限（10–1800秒）<input type="number" min="10" max="1800" :value="seconds" :disabled="busy||active" @input="seconds=Number(($event.target as HTMLInputElement).value)"/></label><el-button type="primary" :disabled="busy||active||!selected.length||!result.is_current" @click="confirm">确认范围与预算，开始补读</el-button></div>
   <p class="note">失败和引用修复调用计入预算；按原请求续跑不重置预算或期限。网络调用不保证立即取消，超期响应不会发布。补读另存新分析，原分析保留。</p>
  </template>
  <p v-if="error" class="error-text" role="alert">{{error}}</p>
  <div v-for="entry in records" :key="entry.task.id" class="reread-record">
   <TaskProgress :task="entry.task"/><p v-if="entry.budget">调用 {{entry.budget.calls_used}} / {{entry.budget.max_calls}}；期限 {{new Date(entry.budget.deadline_epoch*1000).toLocaleString()}}</p>
   <el-button v-if="entry.task.status==='failed'&&entry.task.error?.retryable" :disabled="busy||active" @click="resume(entry)">继续原补读（预算不重置）</el-button>
   <template v-if="entry.task.status==='succeeded'"><router-link :to="'/analysis?analysisId='+encodeURIComponent(entry.task.result_id||'')">查看新分析及原文引用</router-link> <el-button :disabled="busy||active||!result.is_current" @click="recheck(entry)">用新分析重新检查覆盖</el-button><p class="note">重新检查会另建执行分支，并发起一次覆盖模型调用，单独计为覆盖任务。新分析尚未经辅助评审，不沿用旧评审。</p></template>
  </div>
 </div>
</template>
<style scoped>.reread-panel{border-top:1px solid #dbe3eb;margin-top:12px;padding-top:10px}.reread-controls{display:flex;flex-wrap:wrap;align-items:end;gap:12px;margin:12px 0}.reread-controls label{display:grid;gap:6px;font-size:13px}.reread-controls input,.reread-controls select{padding:7px;border:1px solid #cbd5e1;border-radius:4px;font:inherit}.reread-candidates{max-height:380px;overflow:auto;background:white}.reread-candidates details{padding:8px;border-bottom:1px solid #e2e8f0}.reread-candidates summary{cursor:pointer}.reread-candidates pre{white-space:pre-wrap;font:inherit;line-height:1.7;padding:10px}.reread-record{border-top:1px solid #dbe3eb;padding-top:12px;margin-top:14px}</style>
