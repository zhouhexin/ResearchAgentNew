<script setup lang="ts">
import {computed} from 'vue'
const props=defineProps<{fileUrl:string|null;page:number;pageCount:number|null}>()
const emit=defineEmits<{pageChange:[number]}>()
const url=computed(()=>props.fileUrl?props.fileUrl.split('#')[0]+`#page=${props.page}&toolbar=1`:null)
</script>
<template>
 <div class="pdf-viewer">
  <div class="pdf-toolbar"><span>PDF 原文</span><div class="button-row">
   <el-button size="small" :disabled="page<=1" @click="emit('pageChange',page-1)">上一页</el-button>
   <el-input-number :model-value="page" :min="1" :max="pageCount||9999" :controls="false" aria-label="PDF 跳转页码" @change="emit('pageChange',Number($event)||1)"/>
   <span>/ {{pageCount||'未知'}}</span>
   <el-button size="small" :disabled="!!pageCount&&page>=pageCount" @click="emit('pageChange',page+1)">下一页</el-button>
  </div></div>
  <iframe v-if="url" :key="url" :src="url" title="论文 PDF 原文预览"/>
  <div v-else class="pdf-empty"><span class="large-document">PDF</span><h3>尚无可预览的 PDF 文件</h3><p>该全文可能是网页，或原 PDF 已移动、删除。也可选择本地 PDF 预览。</p><p class="note">已保存的原文片段仍可在“证据片段”中查看。</p></div>
 </div>
</template>
