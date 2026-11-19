<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import {api,mode} from '../api'
import {readStorage} from '../api/utils'
import {useTaskStore} from '../stores/tasks'
import {deliverableNames,planActionNames,planVenues,venueNames,venuePlatforms} from '../types/planning'
import type {ResearchPlan,ResearchPlanRecord} from '../types/planning'
import TaskProgress from './TaskProgress.vue'
import ResearchSearchWorkspace from './ResearchSearchWorkspace.vue'
import LibraryExecutionWorkspace from './LibraryExecutionWorkspace.vue'

const props=defineProps<{initialPlanId?:string;initialTaskId?:string;initialSearchRunId?:string}>()
const emit=defineEmits<{(e:'opened',id:string):void}>()
const store=useTaskStore(),storageKey=`rp-research-planning-${mode()}`
const cached=readStorage<{goal?:string;task_id?:string}|null>(storageKey,{})
const goal=ref(typeof cached?.goal==='string'?cached.goal:''),taskId=ref(''),record=ref<ResearchPlanRecord|null>(null),draft=ref<ResearchPlan|null>(null)
const fields=ref<Record<string,string>>({}),stepFields=ref<Record<number,{inputs:string;outputs:string}>>({})
const venueMode=ref('arxiv')
const creating=ref(false),restoring=ref(false),saving=ref(false),error=ref(''),notice=ref(''),conflict=ref(false)
const task=computed(()=>store.tasks[taskId.value])
const active=computed(()=>!!task.value&&['queued','running'].includes(task.value.status))
const locked=computed(()=>creating.value||restoring.value||saving.value||active.value)
const goalChanged=computed(()=>!!record.value&&goal.value.trim()!==record.value.goal)
const listFields={research_questions:'研究问题',search_queries:'拟用检索词',comparison_dimensions:'比较维度',assumptions:'建议与假设',clarification_questions:'建议补充的问题'} as const
const scopeFields={languages:'文献语言',requested_sources:'用户指定的检索来源',inclusion_criteria:'纳入条件',exclusion_criteria:'排除条件'} as const
let version=0
const clone=<T,>(value:T):T=>JSON.parse(JSON.stringify(value))
const lines=(text?:string)=>(text||'').split('\n').map(value=>value.trim()).filter(Boolean)
function persist(){try{localStorage.setItem(storageKey,JSON.stringify({goal:goal.value,task_id:taskId.value}))}catch{/* 保存功能仍可使用服务端。 */}}
function snapshot(){
 if(!draft.value)return null
 const value=clone(draft.value)
 for(const key of Object.keys(listFields) as (keyof typeof listFields)[])value[key]=lines(fields.value[key])
 for(const key of Object.keys(scopeFields) as (keyof typeof scopeFields)[])value.scope[key]=lines(fields.value[key])
 for(const step of value.steps){step.inputs=lines(stepFields.value[step.number]?.inputs);step.outputs=lines(stepFields.value[step.number]?.outputs)}
 return value
}
const dirty=computed(()=>!!record.value&&JSON.stringify(snapshot())!==JSON.stringify(record.value.plan))
function apply(value:ResearchPlanRecord){
 venueMode.value=planVenues(value.plan.scope).length?'venues':'arxiv'
 record.value=value;draft.value=clone(value.plan);fields.value={};stepFields.value={}
 for(const key of Object.keys(listFields) as (keyof typeof listFields)[])fields.value[key]=value.plan[key].join('\n')
 for(const key of Object.keys(scopeFields) as (keyof typeof scopeFields)[])fields.value[key]=value.plan.scope[key].join('\n')
 for(const step of value.plan.steps)stepFields.value[step.number]={inputs:step.inputs.join('\n'),outputs:step.outputs.join('\n')}
 conflict.value=false
}
async function openPlan(id:string,keepGoal=false){
 const current=version;restoring.value=true;error.value='';notice.value=''
 try{
  const value=await api.researchPlan(id)
  if(current!==version)return
  apply(value);taskId.value=value.source_task_id
  if(!keepGoal||!goal.value.trim())goal.value=value.goal
  persist();emit('opened',value.id)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)restoring.value=false}
}
async function restore(){
 if(props.initialPlanId&&record.value?.id===props.initialPlanId)return
 const current=++version;error.value='';notice.value=''
 if(mode()==='demo')return
 if(props.initialPlanId){await openPlan(props.initialPlanId);return}
 const id=props.initialTaskId||cached?.task_id
 if(!id)return
 restoring.value=true
 try{
  const value=await api.task(id)
  if(current!==version)return
  if(value.type!=='research_planning')throw new Error('这项任务不是研究目标规划')
  taskId.value=id
  if(props.initialTaskId||!goal.value.trim())goal.value=value.research_goal||''
  persist();await store.track(id)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)restoring.value=false}
}
async function generate(){
 if(locked.value||goal.value.trim().length<2||mode()==='demo')return
 const current=++version;creating.value=true;error.value='';notice.value=''
 try{
  const response=await api.startResearchPlan(goal.value.trim())
  if(current!==version)return
  taskId.value=response.task_id;persist();await store.track(response.task_id)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)creating.value=false}
}
async function save(){
 const value=snapshot(),saved=record.value
 if(!value||!saved||locked.value||goalChanged.value)return
 if(venueMode.value==='venues'&&!planVenues(value.scope).length){error.value='请至少选择一个会议或期刊，或切换到 arXiv。';return}
 const current=version;saving.value=true;error.value='';notice.value='';conflict.value=false
 try{
  const result=await api.saveResearchPlan(saved.id,value,saved.revision)
  if(current!==version)return
  apply(result);notice.value=`计划已保存为第 ${result.revision} 版，尚未执行。`
 }catch(e){if(current===version){error.value=(e as Error).message;conflict.value=(e as {status?:number}).status===409}}
 finally{if(current===version)saving.value=false}
}
function setYear(key:'start_year'|'end_year',value:string){
 if(!draft.value)return
 draft.value.scope[key]=value.trim()===''?null:Number(value)
 draft.value.scope.year_basis='edited';draft.value.scope.year_evidence=''
}
function setPlanVenues(slugs:string[]){
 if(!draft.value)return
 const values=slugs as NonNullable<ResearchPlan['scope']['venue_slugs']>
 draft.value.scope.venue_slug=values.length===1?values[0]:null
 draft.value.scope.venue_slugs=values.length>1?values:[]
 draft.value.scope.verified_main_only=false
 fields.value.requested_sources=[...new Set(['openalex',...values.flatMap(v=>record.value?.capabilities.venue_platforms?.[v]||venuePlatforms[v]||[])])].join('\n')
}
function setVenue(value:string){
 if(!draft.value)return
 venueMode.value=value==='arxiv'?'arxiv':'venues'
 if(value==='arxiv'){draft.value.scope.venue_slug=null;draft.value.scope.venue_slugs=[];draft.value.scope.verified_main_only=false;fields.value.requested_sources='arxiv';return}
 setPlanVenues(value==='venues'?(planVenues(draft.value.scope).length?planVenues(draft.value.scope):['cvpr']):value==='cvpr_iccv'?['cvpr','iccv']:[value])
}
function togglePlanVenue(slug:string,checked:boolean){
 if(!draft.value)return
 const current=planVenues(draft.value.scope)
 setPlanVenues(checked?[...new Set([...current,slug])]:current.filter(value=>value!==slug))
}

watch(goal,persist)
watch(()=>[task.value?.id,task.value?.status,task.value?.result_id],()=>{
 if(task.value?.status==='succeeded'&&task.value.result_id&&record.value?.id!==task.value.result_id)void openPlan(task.value.result_id,true)
})
onMounted(()=>{watch([()=>props.initialPlanId,()=>props.initialTaskId],restore,{immediate:true})})
onBeforeUnmount(()=>{version++})
</script>

<template>
 <section class="panel goal-composer">
  <h2>先说清你想研究什么</h2>
  <p class="muted">描述研究主题、关注的问题，以及时间、场景或论文数量要求。没有确定的条件可以留给系统提出建议。</p>
  <label for="research-goal">研究目标</label>
  <el-input id="research-goal" v-model="goal" type="textarea" :rows="5" :maxlength="6000" show-word-limit :disabled="locked" placeholder="例如：梳理近五年机器人视觉评测研究，比较任务精度、运行速度和能耗，分析3篇论文并形成带原文证据的报告。" aria-label="研究目标"/>
  <div class="goal-actions"><el-button type="primary" :loading="creating||active" :disabled="locked||goal.trim().length<2||mode()==='demo'" @click="generate">理解目标并制定计划</el-button><span class="note">先确认并保存计划，再按计划从文献库选文或执行检索初筛。</span></div>
  <p v-if="mode()==='demo'" class="note">演示模式不调用模型或伪造研究计划，请在设置中切换到真实 API 模式。</p>
 </section>
 <p v-if="error" role="alert" class="error-text">{{error}}</p>
 <p v-if="notice" role="status" class="plan-notice">{{notice}}</p>
 <p v-if="restoring" class="note">正在读取研究计划…</p>
 <TaskProgress v-if="task" :task="task"/>
 <p v-if="store.error&&taskId" class="error-text">规划进度更新失败：{{store.error}}。可到任务中心刷新查看。</p>
 <section v-if="draft&&record" class="panel plan-editor">
  <div class="flex-between"><div><h2>研究目标与执行计划</h2><p class="note">草稿 · 第 {{record.revision}} 版 · {{record.model_name}}</p></div><el-button :loading="saving" :disabled="locked||!dirty||goalChanged" @click="save">保存计划修改</el-button></div>
  <p class="plan-scope-note">{{record.capabilities.notice}}</p>
  <p v-if="goalChanged" class="note warning">上方研究目标已改变。请重新生成计划，使研究范围和执行步骤与新目标一致。</p>
  <p v-if="conflict" class="note warning">服务器上已有较新版本。<el-button text @click="openPlan(record.id)">重新读取服务器版本</el-button></p>
  <p class="note">原始目标：{{record.goal}}</p>
  <div class="plan-grid">
   <label>检索范围<select :value="venueMode" :disabled="locked" aria-label="计划检索范围" @change="setVenue(($event.target as HTMLSelectElement).value)"><option value="arxiv">arXiv</option><option value="venues">顶刊顶会（会议/期刊可多选）</option></select></label>
   <fieldset v-if="venueMode==='venues'" class="wide"><legend>会议/期刊范围</legend><label v-for="(name,slug) in record.capabilities.venues||venueNames" :key="slug"><span><input type="checkbox" :checked="planVenues(draft.scope).some(value=>value===slug)" :disabled="locked" :aria-label="`计划检索 ${name}`" @change="togglePlanVenue(String(slug),($event.target as HTMLInputElement).checked)"/> {{name}}</span></label></fieldset>
   <label v-if="planVenues(draft.scope).length"><el-checkbox v-model="draft.scope.verified_main_only" :disabled="locked">仅保留正式来源确认的主会/期刊论文</el-checkbox><span class="note">切换范围后需保存计划。主会筛选与摘要相关性初筛分开。</span></label>
   <label>研究主题<el-input v-model="draft.topic" :disabled="locked" :maxlength="300" aria-label="研究主题"/></label>
   <label>本轮深入分析篇数<el-input-number v-model="draft.scope.paper_limit" :min="2" :max="5" :disabled="locked" aria-label="本轮分析篇数"/></label>
   <label class="wide">研究目的<el-input v-model="draft.objective" type="textarea" :rows="2" :disabled="locked" :maxlength="1500" aria-label="研究目的"/></label>
   <label>起始年份<el-input :model-value="draft.scope.start_year==null?'':String(draft.scope.start_year)" type="number" :disabled="locked" aria-label="起始年份" placeholder="不限" @update:model-value="setYear('start_year',$event)"/></label>
   <label>截止年份<el-input :model-value="draft.scope.end_year==null?'':String(draft.scope.end_year)" type="number" :disabled="locked" aria-label="截止年份" placeholder="不限" @update:model-value="setYear('end_year',$event)"/></label>
  </div>
  <p class="note">时间依据：{{({explicit:'用户明确要求',assumed:'系统建议值，可修改',unspecified:'用户未限定时间',edited:'用户手动修改'})[draft.scope.year_basis]}}<span v-if="draft.scope.year_evidence"> · 原话：{{draft.scope.year_evidence}}</span><span v-if="draft.scope.requested_paper_count!=null"> · 用户原始篇数要求：{{draft.scope.requested_paper_count}} 篇</span></p>
  <div class="plan-grid text-lists">
   <label v-for="(label,key) in listFields" :key="key">{{label}}<el-input v-model="fields[key]" type="textarea" :rows="3" :disabled="locked" :aria-label="label" placeholder="每行一项"/></label>
   <label v-for="(label,key) in scopeFields" :key="key">{{label}}<el-input v-model="fields[key]" type="textarea" :rows="2" :disabled="locked" :aria-label="label" placeholder="每行一项，未限定可留空"/></label>
  </div>
  <h3>预期交付物</h3><div class="tag-row"><el-tag v-for="value in draft.deliverables" :key="value">{{deliverableNames[value]}}</el-tag></div>
  <h3 class="steps-title">拟定执行步骤</h3>
  <article v-for="step in draft.steps" :key="step.number" class="plan-step">
   <div class="flex-between"><strong>{{String(step.number).padStart(2,'0')}} · {{planActionNames[step.action]}}</strong><span class="note">步骤定义<span v-if="step.depends_on.length"> · 依赖步骤 {{step.depends_on.join('、')}}</span></span></div>
   <label>步骤名称<el-input v-model="step.title" :disabled="locked" :maxlength="120" :aria-label="`步骤${step.number}名称`"/></label>
   <label>目的与安排<el-input v-model="step.purpose" type="textarea" :rows="2" :disabled="locked" :maxlength="800" :aria-label="`步骤${step.number}目的`"/></label>
   <div class="plan-grid"><label>计划输入<el-input v-model="stepFields[step.number].inputs" type="textarea" :rows="2" :disabled="locked" :aria-label="`步骤${step.number}输入`"/></label><label>预期产物<el-input v-model="stepFields[step.number].outputs" type="textarea" :rows="2" :disabled="locked" :aria-label="`步骤${step.number}产物`"/></label></div>
  </article>
  <div class="goal-actions"><el-button type="primary" :loading="saving" :disabled="locked||!dirty||goalChanged" @click="save">保存计划修改</el-button><span class="note">检索来源：{{record.capabilities.search_sources.join('、')}}。下方可执行检索与摘要初筛，确认选文后继续全文与分析。</span></div>
 </section>
 <LibraryExecutionWorkspace v-if="record&&record.plan.steps.some(s=>s.action==='select_library_papers')" :record="record" :disabled="locked||dirty||goalChanged"/>
 <ResearchSearchWorkspace v-else-if="record" :record="record" :disabled="locked||dirty||goalChanged" :initial-run-id="initialSearchRunId"/>
</template>

<style scoped>
.goal-composer,.plan-editor{padding:26px;margin-bottom:22px}
.goal-composer label,.plan-editor label{display:flex;flex-direction:column;gap:8px;font-size:13px;color:#526278}
.goal-actions{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:20px}
.plan-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:18px 0}
.plan-grid .wide{grid-column:1/-1}
.plan-grid .el-input-number{width:100%}
.plan-scope-note{padding:12px 15px;background:#f4f7fa;border-radius:7px;font-size:13px;color:#516777}
.steps-title{margin-top:28px}.plan-step{padding:20px 0;border-top:1px solid #e5ebf1}
.plan-step>label{margin-top:15px}.plan-notice{color:#357258;font-size:13px}
@media(max-width:650px){.goal-composer,.plan-editor{padding:18px}.plan-grid{grid-template-columns:1fr}.plan-editor>.flex-between{align-items:flex-start;flex-direction:column}}
</style>
