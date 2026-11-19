<script setup lang="ts">
import {useRoute,useRouter} from 'vue-router'
import ResearchPlanWorkspace from '../components/ResearchPlanWorkspace.vue'
const route=useRoute(),router=useRouter()
function opened(id:string){if(route.query.planId!==id)void router.replace({query:{planId:id}})}
</script>
<template>
 <div class="page-heading"><div><div class="eyebrow">FROM GOAL TO PAPER CANDIDATES</div><h1>科研 Agent</h1><p>理解目标、确认计划，从已有文献库选文或检索筛选、阅读全文并分析，围绕研究问题比较论文、生成综述和导出。</p></div><span class="muted">计划 → 选文 → 分析 → 比较 → 综述 → 导出</span></div>
 <ResearchPlanWorkspace :initial-plan-id="typeof route.query.planId==='string'?route.query.planId:undefined" :initial-task-id="typeof route.query.planningTask==='string'?route.query.planningTask:undefined" :initial-search-run-id="typeof route.query.searchRun==='string'?route.query.searchRun:undefined" @opened="opened"/>
</template>
