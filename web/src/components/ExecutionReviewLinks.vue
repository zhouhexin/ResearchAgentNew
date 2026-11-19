<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import type {ResearchExecution} from '../types/execution'
import type {ClaimReviewSummary} from '../types/claim-review'
import TaskProgress from './TaskProgress.vue'
const props=defineProps<{execution:ResearchExecution}>()
const emit=defineEmits<{rechecked:[executionId:string]}>()
interface Paper {paper_id:number;title:string;analysis_id:string;claim_count:number;selected_review_id?:string;reviews:ClaimReviewSummary[]}
const items=ref<Paper[]>([]),selected=ref<Record<string,string>>({}),opened=ref(false),busy=ref(false),error=ref(''),model=ref(''),questions=ref(''),timeout=ref(0),retries=ref(0)
const ids=computed(()=>Object.values(selected.value).filter(Boolean))
let epoch=0,ticket=0,timer:ReturnType<typeof setTimeout>|undefined
const complete=(r:ClaimReviewSummary)=>r.is_current&&r.task.status==='succeeded'&&r.completed===r.total&&r.total>0
async function load(){
 const version=epoch,id=++ticket;clearTimeout(timer)
 if(mode()==='demo')return
 try{const response=await api.executionReviewVersions(props.execution.plan_id,props.execution.id)
  if(version!==epoch||id!==ticket)return
  items.value=response.items;model.value=response.model_name;timeout.value=response.read_timeout;retries.value=response.sdk_retries;opened.value=true
  for(const p of items.value){
   if(!(p.analysis_id in selected.value))selected.value[p.analysis_id]=p.reviews.find(r=>r.id===p.selected_review_id&&complete(r))?.id||''
   if(selected.value[p.analysis_id]&&!p.reviews.some(r=>r.id===selected.value[p.analysis_id]&&complete(r)))selected.value[p.analysis_id]=''
  }
  if(items.value.some(p=>p.reviews.some(r=>['queued','running'].includes(r.task.status))))timer=setTimeout(()=>void load(),2500)
 }catch(e){if(version===epoch&&id===ticket)error.value=(e as Error).message}
}
async function start(p:Paper,resume?:string){
 if(busy.value||mode()==='demo')return
 const version=epoch;busy.value=true;error.value=''
 try{await api.startExecutionReview(props.execution.plan_id,props.execution.id,{analysis_id:p.analysis_id,...(resume?{resume_review_id:resume}:{})});if(version===epoch)await load()}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function link(){
 if(busy.value||!ids.value.length||mode()==='demo')return
 const version=epoch;busy.value=true;error.value=''
 try{const response=await api.linkExecutionReviews(props.execution.plan_id,props.execution.id,{review_ids:ids.value,research_questions:questions.value.split('\n').map(s=>s.trim()).filter(Boolean)});if(version===epoch)emit('rechecked',response.execution_id)}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
watch(()=>props.execution.id,()=>{epoch++;ticket++;clearTimeout(timer);items.value=[];selected.value={};opened.value=false;busy.value=false;error.value='';questions.value=props.execution.plan.research_questions.join('\n')},{immediate:true})
onBeforeUnmount(()=>{epoch++;clearTimeout(timer)})
</script>
<template>
 <section class="execution-reviews" aria-label="执行内独立评审与版本关联">
  <h3>独立评审与版本关联</h3><p>逐篇确认发起评审，完成后选择要关联的版本。新分析不会自动继承旧评审；模型判断不等于人工裁定。</p>
  <el-button :disabled="busy||mode()==='demo'" @click="load">查看或刷新评审版本</el-button>
  <template v-if="opened"><p>本执行固定模型：{{model}} · 单次超时 {{timeout}} 秒 · SDK额外重试 {{retries}} 次。每条结论单独请求；下方确认仅针对一篇论文，不自动批量执行，也不是整个研究的总预算。</p>
   <article v-for="p in items" :key="p.analysis_id"><h4>P{{p.paper_id}} · {{p.title}}</h4><p>固定分析 {{p.analysis_id}} · {{p.claim_count}} 条待核对结论。失败恢复复用已完成项，仍可能增加调用。</p>
    <el-button :disabled="busy||!p.claim_count||p.reviews.some(r=>['queued','running'].includes(r.task.status))" @click="start(p)">确认评审此分析（{{p.claim_count}} 条）</el-button>
    <div v-for="r in p.reviews" :key="r.id"><TaskProgress :task="r.task"/><p>{{r.model_name}} · {{r.completed}} / {{r.total}} 条 · {{r.quote_protocol}}<span v-if="r.legacy_reused"> · 旧协议复用 {{r.legacy_reused}} 条</span></p><router-link :to="'/analysis?analysisId='+encodeURIComponent(p.analysis_id)+'&claimReviewId='+encodeURIComponent(r.id)">核对评审理由与原文</router-link><el-button v-if="r.task.status==='failed'&&r.task.error?.retryable!==false" :disabled="busy" @click="start(p,r.id)">继续此评审未完成项</el-button></div>
    <label>本分支关联版本<select :value="selected[p.analysis_id]||''" :disabled="busy" @change="selected[p.analysis_id]=($event.target as HTMLSelectElement).value"><option value="">本次不关联该分析评审</option><option v-for="r in p.reviews.filter(complete)" :key="r.id" :value="r.id">{{r.id}} · {{r.model_name}} · {{r.completed}}/{{r.total}}</option></select></label>
   </article>
   <label>重新检查的问题（每行一项，最多6项）<textarea :value="questions" rows="2" maxlength="6000" :disabled="busy" @input="questions=($event.target as HTMLTextAreaElement).value"/></label>
   <p>仅关联本次选择的完整评审，未选择项标为未评审。确认将另存执行分支，并发起独立覆盖检查；原评审、原报告和原分析保留。综述可继续使用新分支及其评审编号。</p>
   <el-button type="primary" :disabled="busy||!ids.length||!questions.trim()" @click="link">确认评审版本并重新检查覆盖</el-button>
  </template><p v-if="error" role="alert" class="error-text">{{error}}</p>
 </section>
</template>
<style scoped>.execution-reviews{border-top:1px solid #dbe3eb;margin-top:18px;padding-top:14px}.execution-reviews article{border-bottom:1px solid #dbe3eb;padding:12px 0}.execution-reviews label{display:grid;gap:8px;margin:14px 0;font-size:13px}.execution-reviews select,.execution-reviews textarea{font:inherit;border:1px solid #cbd5e1;border-radius:4px;padding:8px;max-width:100%;box-sizing:border-box}.execution-reviews p{line-height:1.7}.execution-reviews a{margin-right:12px}</style>
