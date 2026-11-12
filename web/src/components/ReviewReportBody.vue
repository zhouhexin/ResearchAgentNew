<script setup lang="ts">
import {computed} from 'vue'
import {reviewSections} from '../types/research'
import type {ReviewDraft,ReviewResult} from '../types/research'
import EvidenceCitation from './EvidenceCitation.vue'
const props=defineProps<{draft:ReviewDraft;review?:ReviewResult}>()
const sections=computed(()=>Object.entries(reviewSections).filter(([key])=>Array.isArray(props.draft[key as keyof typeof reviewSections])))
const references=computed(()=>props.review?.paper_ids.map((id,i)=>({number:i+1,...props.review!.matrix.find(p=>p.paper_id===id)}))||[])
</script>
<template>
 <section v-for="([key,label],i) in sections" :key="key" class="review-section">
  <h2><span>{{String(i+1).padStart(2,'0')}}</span>{{label}}</h2>
  <p v-if="!draft[key as keyof typeof reviewSections]?.length" class="muted">现有证据不足，暂不作结论。</p>
  <div v-for="(point,j) in draft[key as keyof typeof reviewSections]" :key="j" class="review-point">
   <h3 v-if="point.heading?.trim()">{{point.heading}}</h3>
   <p class="review-claim">{{point.claim}}</p>
   <div class="citation-row"><EvidenceCitation v-for="refId in point.evidence_refs" :key="refId" :ref-id="refId" :citation="review?.citation_index?.[refId]"/></div>
   <div v-for="(paragraph,k) in point.paragraphs||[]" :key="k" class="review-explanation">
    <p>{{paragraph.text}}</p>
    <div class="citation-row"><EvidenceCitation v-for="refId in paragraph.evidence_refs" :key="refId" :ref-id="refId" :citation="review?.citation_index?.[refId]"/></div>
   </div>
  </div>
 </section>
 <section v-if="references.length" class="review-references">
  <h2>参考文献</h2>
  <p class="note">引用页码为 PDF 文件页码；点击引用查看完整引句及原文上下文。</p>
  <p v-for="paper in references" :key="paper.number"><strong>[{{paper.number}}]</strong> {{paper.title||'论文信息缺失'}}<span v-if="paper.doi" class="muted"> · DOI: {{paper.doi}}</span></p>
 </section>
</template>
<style scoped>
.review-point{padding-bottom:22px;margin-bottom:26px}
.review-point h3{font-size:17px;margin:0 0 12px;color:#263a50}
.review-claim{font-weight:550}
.review-explanation{margin-top:18px}
.review-explanation p{font-size:15px;line-height:2;white-space:pre-line;color:#354357;margin-bottom:10px}
.citation-row{margin-bottom:12px}
.review-references{border-top:1px solid #e4e9ef;padding-top:20px}
.review-references p{overflow-wrap:anywhere}
@media(max-width:600px){.review-point h3{font-size:15px}.review-explanation p{font-size:14px;line-height:1.9}}
</style>
