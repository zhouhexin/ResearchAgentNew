<script setup lang="ts">
import {onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
import type {CheckKind,ReportCheckTask,ReportCheckResult} from '../types/report-check'
const props=defineProps<{reportId:string}>()
const records=ref<ReportCheckTask[]>([]),result=ref<ReportCheckResult|null>(null),busy=ref(false),error=ref('')
const consent=ref(false),reviewer=ref(''),requirement=ref(''),handoffId=ref('')
const maxCalls=ref(100),totalSeconds=ref(1800)
let epoch=0,timer:ReturnType<typeof setTimeout>|undefined
const labels:Record<CheckKind,string>={independent_review:'独立审查',original_replay:'按原协议回放'}
async function refresh(){
 if(mode()==='demo')return
 const version=epoch
 try{const value=await api.reportChecks(props.reportId);if(version!==epoch)return;records.value=value.items
  clearTimeout(timer)
  if(records.value.some(v=>['queued','running'].includes(v.status)))timer=setTimeout(()=>void refresh(),2500)
 }catch(e){if(version===epoch)error.value=(e as Error).message}
}
async function start(kind:CheckKind,resume?:string){
 if(busy.value||mode()==='demo')return
 const version=epoch;busy.value=true;error.value=''
 try{await api.startReportCheck(props.reportId,kind,resume,{max_calls:maxCalls.value,total_seconds:totalSeconds.value});if(version===epoch)await refresh()}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function open(id:string){
 const version=epoch;busy.value=true;error.value=''
 try{const value=await api.reportCheck(props.reportId,id);if(version===epoch){result.value=value;handoffId.value=''}}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function handoff(){
 if(!result.value||!consent.value||!reviewer.value.trim()||!requirement.value.trim()||busy.value)return
 const version=epoch;busy.value=true;error.value=''
 try{const value=await api.handoffReport(props.reportId,{result_ids:[result.value.id],
  permission:{allowed:true,basis:'用户在综述页面授权本报告及所选检查记录用于离线开发实验。',granted_by:reviewer.value.trim(),granted_at:new Date().toISOString()},
  requirements:[{id:'report-requirement',description:requirement.value.trim(),author:reviewer.value.trim(),rationale:'用户明确填写的验收要求；没有提供人工标准答案。'}]})
  if(version===epoch)handoffId.value=value.artifact_id
 }catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
watch(()=>props.reportId,()=>{epoch++;clearTimeout(timer);records.value=[];result.value=null;busy.value=false;error.value='';consent.value=false;handoffId.value='';void refresh()},{immediate:true})
onBeforeUnmount(()=>{epoch++;clearTimeout(timer)})
</script>
<template>
 <section class="report-checks" aria-label="综述独立审查与回放">
  <h3>综述独立审查与回放</h3>
  <p>逐条检查主题句和解释段落，保留理由与原文。回放使用原输入、模型配置及引用规则；输出可能因模型随机性而变化。两项检查均会调用模型，原报告保留。</p>
  <label>新检查最多调用次数<input v-model.number="maxCalls" type="number" min="1" max="500"/></label><label>新检查总时限（秒）<input v-model.number="totalSeconds" type="number" min="10" max="86400"/></label><p>继续任务沿用原预算；供应方内部重试及未知用量另行记录。</p>
  <div class="check-actions"><el-button :disabled="busy||mode()==='demo'" @click="start('independent_review')">开始独立审查</el-button><el-button :disabled="busy||mode()==='demo'" @click="start('original_replay')">按原协议回放</el-button><el-button :disabled="busy||mode()==='demo'" @click="refresh">刷新检查记录</el-button></div>
  <p v-for="record in records" :key="record.id">{{labels[record.check_kind]}} · {{record.status}} · {{record.stage}}
   <span v-if="record.error"> · {{record.error.message}}</span>
   <el-button v-if="record.result_id" :disabled="busy" @click="open(record.result_id)">查看结果与原文</el-button>
   <el-button v-if="record.status==='failed'" :disabled="busy" @click="start(record.check_kind,record.report_check_request_id)">在原预算内继续</el-button>
  </p>
  <div v-if="result">
   <p>{{labels[result.check_kind]}} · 共 {{result.denominator}} 项，未完成 {{result.failed}} 项。{{result.source_unchanged?'来源未变化':'来源已变化，请核对历史版本'}}。模型判断仍待人工复核。</p>
   <p>结果编号：{{result.id}}</p>
   <details v-for="item in result.items" :key="item.unit.unit_id"><summary>{{item.unit.statement||'原协议回放'}} · {{item.result?.review?.verdict||item.status}}</summary>
    <p>{{item.result?.review?.reason||item.error?.message}}</p><p v-if="item.result?.diagnostics?.length">总体与逐证据判断有冲突，需人工复核；原标签已保留。</p>
    <p v-if="item.result?.same_draft!==undefined">本次输出{{item.result.same_draft?'与原报告一致':'与原报告有差异'}}；一致性不代表语义正确。</p>
    <blockquote v-for="(citation,i) in item.result?.citations||[]" :key="i"><p>{{citation.ref_id}} · {{citation.page_number===null?'无页码':`第 ${citation.page_number} 页`}} · 片段字符 {{citation.start_char}}–{{citation.end_char}}</p>{{citation.text}}</blockquote>
   </details>
   <details><summary>将实际问题交给 Evolution</summary><p>仅保存失败或待复核问题，供离线开发；不自动运行实验或修改生产版本。</p><label>授权人<input v-model="reviewer" maxlength="100"/></label><label>本次验收要求<textarea v-model="requirement" maxlength="2000"/></label><label><input v-model="consent" type="checkbox"/>允许本报告材料和所选检查记录用于离线开发实验</label><el-button :disabled="busy||!consent||!reviewer.trim()||!requirement.trim()||!result.source_unchanged" @click="handoff">保存并交给 Evolution</el-button><p v-if="handoffId">已交接：{{handoffId}}。实验尚未运行。</p></details>
  </div>
  <p v-if="error" class="error-text" role="alert">{{error}}</p>
 </section>
</template>
<style scoped>.report-checks{border-top:1px solid #dbe3eb;margin-top:24px;padding-top:16px;line-height:1.8}.check-actions{display:flex;gap:12px;flex-wrap:wrap}details{margin:12px 0}blockquote{white-space:pre-wrap;border-left:3px solid #cbd5e1;padding:12px}label{display:block;margin:10px 0}textarea{display:block;width:100%;box-sizing:border-box}</style>
