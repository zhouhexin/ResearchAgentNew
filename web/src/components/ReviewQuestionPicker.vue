<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import {api,mode} from '../api'
import {readStorage} from '../api/utils'
import type {TaskInfo} from '../types/research'
import {useTaskStore} from '../stores/tasks'
import EvidenceCitation from './EvidenceCitation.vue'
import TaskProgress from './TaskProgress.vue'

const props=defineProps<{paperIds:number[];analysisIds:string[];modelValue:string;disabled?:boolean;restoreTaskId?:string}>()
const emit=defineEmits<{(e:'update:modelValue',value:string):void;(e:'sourceTask',value:string):void}>()
const store=useTaskStore(),taskId=ref(''),submitting=ref(false),error=ref('')
const selection=computed(()=>JSON.stringify(props.paperIds.map((id,i)=>[id,props.analysisIds[i]]).sort((a,b)=>Number(a[0])-Number(b[0]))))
const storageKey=computed(()=>`rp-review-questions-${mode()}-${selection.value}`)
const task=computed(()=>store.tasks[taskId.value])
const active=computed(()=>!!task.value&&['queued','running'].includes(task.value.status))
const suggestions=computed(()=>task.value?.status==='succeeded'?task.value.question_suggestions||[]:[])
let version=0
function matches(value:TaskInfo){
 return value.type==='review_questions'&&!!value.paper_ids&&!!value.analysis_ids&&
  JSON.stringify(value.paper_ids.map((id,i)=>[id,value.analysis_ids![i]]).sort((a,b)=>Number(a[0])-Number(b[0])))===selection.value
}
function persist(){
 try{localStorage.setItem(storageKey.value,JSON.stringify({task_id:taskId.value,topic:props.modelValue}))}catch{/* 私密浏览等场景仍可继续使用。 */}
}
async function recommend(){
 if(props.disabled||submitting.value||active.value||mode()==='demo')return
 const current=++version;submitting.value=true;error.value=''
 try{
  const response=await api.suggestReviewQuestions([...props.paperIds],[...props.analysisIds])
  if(current!==version)return
  taskId.value=response.task_id;persist()
  await store.track(response.task_id)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)submitting.value=false}
}
async function restore(){
 const current=++version;taskId.value='';error.value='';submitting.value=false
 if(mode()==='demo')return
 const cached=readStorage<{task_id?:string;topic?:string}|null>(storageKey.value,{})
 const saved=cached&&typeof cached==='object'?cached:{}
 if(!props.modelValue&&typeof saved.topic==='string')emit('update:modelValue',saved.topic)
 const id=props.restoreTaskId||saved.task_id
 if(id){
  submitting.value=true
  try{
   const value=await api.task(id)
   if(current!==version)return
   if(!matches(value))throw new Error('推荐问题与当前所选论文或分析版本不一致，请重新推荐')
   taskId.value=value.id;persist();await store.track(value.id)
   return
  }catch(e){if(current===version)error.value=(e as Error).message}
  finally{if(current===version)submitting.value=false}
  return
 }
 await recommend()
}
watch(()=>props.modelValue,persist)
watch(()=>[task.value?.id,task.value?.status],()=>emit('sourceTask',task.value?.status==='succeeded'?task.value.id:''),{immediate:true})
onMounted(()=>{watch([selection,()=>props.restoreTaskId],restore,{immediate:true})})
onBeforeUnmount(()=>{version++})
</script>

<template>
 <section class="panel question-picker">
  <div class="flex-between question-heading"><div><h3>选择这次综述的研究问题</h3><p class="muted">根据所选论文推荐，可直接选择，也可修改措辞或自行填写。</p></div>
   <el-button :loading="submitting||active" :disabled="disabled||mode()==='demo'" @click="recommend">{{taskId?'重新推荐':'推荐研究问题'}}</el-button>
  </div>
  <p v-if="mode()==='demo'" class="note">演示模式不调用模型推荐问题，可手动填写并查看已有示例综述。</p>
  <p v-if="error" role="alert" class="error-text">{{error}}</p>
  <TaskProgress v-if="task" :task="task"/>
  <p v-if="store.error&&taskId" role="alert" class="error-text">推荐进度更新失败：{{store.error}}。可在任务中心刷新查看。</p>
  <div v-if="suggestions.length" class="question-options">
   <article v-for="(item,i) in suggestions" :key="i" class="question-option" :class="{selected:modelValue.trim()===item.question.trim()}">
    <button type="button" class="question-choice" :disabled="disabled" :aria-pressed="modelValue.trim()===item.question.trim()" @click="emit('update:modelValue',item.question)"><span>{{String(i+1).padStart(2,'0')}}</span>{{item.question}}</button>
    <p class="muted">{{item.rationale}}</p>
    <div class="citation-row"><EvidenceCitation v-for="refId in item.evidence_refs" :key="refId" :ref-id="refId"/></div>
   </article>
  </div>
  <p v-else-if="task?.status==='succeeded'" class="note">这些材料暂不足以推荐共同问题。可调整论文，或自行填写希望讨论的问题。</p>
  <p v-if="task?.status==='failed'" class="note">可以重新推荐，或直接在下方填写研究问题。</p>
  <label class="question-editor-label" for="review-topic">本次研究问题（可编辑）</label>
  <el-input id="review-topic" :model-value="modelValue" :disabled="disabled" type="textarea" :rows="3" :maxlength="1000" show-word-limit placeholder="选择上方问题后可继续修改，也可以自行填写" aria-label="综述主题" @update:model-value="emit('update:modelValue',$event)"/>
 </section>
</template>

<style scoped>
.question-picker{margin-top:24px;padding:26px}
.question-heading{gap:16px;align-items:flex-start}
.question-heading h3{margin:0 0 9px}
.question-heading p{margin:0 0 18px}
.question-options{display:grid;gap:12px;margin:18px 0 22px}
.question-option{padding:17px 19px;border:1px solid #dfe6ee;border-radius:10px}
.question-option.selected{border-color:#40705e;background:#f5faf7}
.question-choice{display:flex;gap:12px;align-items:baseline;text-align:left;width:100%;padding:0;border:0;background:transparent;font:inherit;font-weight:600;line-height:1.6;cursor:pointer;color:inherit}
.question-choice span{font-size:12px;color:#778899;flex-shrink:0}
.question-choice:focus-visible{outline:2px solid #40705e;outline-offset:5px}
.question-choice:disabled{cursor:default;opacity:.65}
.question-option p{font-size:13px;line-height:1.7;margin:10px 0}
.question-editor-label{display:block;font-size:13px;margin:18px 0 10px}
@media(max-width:600px){.question-picker{padding:18px}.question-heading{flex-direction:column}.question-option{padding:14px}}
</style>
