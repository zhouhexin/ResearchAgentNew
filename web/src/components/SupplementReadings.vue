<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {api,mode} from '../api'
interface Choice {candidate_id:string;title:string;screening:{decision:string}|null;analyses:{paper_id:number;analysis_id:string;scope:string;selected_chunks:number;total_chunks:number}[]}
const props=defineProps<{requestId:string;current:boolean}>()
const emit=defineEmits<{rechecked:[executionId:string]}>()
const items=ref<Choice[]>([]),selection=ref<Record<string,string>>({}),reason=ref(''),slots=ref(0)
const opened=ref(false),busy=ref(false),error=ref('')
const selected=computed(()=>Object.entries(selection.value).filter(([,value])=>value).map(([candidate_id,analysis_id])=>({candidate_id,analysis_id})))
let epoch=0
async function load(){
 if(mode()==='demo')return
 const version=epoch;busy.value=true;error.value=''
 try{const response=await api.supplementReadings(props.requestId);if(version!==epoch)return
  items.value=response.items;slots.value=response.remaining_slots;selection.value={};opened.value=true
 }catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
async function adopt(){
 if(mode()==='demo'||busy.value||!props.current||!selected.value.length||selected.value.length>slots.value||!reason.value.trim())return
 const version=epoch;busy.value=true;error.value=''
 try{const response=await api.adoptSupplementReadings(props.requestId,{readings:selected.value,reason:reason.value.trim()});if(version===epoch)emit('rechecked',response.execution_id)}
 catch(e){if(version===epoch)error.value=(e as Error).message}
 finally{if(version===epoch)busy.value=false}
}
watch(()=>props.requestId,()=>{epoch++;opened.value=false;items.value=[];selection.value={};reason.value='';error.value='';busy.value=false})
onBeforeUnmount(()=>{epoch++})
</script>
<template>
 <section class="supplement-readings" aria-label="补充阅读接回研究任务">
  <p>补充论文完成下载和分析后，可选择有效分析版本接回原研究问题。保留原论文，合计最多5篇；不自动下载或分析。</p>
  <el-button :disabled="busy||!current||mode()==='demo'" @click="load">关联已完成的补充阅读 / 刷新版本</el-button>
  <template v-if="opened"><p>还可加入 {{slots}} 篇。原论文不会移除。初筛仅作参考，纳入理由由你填写。</p>
   <label v-for="item in items" :key="item.candidate_id">{{item.title}} · {{item.screening?.decision==='included'?'初筛建议纳入':item.screening?.decision==='excluded'?'初筛建议排除，请说明纳入理由':'初筛待确认'}}
    <select :value="selection[item.candidate_id]||''" :disabled="busy||!current||!slots||!item.analyses.length" @change="selection[item.candidate_id]=($event.target as HTMLSelectElement).value"><option value="">{{item.analyses.length?'不加入，或选择固定分析版本':'尚无身份匹配的有效分析，请先到文献库完成阅读'}}</option><option v-for="a in item.analyses" :key="a.analysis_id" :value="a.analysis_id">P{{a.paper_id}} · {{a.scope}} · {{a.selected_chunks}}/{{a.total_chunks}} 片段 · {{a.analysis_id}}</option></select>
   </label>
   <label>纳入理由（与当前问题、缺口的关系）<textarea :value="reason" rows="2" maxlength="1000" :disabled="busy" @input="reason=($event.target as HTMLTextAreaElement).value"/></label>
   <p v-if="selected.length>slots" role="alert">超过剩余名额，请减少选择；系统不会自动替换原论文。</p>
   <p class="note">确认后保存新分支，并沿用原问题发起一次独立覆盖检查，不扣除原补检索预算。新增分析尚未关联辅助评审；覆盖判断仍需人工核对。</p>
   <el-button type="primary" :disabled="busy||!current||!selected.length||selected.length>slots||!reason.trim()" @click="adopt">确认新增论文并重新检查覆盖</el-button>
  </template>
  <p v-if="error" role="alert" class="error-text">{{error}}</p>
 </section>
</template>
<style scoped>.supplement-readings{border-top:1px solid #dbe3eb;margin-top:16px;padding-top:12px}.supplement-readings label{display:grid;gap:8px;margin:14px 0;font-size:13px}.supplement-readings select,.supplement-readings textarea{font:inherit;border:1px solid #cbd5e1;border-radius:4px;padding:8px;max-width:100%;box-sizing:border-box}</style>
