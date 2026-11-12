<script setup lang="ts">
import {computed,ref,watch,onBeforeUnmount} from 'vue'
import {api,mode} from '../api'
import {useTaskStore} from '../stores/tasks'
import type {AbstractTranslationRecord,AbstractTranslationResponse} from '../types/translation'
import type {TaskInfo} from '../types/research'
const props=withDefaults(defineProps<{abstract?:string;title?:string;initialTranslationId?:string;showOriginal?:boolean;showTitle?:boolean}>(),{abstract:'',title:'',showOriginal:true,showTitle:false})
const store=useTaskStore(),record=ref<AbstractTranslationRecord|null>(null),loadedTask=ref<TaskInfo|null>(null)
const creating=ref(false),restoring=ref(false),error=ref('')
const task=computed(()=>record.value?(store.tasks[record.value.task_id]||loadedTask.value):null)
const active=computed(()=>!!task.value&&['queued','running'].includes(task.value.status))
const original=computed(()=>props.initialTranslationId?(record.value?.original_abstract||''):props.abstract)
const title=computed(()=>props.initialTranslationId?(record.value?.title||''):props.title)
const available=computed(()=>original.value.trim().length>0&&original.value.length<=24000)
let version=0,readVersion=0
async function apply(value:AbstractTranslationResponse){
 record.value=value.record;loadedTask.value=value.task
 if(value.task&&['queued','running'].includes(value.task.status))await store.track(value.task.id)
}
async function restore(){
 const current=++version;readVersion++;record.value=null;loadedTask.value=null;error.value='';creating.value=false;restoring.value=false
 if(mode()==='demo'||(!props.initialTranslationId&&!props.abstract.trim()))return
 if(!props.initialTranslationId&&props.abstract.length>24000)return
 restoring.value=true
 try{
  const value=props.initialTranslationId?await api.abstractTranslation(props.initialTranslationId):await api.lookupAbstractTranslation(props.title,props.abstract)
  if(current!==version)return
  if(value)await apply(value)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)restoring.value=false}
}
async function refresh(){
 const current=version,request=++readVersion,id=record.value?.id
 if(!id)return
 try{
  const value=await api.abstractTranslation(id)
  if(current!==version||request!==readVersion||record.value?.id!==id)return
  // 轮询读取只更新记录，不再次启动任务轮询。
  record.value=value.record;loadedTask.value=value.task
 }catch(e){if(current===version&&request===readVersion)error.value=(e as Error).message}
}
async function translate(){
 if(!available.value||active.value||creating.value||restoring.value||mode()==='demo')return
 const current=version;creating.value=true;error.value=''
 try{
  const response=await api.startAbstractTranslation(title.value,original.value)
  if(current!==version)return
  const value=await api.abstractTranslation(response.translation_id)
  if(current!==version)return
  await apply(value)
 }catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)creating.value=false}
}
watch(()=>[props.abstract,props.title,props.initialTranslationId],restore,{immediate:true})
watch([()=>task.value?.id,()=>task.value?.status],()=>{if(task.value?.status==='succeeded')void refresh()})
onBeforeUnmount(()=>{version++;readVersion++})
</script>
<template>
 <div class="abstract-translation">
  <h3 v-if="showTitle">{{title||'摘要翻译'}}</h3>
  <template v-if="showOriginal"><h4>原文摘要</h4><p class="abstract-original">{{original||'暂无摘要'}}</p></template>
  <div class="translation-heading"><h4>中文翻译</h4><el-button v-if="!record?.translation_zh" size="small" :disabled="!available||creating||restoring||active||mode()==='demo'" :loading="creating||active" @click="translate">{{task?.status==='failed'?'重试中文翻译':'生成中文翻译'}}</el-button></div>
  <p v-if="record?.translation_zh" class="translation-text">{{record.translation_zh}}</p>
  <p v-else-if="active" class="note" role="status">正在生成中文翻译，可继续阅读原文。</p>
  <p v-else-if="restoring" class="note">正在读取已保存译文…</p>
  <p v-else-if="!original.trim()" class="note">没有原文摘要，无法翻译。</p>
  <p v-else-if="!available" class="note">摘要超过24000字符，本次未截断原文或生成翻译。</p>
  <p v-else class="note">点击后使用当前模型生成中文译文；译文仅辅助阅读，引用请核对原文。</p>
  <p v-if="record?.translation_zh" class="note">机器翻译 · {{record.model_name}} · 引用请核对原文。</p>
  <p v-if="mode()==='demo'" class="note">中文翻译需要真实 API 模式。</p>
  <p v-if="error" class="error-text" role="alert">{{error}}</p>
  <p v-if="task?.status==='failed'&&task.error" class="error-text" role="alert">{{task.error.code}} · {{task.error.message}}</p>
  <p v-if="store.error&&active" class="error-text">翻译进度更新失败：{{store.error}}。可在任务中心刷新查看。</p>
 </div>
</template>
<style scoped>.abstract-translation{margin:14px 0}.abstract-translation h4{font-size:13px;margin:10px 0;color:#526278}.translation-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.abstract-original,.translation-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8;font-size:14px}.translation-text{padding:12px 14px;background:#f5f8fb;border-radius:6px}</style>
