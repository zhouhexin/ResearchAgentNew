<script setup lang="ts">
import {ref,computed,onMounted,watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {ElMessage,ElMessageBox} from 'element-plus'
import {api,mode} from '../api'
import {categories,reviewSections} from '../types/research'
import type {PaperSummary,ReviewResult} from '../types/research'
import {csvCell,download} from '../api/utils'
import {useTaskStore} from '../stores/tasks'
import {useResource} from '../composables/useResource'
import AsyncState from '../components/AsyncState.vue'
import EvidenceCitation from '../components/EvidenceCitation.vue'
import TaskProgress from '../components/TaskProgress.vue'
import ReviewQuestionPicker from '../components/ReviewQuestionPicker.vue'
import ReviewReportBody from '../components/ReviewReportBody.vue'
const route=useRoute(),router=useRouter(),papers=ref<PaperSummary[]>([]),selected=ref<number[]>(String(route.query.paperIds||'').split(',').map(Number).filter(Boolean)),step=ref(0),topic=ref(''),matrixRows=ref<ReviewResult['matrix']>([]),analysisIds=ref<string[]>([]),review=ref<ReviewResult|null>(null),busy=ref(false),taskId=ref(''),tasks=useTaskStore()
const task=computed(()=>tasks.tasks[taskId.value]),valid=computed(()=>selected.value.length>=2&&selected.value.length<=5&&new Set(selected.value).size===selected.value.length&&selected.value.every(id=>papers.value.some(p=>p.id===id&&p.has_analysis)))
const {loading,error,load}=useResource(null)
const questionRestoreId=ref('')
const questionSourceId=ref('')
let selectionVersion=0,restoring=false
async function refresh(){await load(async()=>{
 papers.value=(await api.papers({has_analysis:true,page_size:100})).items
 if(route.query.reviewId){
  const value=await api.review(String(route.query.reviewId));restoring=true
  selected.value=[...value.paper_ids];topic.value=value.topic;analysisIds.value=value.analysis_ids||[]
  review.value=value;matrixRows.value=value.matrix;step.value=2;restoring=false
 }else if(route.query.questionsTask){
  const saved=await api.task(String(route.query.questionsTask))
  if(saved.type!=='review_questions'||!saved.paper_ids||!saved.analysis_ids)throw new Error('这项任务不是研究问题推荐')
  const value=await api.comparePapers(saved.paper_ids,saved.analysis_ids)
  restoring=true
  selected.value=[...value.paper_ids];analysisIds.value=value.analysis_ids;matrixRows.value=value.matrix
  questionRestoreId.value=saved.id;step.value=1;restoring=false
 }else if(route.query.paperIds&&valid.value){
  await matrix()
 }
 return null
})}
watch(selected,()=>{if(restoring)return;selectionVersion++;taskId.value='';questionRestoreId.value='';questionSourceId.value='';topic.value='';review.value=null;matrixRows.value=[];analysisIds.value=[];step.value=0},{deep:true,flush:'sync'})
async function matrix(){
 if(!valid.value){ElMessage.warning('请选择 2–5 篇不同且已完成分析的论文');return}
 const current=selectionVersion,ids=[...selected.value];busy.value=true
 try{
  const value=await api.comparePapers(ids)
  if(current!==selectionVersion)return
  if(analysisIds.value.join(',')!==value.analysis_ids.join(','))topic.value=''
  matrixRows.value=value.matrix;analysisIds.value=value.analysis_ids;questionRestoreId.value=''
  await router.replace({query:{paperIds:ids.join(',')}})
  if(current===selectionVersion)step.value=1
 }catch(e){if(current===selectionVersion)ElMessage.error((e as Error).message)}finally{busy.value=false}
}
async function generate(){
 if(!valid.value||!topic.value.trim()){ElMessage.warning('请填写与所选论文相关的综述主题');return}
 if(analysisIds.value.length!==selected.value.length){ElMessage.warning('请先重新生成比较矩阵，以确认本次分析版本');return}
 const current=selectionVersion,ids=[...selected.value],versions=[...analysisIds.value],focus=topic.value.trim()
 if(review.value){try{await ElMessageBox.confirm('将创建新综述任务，已有报告会保留。是否继续？','重新生成',{confirmButtonText:'创建任务',cancelButtonText:'取消'})}catch{return}}
 if(current!==selectionVersion)return
 busy.value=true
 try{
  const response=await api.startReview(ids,focus,versions,questionSourceId.value||undefined)
  if(current===selectionVersion)taskId.value=response.task_id
  if(response.reused)ElMessage.info('相同输入的综述任务正在进行，已切换到该任务')
  await tasks.track(response.task_id)
 }catch(e){ElMessage.error((e as Error).message)}finally{busy.value=false}
}
watch(()=>task.value?.status,async status=>{
 const completed=task.value,current=selectionVersion
 if(status!=='succeeded'||!completed?.result_id)return
 try{
  const value=await api.review(completed.result_id)
  if(current!==selectionVersion)return
  review.value=value;step.value=2
  void router.replace({query:{reviewId:value.id}})
  ElMessage.success('综述生成完成，跨论文引用已通过核验')
 }catch(e){if(current===selectionVersion)ElMessage.error((e as Error).message)}
})
const columns=computed(()=>(matrixRows.value.length?matrixRows.value:review.value?.matrix||[]).map(p=>({paper:{id:p.paper_id,title:p.title},analysis:p.analysis})))
function exportFile(format:'csv'|'json'|'md'){if(!review.value)return;if(mode()!=='demo'){window.open(api.reviewFileUrl(review.value.id,format),'_blank','noopener');return}if(format==='json'){download(JSON.stringify(review.value,null,2),'literature_review.json');return}if(format==='csv'){const rows=[['研究维度',...review.value.matrix.map(p=>p.title)],...Object.entries(categories).map(([key,label])=>[label,...review.value!.matrix.map(p=>p.analysis[key as keyof typeof categories].map(f=>f.statement+' '+f.evidence.map(e=>`[P${p.paper_id}:${e.evidence_id}]`).join(' ')).join('\n'))])];download('\uFEFF'+rows.map(r=>r.map(csvCell).join(',')).join('\r\n'),'comparison_matrix.csv','text/csv;charset=utf-8');return}const md=['# '+review.value.draft.title,'','> 演示报告 · 来自项目已导出快照；不是新生成结果。',...Object.entries(reviewSections).filter(([key])=>Array.isArray(review.value!.draft[key as keyof typeof reviewSections])).flatMap(([key,label])=>['','## '+label,'',...(review.value!.draft[key as keyof typeof reviewSections]||[]).flatMap(p=>[...(p.heading?['### '+p.heading,'']:[]),p.claim+' '+p.evidence_refs.map(r=>`[${r}]`).join(' '),'',...(p.paragraphs||[]).flatMap(paragraph=>[paragraph.text+' '+paragraph.evidence_refs.map(r=>`[${r}]`).join(' '),''])])])].join('\n');download(md,'literature_review.md','text/markdown;charset=utf-8')}
onMounted(refresh)
</script>
<template><div class="page-heading"><div><div class="eyebrow">FROM PAPERS TO PERSPECTIVE</div><h1>文献综述</h1><p>比较不同研究，让共同线索和关键差异浮现。</p></div><span class="muted">2–5 篇论文 · 原文证据可追溯</span></div><div class="panel review-steps"><button v-for="(label,i) in ['选择研究文献','查看比较矩阵','生成研究综述']" :key="i" :class="{current:step===i,done:step>i}" :disabled="i>step" @click="step=i"><span>{{step>i?'✓':String(i+1).padStart(2,'0')}}</span><strong>{{label}}</strong><i v-if="i<2">→</i></button></div><AsyncState :loading="loading" :error="error" @retry="refresh"><section v-if="step===0" class="panel selection-panel"><div class="section-heading"><div><h2>哪些论文，值得放在一起读？</h2><p class="muted">选择已有有效分析的论文，最多 5 篇。</p></div><span class="selection-count">{{selected.length}} / 5 已选择</span></div><div class="review-paper-options"><label v-for="p in papers" :key="p.id" class="review-paper-option" :class="{selected:selected.includes(p.id)}"><el-checkbox v-model="selected" :value="p.id" :disabled="selected.length>=5&&!selected.includes(p.id)" :aria-label="`选择 ${p.title}`"/><div><code>P{{p.id}}</code><h3>{{p.title}}</h3><p>{{p.doi||'DOI 未记录'}}</p><span class="status-chip blue"><i/>已有分析</span></div></label></div><div v-if="!papers.length" class="quiet-empty">还没有可用的分析，先去完成单篇论文分析。</div><div class="selection-footer"><span class="note">请确保所选材料能够共同支持你的综述主题。</span><el-button type="primary" :disabled="!valid" :loading="busy" @click="matrix">查看比较矩阵 →</el-button></div></section><template v-else-if="step===1"><div class="section-heading"><div><h2>让差异，一目了然。</h2><p class="muted">矩阵基于已有分析生成，每条发现保留原文引用。</p></div><el-button @click="step=0">调整所选论文</el-button></div><div class="panel matrix-scroll"><table class="comparison-table"><thead><tr><th>研究维度</th><th v-for="p in columns" :key="p.paper.id"><code>P{{p.paper.id}}</code><h3>{{p.paper.title}}</h3></th></tr></thead><tbody><tr v-for="(label,key) in categories" :key="key"><th>{{label}}</th><td v-for="p in columns" :key="p.paper.id"><div v-for="(f,i) in p.analysis[key]" :key="i" class="matrix-finding"><p>{{f.statement}}</p><EvidenceCitation v-for="e in f.evidence" :key="e.evidence_id" :ref-id="`P${p.paper.id}:${e.evidence_id}`"/></div><p v-if="!p.analysis[key].length" class="muted">本次证据不足</p></td></tr></tbody></table></div><ReviewQuestionPicker :key="analysisIds.join(',')" v-model="topic" :paper-ids="selected" :analysis-ids="analysisIds" :restore-task-id="questionRestoreId||undefined" @source-task="questionSourceId=$event" :disabled="busy||['queued','running'].includes(task?.status||'')"/><section class="panel generate-panel"><div><h3>按确认后的研究问题生成综述</h3><p class="muted">请选择或填写上方研究问题，再生成带原文引用的综述。</p></div><el-button type="primary" :loading="busy" :disabled="!topic.trim()||['queued','running'].includes(task?.status||'')" @click="generate">生成综述 ↗</el-button></section><TaskProgress v-if="task" :task="task"/></template><template v-else-if="review"><div class="report-toolbar"><div><span class="eyebrow">RESEARCH SYNTHESIS</span><h2>研究综述</h2></div><div class="button-row"><el-button @click="exportFile('csv')">比较矩阵 CSV ↓</el-button><el-button @click="exportFile('json')">JSON ↓</el-button><el-button type="primary" @click="exportFile('md')">Markdown ↓</el-button></div></div><p v-if="review.is_current===false" class="note warning" role="alert">这份综述对应的原文、分析或关联评审已变化，属于历史报告。请核对材料与版本后重新生成，再用于当前研究。</p><article class="panel review-report"><div class="report-topline">RESEARCHPILOT <span>文献综述 / {{review.paper_ids.length}} 篇论文</span></div><h1>{{review.draft.title}}</h1><p class="note">本次主题：{{review.topic}}</p><p v-if="review.analysis_ids?.length" class="note">本次分析版本：{{review.analysis_ids.join(' · ')}}</p><p class="report-disclaimer">本报告仅综合所选论文的分析结果，不等同于完整领域查新。{{mode()==='demo'?'当前展示项目已导出的示例报告。':''}}</p><ReviewReportBody :draft="review.draft" :review="review"/></article></template></AsyncState></template>
