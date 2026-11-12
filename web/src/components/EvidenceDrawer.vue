<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import {useEvidenceStore} from '../stores/evidence'
import {useRouter} from 'vue-router'
import {safeUrl} from '../api/utils'
import AsyncState from './AsyncState.vue'
import PdfViewer from './PdfViewer.vue'
const s=useEvidenceStore(),router=useRouter()
const matchesCurrent=computed(()=>!s.citation||!s.evidence||s.citation.quotes.every(q=>s.evidence!.quote.includes(q)))
const pdfUrl=computed(()=>safeUrl(s.evidence?.pdf_file_url))
const pdfPage=ref(1)
watch(()=>s.evidence,record=>{
 const page=record?.page_number
 pdfPage.value=page&&Number.isInteger(page)&&page>0?Math.min(page,record?.page_count||page):1
},{immediate:true})
function go(){const e=s.evidence;if(e){s.visible=false;router.push({path:`/papers/${e.paper_id}`,query:{tab:'evidence',evidenceId:e.evidence_id}})}}
</script>
<template>
 <el-drawer v-model="s.visible" :title="pdfUrl?'PDF 原文与证据':'原文证据'" :size="pdfUrl?'min(1120px, 100vw)':'440px'" class="evidence-drawer">
  <div class="reader-layout" :class="{'with-pdf':pdfUrl}">
  <section v-if="pdfUrl" class="citation-pdf" aria-label="引用对应的 PDF 阅读器">
   <PdfViewer :file-url="pdfUrl" :page="pdfPage" :page-count="s.evidence?.page_count||null" @page-change="pdfPage=$event"/>
   <p v-if="s.evidence?.page_number===null" class="note">该片段未记录页码，已打开 PDF 首页，可手动翻页查看。</p>
   <p v-else class="note">引用位置：PDF 第 {{s.evidence?.page_number}} 页。上方页码用于跳转，不随阅读器内滚动更新。</p>
  </section>
  <div class="citation-details">
  <section v-if="s.citation" class="cited-quotes">
   <h2>[{{s.citation.paper_number}}] {{s.citation.paper_title}}</h2>
   <p class="source-meta">{{s.citation.page_number===null?'无页码':`PDF · 第 ${s.citation.page_number} 页`}}</p>
   <h3>{{s.citation.quote_kind==='analysis_quotes'?'关联分析中的完整原文引句':'完整原文片段'}}</h3>
   <p v-if="s.citation.quotes.length>1" class="note">此片段关联多个已核验引句，以下全部列出，具体支持关系请结合综述内容判断。</p>
   <blockquote v-for="(quote,i) in s.citation.quotes" :key="i" class="evidence-quote">{{quote}}</blockquote>
   <p class="note">内部定位：<code>{{s.reference}}</code>。以上内容来自生成本报告时保存的快照。</p>
  </section>
  <AsyncState :loading="s.loading" :error="s.error" @retry="s.open(s.reference,s.citation)">
   <template v-if="s.evidence">
    <p v-if="!pdfUrl&&s.evidence.source_type==='pdf'" class="note warning">原 PDF 文件暂不可读取，仍可查看下方已保存的原文证据。</p>
    <p v-if="!matchesCurrent" class="note warning" role="alert">当前原文片段与报告引句不一致；请将上方内容作为历史快照查看，并检查原文版本。</p>
    <div class="drawer-title"><code>{{s.evidence.evidence_id}}</code><el-tag :type="s.evidence.verification==='location_verified'?'success':'warning'" effect="plain">{{s.evidence.verification==='location_verified'?'当前片段位置已核验':'待后端核验'}}</el-tag></div>
    <h2 v-if="!s.citation">{{s.evidence.paper_title}}</h2>
    <div class="source-meta"><span>{{s.evidence.page_number===null?'网页 · 无页码':`PDF · 第 ${s.evidence.page_number} 页`}}</span><span>{{s.evidence.section_hint||'未识别章节'}}</span></div>
    <details v-if="s.citation" class="source-context"><summary>查看当前原文上下文</summary><div class="evidence-quote">{{s.evidence.quote}}</div></details>
    <div v-else class="evidence-quote">{{s.evidence.quote}}</div>
    <dl class="details-list"><dt>片段范围</dt><dd>{{s.evidence.verification==='location_verified'?`${s.evidence.start_char}–${s.evidence.end_char}`:`未核验（记录范围 ${s.evidence.start_char}–${s.evidence.end_char}）`}}</dd><dt>来源</dt><dd><a v-if="safeUrl(s.evidence.source_url)" :href="safeUrl(s.evidence.source_url)!" target="_blank" rel="noopener noreferrer">打开来源 ↗</a><span v-else>{{s.evidence.source_url}}</span></dd></dl>
    <p class="note">位置核验仅表示来源与原文位置一致，不代表结论已获得充分的语义支持。</p>
    <el-button type="primary" @click="go">查看对应论文 <span>↗</span></el-button>
   </template>
  </AsyncState>
  </div>
  </div>
 </el-drawer>
</template>
<style scoped>
.cited-quotes{margin-bottom:24px}
.cited-quotes h3{font-size:14px;margin-top:20px}
.evidence-quote{white-space:pre-wrap;overflow-wrap:anywhere;margin:12px 0}
.source-context summary{cursor:pointer;padding:10px 0;color:#365a7c}
.reader-layout.with-pdf{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:24px;align-items:start}
.citation-pdf,.citation-details{min-width:0}
.citation-pdf :deep(iframe){height:calc(100dvh - 225px);min-height:360px}
.citation-pdf :deep(.pdf-toolbar){flex-wrap:wrap;gap:8px}
.citation-pdf :deep(.button-row){gap:6px}
@media(max-width:800px){.reader-layout.with-pdf{grid-template-columns:minmax(0,1fr)}.citation-pdf :deep(iframe){height:60dvh;min-height:320px}}
</style>
