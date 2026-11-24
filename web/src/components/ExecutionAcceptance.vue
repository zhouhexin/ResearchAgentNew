<script setup lang="ts">
import {onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import type {ResearchExecution} from '../types/execution'
import type {Inspection,Acceptance} from '../types/acceptance'
const props=defineProps<{execution:ResearchExecution}>()
const labels={source_quotes:'原文引句与来源',semantic_judgments:'结论及覆盖判断',pdf_navigation:'PDF页码跳转',export_files:'文件实际下载与内容'}
const checks={execution_completed:'执行完成状态',analysis_sources:'分析与原文',runtime_replay_available:'当前运行版本可恢复性',associated_reviews:'已关联评审',coverage:'所选覆盖报告',report:'所选综述'}
const statuses={passed:'核验通过',failed:'核验未通过',not_selected:'未选择、未验证'}
const coverageId=ref(''),reportId=ref(''),reviewer=ref(''),busy=ref(false),error=ref(''),inspection=ref<Inspection|null>(null),saved=ref<Acceptance|null>(null)
const manual=ref(Object.keys(labels).map(key=>({key,verdict:'not_checked',reason:''})))
const coverages=ref<{id:string;label:string}[]>([]),reports=ref<{id:string;label:string}[]>([]),history=ref<{id:string;created_at:string;reviewer:string}[]>([])
let epoch=0
function selection(){return {...(coverageId.value?{coverage_id:coverageId.value}:{}),...(reportId.value?{report_id:reportId.value}:{})}}
async function refresh(){
 if(mode()==='demo'||busy.value)return
 const version=epoch;busy.value=true;error.value=''
 try{const [c,r,h]=await Promise.all([api.coverageTasks(props.execution.plan_id,props.execution.id),api.planReports(props.execution.plan_id,props.execution.id),api.acceptanceHistory(props.execution.plan_id,props.execution.id)])
  if(version!==epoch)return
  coverages.value=c.items.filter(v=>v.task.status==='succeeded'&&v.task.result_id).map(v=>({id:v.task.result_id!,label:v.task.created_at+' · '+v.task.result_id}))
  reports.value=(r.items as {review_id?:string;status:string;created_at:string}[]).filter(v=>v.status==='succeeded'&&v.review_id).map(v=>({id:v.review_id!,label:v.created_at+' · '+v.review_id}))
  history.value=h.items
 }catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function inspect(){
 if(mode()==='demo'||busy.value)return
 const version=epoch;busy.value=true;error.value='';inspection.value=null;saved.value=null
 try{const result=await api.inspectAcceptance(props.execution.plan_id,props.execution.id,selection());if(version===epoch)inspection.value=result}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function save(){
 if(!inspection.value||busy.value||mode()==='demo')return
 const version=epoch;busy.value=true;error.value=''
 try{const result=await api.saveAcceptance(props.execution.plan_id,props.execution.id,{...selection(),expected_fingerprint:inspection.value.fingerprint,reviewer:reviewer.value,manual:manual.value});if(version!==epoch)return;const record=await api.acceptance(result.id);if(version===epoch){saved.value=record;history.value.unshift({id:result.id,created_at:result.created_at,reviewer:result.reviewer})}}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function open(id:string){const version=epoch;busy.value=true;error.value='';try{const result=await api.acceptance(id);if(version===epoch)saved.value=result}catch(e){if(version===epoch)error.value=(e as Error).message}finally{if(version===epoch)busy.value=false}}
watch(()=>[coverageId.value,reportId.value],()=>{inspection.value=null;saved.value=null;manual.value=Object.keys(labels).map(key=>({key,verdict:'not_checked',reason:''}))})
watch(()=>props.execution.id,()=>{epoch++;coverageId.value='';reportId.value='';reviewer.value='';inspection.value=null;saved.value=null;coverages.value=[];reports.value=[];history.value=[];error.value='';busy.value=false;manual.value=Object.keys(labels).map(key=>({key,verdict:'not_checked',reason:''}))})
onBeforeUnmount(()=>{epoch++})
</script>
<template>
 <section class="execution-acceptance" aria-label="研究流程验收记录">
  <h3>研究流程验收</h3><p>程序检查与人工声明分开保存。不会调用模型，也不会自动认定 P2 已完成或授权材料用于评测。</p>
  <el-button :disabled="busy||mode()==='demo'" @click="refresh">加载可验收产物及历史</el-button>
  <div class="acceptance-grid"><label>覆盖报告<select :value="coverageId" :disabled="busy" @change="coverageId=($event.target as HTMLSelectElement).value"><option value="">不选择，保持未验证</option><option v-for="c in coverages" :key="c.id" :value="c.id">{{c.label}}</option></select></label><label>综述<select :value="reportId" :disabled="busy" @change="reportId=($event.target as HTMLSelectElement).value"><option value="">不选择，保持未验证</option><option v-for="r in reports" :key="r.id" :value="r.id">{{r.label}}</option></select></label></div>
  <el-button :disabled="busy||mode()==='demo'" @click="inspect">检查当前材料与关联</el-button>
  <template v-if="inspection"><p v-for="c in inspection.checks" :key="c.key">{{checks[c.key as keyof typeof checks]||c.key}} · {{statuses[c.status]}} · {{c.detail}}</p><p>未关联评审的分析：{{inspection.missing_review_analysis_ids.join('、')||'无'}}。已关联不等于人工确认。</p>
   <p v-for="a in inspection.artifacts" :key="a.id">{{a.kind}} · {{a.id}}<span v-if="a.paper_id"> · {{a.scope}} · 阅读 {{a.selected_chunks}}/{{a.total_chunks}} 片段</span></p>
   <label>复核人（自述，未认证）<input :value="reviewer" maxlength="100" :disabled="busy" @input="reviewer=($event.target as HTMLInputElement).value"/></label>
   <div v-for="entry in manual" :key="entry.key" class="manual-check"><label>{{labels[entry.key as keyof typeof labels]}}<select :value="entry.verdict" :disabled="busy" @change="entry.verdict=($event.target as HTMLSelectElement).value"><option value="not_checked">未检查</option><option value="passed">人工核对通过</option><option value="failed">人工核对失败</option></select></label><textarea :value="entry.reason" :disabled="busy" maxlength="2000" placeholder="通过/失败需填写核对依据，例如论文、页码、结论或导出文件" @input="entry.reason=($event.target as HTMLTextAreaElement).value"/></div>
   <el-button :disabled="busy||!reviewer.trim()||manual.some(v=>v.verdict!=='not_checked'&&!v.reason.trim())" @click="save">保存本次验收声明</el-button>
  </template>
  <p v-if="error" class="error-text" role="alert">{{error}}</p>
  <p v-for="h in history" :key="h.id"><el-button :disabled="busy" @click="open(h.id)">{{h.created_at}} · {{h.reviewer}} · 查看验收记录</el-button></p>
  <div v-if="saved"><strong>已保存 {{saved.id}}</strong><p>{{saved.inputs_unchanged?'输入未变化；仍需结合人工声明判断':'材料或检查状态已变化，旧声明须重新核对'}}</p><p v-for="v in saved.manual" :key="v.key">{{labels[v.key as keyof typeof labels]}} · {{v.verdict}} · {{v.reason}}</p><a :href="api.acceptanceFileUrl(saved.id,'md')" target="_blank" rel="noopener">导出验收 Markdown</a> · <a :href="api.acceptanceFileUrl(saved.id,'json')" target="_blank" rel="noopener">导出验收 JSON</a></div>
 </section>
</template>
<style scoped>.execution-acceptance{border-top:1px solid #dbe3eb;margin-top:20px;padding-top:16px;line-height:1.7}.execution-acceptance label{display:grid;gap:8px;margin:12px 0;font-size:13px}.acceptance-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.execution-acceptance input,.execution-acceptance select,.execution-acceptance textarea{font:inherit;border:1px solid #cbd5e1;border-radius:4px;padding:8px;width:100%;max-width:100%;box-sizing:border-box}.manual-check{border-top:1px solid #edf0f4;padding:10px 0}</style>
