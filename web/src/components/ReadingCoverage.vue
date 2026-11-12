<script setup lang="ts">
import type {ReadingSelection} from '../types/research'
defineProps<{selection?:ReadingSelection}>()
</script>
<template><div v-if="selection" class="reading-coverage"><strong>快速阅读 · 正文关键章节优先</strong><p>{{selection.note}}</p><p>阅读 {{selection.selected_chunks}} / {{selection.total_chunks}} 个片段<span v-if="selection.selected_pages.length"> · 页码 {{selection.selected_pages.join('、')}}</span></p><p>已覆盖：{{selection.sections.filter(s=>s.selected_chunks>0).map(s=>`${s.label}（${s.selected_chunks}段）`).join('、')}}</p><p v-if="selection.missing_sections.length" class="missing">未识别到章节标题：{{selection.missing_sections.join('、')}}。相关内容可能以其他标题出现，需人工核对。</p></div></template>
<style scoped>.reading-coverage{padding:12px 16px;margin:14px 0;background:#f5f8fb;border:1px solid #e1e7ef;border-radius:5px;font-size:12px;line-height:1.8}.reading-coverage p{margin:5px 0;overflow-wrap:anywhere}.missing{color:#805e28}</style>
