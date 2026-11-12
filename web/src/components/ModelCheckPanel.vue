<script setup lang="ts">
import {computed,onMounted,ref,watch} from 'vue'
import {useRoute} from 'vue-router'
import {ElMessage} from 'element-plus'
import {api,mode} from '../api'
import type {Health,HttpBackend,ModelCheckMode} from '../types/research'
import {useTaskStore} from '../stores/tasks'
import TaskProgress from './TaskProgress.vue'

defineProps<{health?:Health}>()
const route=useRoute(),store=useTaskStore()
const checkMode=ref<ModelCheckMode>('structured'),backend=ref<'current'|HttpBackend>('current')
const taskId=ref(''),submitting=ref(false),restoring=ref(false),error=ref('')
const historical=ref(false)
const task=computed(()=>store.tasks[taskId.value])
const active=computed(()=>!!task.value&&['queued','running'].includes(task.value.status))
let selection=0
function filters(){return {mode:checkMode.value,...(backend.value==='current'?{}:{http_backend:backend.value})}}
async function restore(){
 const current=++selection
 taskId.value='';error.value='';historical.value=false
 if(mode()==='demo')return
 restoring.value=true
 try{
  const latest=await api.latestModelCheck(filters())
  if(current!==selection)return
  if(latest){taskId.value=latest.id;await store.track(latest.id)}
 }catch(e){if(current===selection)error.value=(e as Error).message}
 finally{if(current===selection)restoring.value=false}
}
async function start(){
 if(submitting.value||active.value)return
 submitting.value=true;error.value='';historical.value=false
 try{
  const response=await api.startModelCheck({...filters(),timeout:60})
  taskId.value=response.task_id
  if(response.reused)ElMessage.info('相同检测正在执行，已恢复进度')
  await store.track(response.task_id)
 }catch(e){error.value=(e as Error).message}
 finally{submitting.value=false}
}
watch([checkMode,backend],restore)
onMounted(async()=>{
 if(route.query.modelCheck&&mode()!=='demo'){
  restoring.value=true
  try{
   const selected=await api.task(String(route.query.modelCheck))
   if(selected.type!=='model_diagnostic')throw new Error('这项任务不是模型检测')
   // 不更改筛选条件，历史任务按它自己的模型和传输信息展示。
   historical.value=true;taskId.value=selected.id;await store.track(selected.id)
  }catch(e){error.value=(e as Error).message}
  finally{restoring.value=false}
 }else await restore()
})
</script>

<template>
 <section class="panel settings-panel model-check-panel">
  <h2>模型连接检测</h2>
  <p class="muted">在当前后端发起真实请求，区分普通对话连通与结构化分析能力。</p>
  <div class="model-check-controls">
   <label>检查内容<el-select v-model="checkMode" aria-label="模型检测内容" :disabled="active||submitting">
    <el-option label="论文结构与原文引用" value="structured"/><el-option label="普通对话" value="chat"/>
   </el-select></label>
   <label>连接方式<el-select v-model="backend" aria-label="模型检测传输" :disabled="active||submitting">
    <el-option :label="`当前配置${health?.model_http_backend?'（'+health.model_http_backend+'）':''}`" value="current"/>
    <template v-if="health?.model_http_backend!=='codex'"><el-option label="SDK 默认传输" value="sdk"/><el-option label="requests 传输" value="requests"/></template>
   </el-select></label>
   <el-button type="primary" :loading="submitting||active" :disabled="mode()==='demo'||health?.model_configured===false||restoring" @click="start">
    {{active?'检测中':'开始检测'}}
   </el-button>
   <el-button :disabled="submitting||active||restoring" @click="restore">读取最近结果</el-button>
  </div>
  <p v-if="mode()==='demo'" class="note">请切换到真实 API 模式后检测；演示模式不发送模型请求。</p>
  <p v-else-if="health?.model_http_backend==='codex'" class="note">使用本机 Codex 的 ChatGPT 登录，计入该账号用量。单次调用时限为 60 秒；结构检查使用两段内置样例，不访问论文数据库。</p>
  <p v-else class="note">检测会发送一次模型请求。结构检查使用两段内置样例，读取超时设为 60 秒，不访问论文数据库，也不修改模型配置。</p>
  <p v-if="error" role="alert" class="error-text">{{error}}</p>
  <p v-if="restoring" class="note" aria-live="polite">正在读取检测记录…</p>
  <p v-if="store.error&&taskId" role="alert" class="error-text">状态更新失败：{{store.error}}。可在任务中心刷新查看。</p>
  <div v-if="task" class="model-check-result" aria-live="polite">
   <p v-if="historical" class="note">正在查看任务中心选中的检测记录。下次检测将使用上方选择的配置。</p>
   <TaskProgress :task="task"/>
   <p v-if="task.model_check_result" class="note">检测耗时 {{task.model_check_result.seconds}} 秒<span v-if="task.model_check_result.evidence_validated"> · {{task.model_check_result.findings}} 条样例结论、{{task.model_check_result.quotes}} 条引文通过核验</span> · 更新于 {{new Date(task.updated_at).toLocaleString('zh-CN')}}</p>
   <p v-if="task.status==='succeeded'" class="note">{{task.check_mode==='structured'?'本次样例的结构和引文检查通过，真实论文仍按各批次独立核验。':'普通对话连接通过；可切换到论文结构与原文引用检查，进一步验证分析能力。'}}</p>
  </div>
  <p v-else-if="!restoring&&!error&&mode()!=='demo'" class="note">当前配置与检查内容暂无检测记录。</p>
 </section>
</template>

<style scoped>
.model-check-panel{grid-column:1/-1}
.model-check-controls{display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap;margin:22px 0 16px}
.model-check-controls label{display:flex;flex-direction:column;gap:8px;font-size:12px;color:#66758b}
.model-check-controls .el-select{width:205px}
.model-check-controls>.el-button{margin:0}
.model-check-result{margin-top:18px}
@media(max-width:600px){.model-check-controls label,.model-check-controls .el-select{width:100%}}
</style>
