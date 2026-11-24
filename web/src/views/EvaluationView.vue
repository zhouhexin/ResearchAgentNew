<script setup lang="ts">
import {onBeforeUnmount,ref} from 'vue'
import {api,mode} from '../api'
import {useRoute} from 'vue-router'
import TaskProgress from '../components/TaskProgress.vue'
import type {TaskInfo} from '../types/research'
const route=useRoute()
const action=ref('datasets'),input=ref(''),result=ref<Record<string,unknown>|null>(null),artifactId=ref(typeof route.query.artifactId==='string'?route.query.artifactId:''),jobId=ref(''),confirmed=ref(false),busy=ref(false),error=ref(''),task=ref<TaskInfo|null>(null)
let timer:ReturnType<typeof setTimeout>|undefined,alive=true
const labels:Record<string,string>={datasets:'登记已获准的固定任务集','datasets/export':'从固定分析导出材料','bindings':'固定当前实际运行配置',jobs:'准备受预算限制的回放任务',compare:'比较两个已登记结果'}
async function perform(){
 if(mode()==='demo'||busy.value)return
 busy.value=true;error.value=''
 try{result.value=await api.evaluationAction(action.value,JSON.parse(input.value));if(typeof result.value.job_id==='string')jobId.value=result.value.job_id}catch(e){error.value=(e as Error).message}finally{busy.value=false}
}
async function read(){if(mode()==='demo')return;busy.value=true;error.value='';try{result.value=await api.evaluationArtifact(artifactId.value)}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function poll(id:string){try{const value=await api.task(id);if(!alive)return;task.value=value;if(['queued','running'].includes(value.status))timer=setTimeout(()=>void poll(id),2500);else if(value.result_id)artifactId.value=value.result_id}catch(e){if(alive)error.value=(e as Error).message}}
async function execute(){
 if(!confirmed.value||mode()==='demo'||busy.value)return
 busy.value=true;error.value=''
 try{const response=await api.evaluationExecute(jobId.value);confirmed.value=false;await poll(response.task_id)}catch(e){error.value=(e as Error).message}finally{busy.value=false}
}
onBeforeUnmount(()=>{alive=false;clearTimeout(timer)})
</script>
<template>
 <div class="page-heading"><div><h1>研究评测</h1><p>固定材料、实际执行配置与逐例结果。离线实验不替换现有科研任务的模型配置。</p></div></div>
 <section class="panel evaluation-form"><h2>准备与结果管理</h2><p>这是高级操作入口。材料须获评测授权；打开页面不会调用模型。先按项目文档准备 JSON 契约，保存返回的产物编号；原始文件和人工标签保留历史。</p>
  <label>操作<select :value="action" :disabled="busy" @change="action=($event.target as HTMLSelectElement).value"><option v-for="(label,key) in labels" :key="key" :value="key">{{label}}</option></select></label>
  <label>已确认的数据或配置 JSON<textarea :value="input" rows="10" :disabled="busy" placeholder="填写任务集、导出说明、配置或作业契约；不要填写密钥" @input="input=($event.target as HTMLTextAreaElement).value"/></label><el-button :disabled="busy||!input.trim()||mode()==='demo'" @click="perform">确认并保存 / 比较</el-button>
 </section>
 <section class="panel evaluation-form"><h2>显式执行真实回放</h2><p>只使用已登记任务的模型、材料及预算。失败尝试也扣减，继续原任务不重置。人工要求和答案不会发送给模型；结果默认尚未人工判定。旧代码不可用时拒绝替换。</p>
  <label>作业编号<input :value="jobId" :disabled="busy" @input="jobId=($event.target as HTMLInputElement).value"/></label><label class="confirm"><input type="checkbox" :checked="confirmed" :disabled="busy" @change="confirmed=($event.target as HTMLInputElement).checked"/> 我已查看该作业的材料授权、调用次数和期限，确认本次模型调用</label><el-button :disabled="busy||!confirmed||!jobId||mode()==='demo'||['queued','running'].includes(task?.status||'')" @click="execute">执行 / 继续固定作业</el-button><TaskProgress v-if="task" :task="task"/>
 </section>
 <section class="panel evaluation-form"><h2>查询不可变产物</h2><label>产物编号<input :value="artifactId" :disabled="busy" @input="artifactId=($event.target as HTMLInputElement).value"/></label><el-button :disabled="busy||!artifactId||mode()==='demo'" @click="read">读取已保存内容</el-button><pre v-if="result">{{JSON.stringify(result,null,2)}}</pre></section>
 <p v-if="error" class="error-text" role="alert">{{error}}</p><p class="note">定时任务、候选实验及发布/回滚使用独立 CLI；本页面不自动启用调度、发布或部署。查看模型后改标须记录背景，不作为独立准确率。</p>
</template>
<style scoped>.evaluation-form{padding:20px;margin:18px 0}.evaluation-form label{display:grid;gap:8px;margin:14px 0;font-size:13px}.evaluation-form input:not([type=checkbox]),.evaluation-form textarea,.evaluation-form select{font:inherit;border:1px solid #cbd5e1;border-radius:4px;padding:8px;max-width:100%;box-sizing:border-box}.evaluation-form .confirm{display:flex;align-items:center}.evaluation-form pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:600px;overflow:auto;line-height:1.7}</style>
