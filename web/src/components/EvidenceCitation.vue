<script setup lang="ts">
import {computed} from 'vue'
import {parseCitation} from '../api/utils'
import {useEvidenceStore} from '../stores/evidence'
import type {ReviewCitation} from '../types/research'
const props=defineProps<{refId:string;citation?:ReviewCitation}>()
const store=useEvidenceStore()
const label=computed(()=>props.citation?`[${props.citation.paper_number}，${props.citation.page_number===null?'无页码':`第${props.citation.page_number}页`}]`:props.refId)
</script>
<template><button v-if="parseCitation(refId)" class="citation" type="button" :title="citation?`${citation.paper_title} · ${refId}`:refId" :aria-label="`查看原文 ${label}`" @click="store.open(refId,citation)">{{label}}</button><span v-else class="muted">[无效引用]</span></template>
