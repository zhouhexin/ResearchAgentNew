<script setup lang="ts">
import type {PublicationInfo} from '../types/research'
import {safeUrl} from '../api/utils'
defineProps<{publication?:PublicationInfo}>()
</script>
<template>
 <div v-if="publication" class="publication-info">
  <el-tag :type="publication.verification_status==='official_verified'?(['main','journal'].includes(publication.track)?'success':'warning'):'info'">{{publication.verification_status==='official_verified'?(publication.track==='main'?(publication.verification_provider||'CVF')+' 官方确认主会':publication.track==='journal'?(publication.verification_provider||'出版方')+' 确认期刊发表':publication.track==='workshop'?(publication.verification_provider||'CVF')+' 官方确认 Workshop':publication.track==='findings'?(publication.verification_provider||'CVF')+' 官方确认 Findings':publication.track==='other'?(publication.verification_provider||'官方论文集')+' 确认其他 Track':'官方确认发表，类别待确认'):'发表状态：索引记录，待核验'}}</el-tag>
  <a v-if="safeUrl(publication.verification_url)" :href="safeUrl(publication.verification_url)!" target="_blank" rel="noopener noreferrer">查看官方记录 ↗</a>
  <p class="note">{{publication.reason}}</p>
 </div>
</template>
<style scoped>.publication-info{margin:12px 0}.publication-info>a{margin-left:12px;font-size:13px}</style>
