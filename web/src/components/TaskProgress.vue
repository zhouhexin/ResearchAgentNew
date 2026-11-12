<script setup lang="ts">
import {computed} from 'vue'
import type {TaskInfo} from '../types/research'
import {statusNames} from '../types/research'
import {modelStageName} from '../api/modelDiagnostics'
const props=defineProps<{task:TaskInfo}>()
const diagnostic=computed(()=>props.task.error?.diagnostic)
const pdfDiagnostic=computed(()=>diagnostic.value?.received_bytes!==undefined?diagnostic.value:null)
const connectionDetails=computed(()=>!pdfDiagnostic.value&&diagnostic.value&&(diagnostic.value.host||diagnostic.value.network||diagnostic.value.phase))
const mb=(value:number)=>value<1024?`${value} B`:value<1024*1024?`${(value/1024).toFixed(1)} KB`:`${(value/1024/1024).toFixed(2)} MB`
const pdfPhases:Record<string,string>={resolve:'域名解析',connect:'建立连接',response_headers:'连接或等待响应',download:'接收 PDF',completed:'下载完成'}
const pdfPhase=(phase?:string)=>pdfPhases[phase||'']||'未知阶段'
const contentProof=(value:import('../types/research').ModelDiagnostic)=>value.pdf_header_verified===true?(value.phase==='completed'?'PDF 数据已接收完成':'已确认 PDF 文件头，文件尚未完整下载'):value.response_received===true?'已收到 HTTP 响应，尚未确认 PDF 文件头':value.candidate_found===true?'已找到候选地址，尚未收到 HTTP 响应':'旧记录未提供文件类型核验信息'
</script>
<template>
 <div class="task-progress panel">
  <div class="flex-between"><strong>{{statusNames[task.status]}}</strong><code>{{task.id.slice(0,20)}}</code></div>
  <el-progress v-if="task.progress!==null" :percentage="task.progress" :status="task.status==='failed'?'exception':task.status==='succeeded'?'success':undefined" :stroke-width="5"/>
  <p v-else>进度未知，请稍候…</p>
  <p class="muted">{{task.stage||'等待任务更新'}}</p>
  <p v-if="task.model_name" class="note">模型：{{task.model_name}}<span v-if="task.http_backend"> · 连接方式：{{task.http_backend}}</span><span v-if="task.read_timeout"> · {{task.http_backend==='codex'?'单次调用时限':'读取超时'}}：{{task.read_timeout}} 秒</span></p>
  <p v-if="task.check_mode" class="note">检查内容：{{task.check_mode==='structured'?'论文结构与原文引用':'普通对话'}}</p>
  <p v-if="task.total_batches" class="note">已保存 {{task.completed_batches||0}} / {{task.total_batches}} 批<span v-if="task.batch_attempt"> · 当前批次第 {{task.batch_attempt}} 次尝试</span><span v-if="task.resumed_batches"> · 复用 {{task.resumed_batches}} 批已完成结果</span></p>
  <p v-if="task.download_provider" class="note">下载来源：{{task.download_provider}}<span v-if="task.download_provider==='arXiv'">（可能为预印本版本）</span></p>
  <p v-if="task.downloaded&&task.status==='failed'" class="note">PDF 已下载到本地，但全文尚未完成解析或保存。</p>
  <p v-if="task.download_progress" class="note">下载站点：{{task.download_progress.host||'未知'}} · {{mb(task.download_progress.received_bytes)}}<span v-if="task.download_progress.total_bytes"> / {{mb(task.download_progress.total_bytes)}}</span> · 已用 {{task.download_progress.elapsed_seconds}} 秒 · 平均 {{((task.download_progress.average_bytes_per_second||0)/1024).toFixed(1)}} KB/s</p>
  <p v-if="task.download_progress" class="note">{{contentProof(task.download_progress)}}<span v-if="task.download_progress.http_status"> · HTTP {{task.download_progress.http_status}}</span></p>
  <p v-if="task.error" class="error-text">{{task.error.code}} · {{task.error.message}}</p>
  <details v-if="pdfDiagnostic" class="connection-details"><summary>查看 PDF 下载诊断</summary><dl><dt>下载站点</dt><dd>{{pdfDiagnostic.host||'未知'}}</dd><dt>失败阶段</dt><dd>{{pdfPhase(pdfDiagnostic.phase)}}</dd><dt>已接收</dt><dd>{{mb(pdfDiagnostic.received_bytes||0)}}<span v-if="pdfDiagnostic.total_bytes"> / {{mb(pdfDiagnostic.total_bytes)}}</span></dd><dt>耗时</dt><dd>{{pdfDiagnostic.elapsed_seconds}} 秒</dd><dt>总时限</dt><dd>{{pdfDiagnostic.total_timeout}} 秒</dd><dt>等待数据</dt><dd>{{pdfDiagnostic.read_timeout}} 秒</dd></dl></details>
  <details v-if="connectionDetails&&diagnostic" class="connection-details">
   <summary>查看连接诊断</summary>
   <dl>
    <dt>失败阶段</dt><dd>{{modelStageName(diagnostic)}}</dd>
    <template v-if="diagnostic.host"><dt>模型服务</dt><dd>{{diagnostic.host}}</dd></template>
    <template v-if="diagnostic.network?.backend"><dt>实际传输</dt><dd>{{diagnostic.network.backend}}</dd></template>
    <template v-if="diagnostic.network?.peer_ip"><dt>连接节点</dt><dd>{{diagnostic.network.peer_ip}}<span v-if="diagnostic.network.peer_port">:{{diagnostic.network.peer_port}}</span></dd></template>
    <template v-if="diagnostic.causes?.length"><dt>异常链</dt><dd>{{diagnostic.causes.join(' → ')}}</dd></template>
   </dl>
  </details>
  <div v-for="source in task.source_results||[]" :key="source.provider" class="note"><strong>{{source.provider}}：</strong>{{source.message}}<p v-if="source.lookup_status==='found'">已取得 {{source.candidate_count}} 个候选地址；{{source.diagnostic?contentProof(source.diagnostic):'返回内容尚待检查。'}}</p><p v-else-if="source.lookup_status==='unavailable'">未取得可用下载地址；这不能证明论文文件不存在。</p><p v-else-if="source.lookup_status==='failed'">查找地址失败，尚无法判断文件是否可下载。</p></div>
  <RouterLink to="/tasks">前往任务中心 →</RouterLink>
 </div>
</template>
<style scoped>
.connection-details{font-size:12px;margin:15px 0;color:#67788e}
.connection-details summary{cursor:pointer}
.connection-details dl{display:grid;grid-template-columns:70px 1fr;gap:9px;margin:13px 0}
.connection-details dt{color:#8b9ab0}
.connection-details dd{margin:0;overflow-wrap:anywhere}
</style>
