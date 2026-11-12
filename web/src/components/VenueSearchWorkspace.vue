<script setup lang="ts">
import {ref,reactive,onMounted,onBeforeUnmount} from 'vue'
import {api,mode} from '../api'
import type {SearchPaper,PageResult,VenueSummary} from '../types/research'
import {safeUrl,validYears} from '../api/utils'
import PublicationStatus from './PublicationStatus.vue'
import AbstractTranslation from './AbstractTranslation.vue'
import VenueSelector from './VenueSelector.vue'
const year=new Date().getFullYear(),form=reactive({venues:['cvpr'] as string[],query:'',start_year:year-2,end_year:year,verified_main_only:false,page:1,page_size:5})
const result=ref<(PageResult<SearchPaper>&{notice:string})|null>(null),loading=ref(false),saving=ref(false),error=ref(''),notice=ref('')
const supported=ref<VenueSummary[]>([]),catalogLoading=ref(false),catalogError=ref('')
let version=0
async function loadCatalog(){
 if(mode()==='demo')return
 const current=version;catalogLoading.value=true;catalogError.value=''
 try{const value=await api.venues();if(current===version)supported.value=value.items}
 catch(e){if(current===version)catalogError.value=(e as Error).message}
 finally{if(current===version)catalogLoading.value=false}
}
function setVenues(values:string[]){form.venues=values;result.value=null;error.value='';notice.value=''}
onMounted(loadCatalog)
async function search(reset=true){
 if(loading.value||catalogLoading.value||mode()==='demo')return
 if(!form.venues.length||form.venues.some(slug=>!supported.value.some(v=>v.slug===slug))){error.value='请选择至少一个已支持的会议或期刊。';return}
 if(!form.query.trim()||!validYears(form.start_year,form.end_year)){error.value='请填写关键词，并检查年份范围。';return}
 const current=++version;loading.value=true;error.value='';notice.value=''
 if(reset)form.page=1
 try{const value=await api.venueSearch({...form,venues:form.venues.join(','),query:form.query.trim()});if(current===version)result.value=value}
 catch(e){if(current===version){error.value=(e as Error).message;result.value=null}}
 finally{if(current===version)loading.value=false}
}
async function save(paper:SearchPaper){
 const current=version;saving.value=true;error.value=''
 try{const value=await api.savePapers([paper]);if(current===version){notice.value=`新增 ${value.created} 篇，已有 ${value.existing} 篇，未收藏 ${value.rejected} 篇。可到论文库继续分析。`;if(!value.rejected)paper.saved_paper_id=-1}}
 catch(e){if(current===version)error.value=(e as Error).message}
 finally{if(current===version)saving.value=false}
}
onBeforeUnmount(()=>{version++})
</script>
<template>
 <section class="panel venue-search">
  <h2>顶刊顶会检索</h2><p class="muted">选择会议或期刊范围，检索相关论文并核对发表身份。<template v-if="supported.length">当前支持 {{supported.length}} 个会议与期刊。</template>目录等级暂未导入。</p>
  <VenueSelector :venues="supported" :model-value="form.venues" :disabled="loading||catalogLoading" @update:model-value="setVenues"/>
  <p v-if="catalogLoading" class="note">正在加载支持的检索范围…</p><p v-if="catalogError" class="error-text">{{catalogError}} <el-button @click="loadCatalog">重新读取支持范围</el-button></p>
  <div class="venue-form"><el-input v-model="form.query" :disabled="loading" placeholder="例如 monocular depth 或 vision transformer" aria-label="顶刊顶会关键词"/><label>正式发表年份<el-input-number v-model="form.start_year" :disabled="loading" :min="1900" :max="2100" aria-label="会议起始年份"/> — <el-input-number v-model="form.end_year" :disabled="loading" :min="1900" :max="2100" aria-label="会议截止年份"/></label>
  <label>每页候选数<el-input-number v-model="form.page_size" :min="1" :max="20" :disabled="loading" aria-label="会议每页候选数"/></label>
  <el-checkbox v-model="form.verified_main_only" :disabled="loading">仅显示正式来源核验的主会/期刊论文</el-checkbox><el-button type="primary" :disabled="mode()==='demo'||loading||catalogLoading||!supported.length" :loading="loading" @click="search()">检索论文</el-button></div>
  <p v-if="mode()==='demo'" class="note">顶刊顶会检索需要真实 API 模式，演示模式不生成候选或核验记录。</p>
  <p v-if="loading" class="note" role="status">正在检索并核对官方页面，首次查询可能较慢…</p>
  <p v-if="error" class="error-text" role="alert">{{error}}</p><p v-if="notice" role="status">{{notice}}</p>
  <template v-if="result"><p class="note">{{result.notice}}</p><p>索引候选 {{result.total}} 条，本页符合当前显示条件 {{result.items.length}} 篇。</p>
   <p v-if="!result.items.length" class="note">本页没有符合条件的论文。可查看下一页候选，或关闭正式发表过滤检查待核验记录。</p>
   <article v-for="paper in result.items" :key="`${paper.venue_slug}:${paper.openalex_id||paper.doi||paper.title}`" class="venue-paper"><h3><a v-if="safeUrl(paper.url)" :href="safeUrl(paper.url)!" target="_blank" rel="noopener noreferrer">{{paper.title}} ↗</a><span v-else>{{paper.title}}</span></h3><p class="note">{{paper.venue_slug?.toUpperCase()}} · {{paper.year}} · {{paper.authors.join('、')}}</p><PublicationStatus :publication="paper.publication"/><details><summary>原文摘要与中文翻译</summary><AbstractTranslation :title="paper.title" :abstract="paper.abstract"/></details><el-button :disabled="saving||!!paper.saved_paper_id||!paper.identity_verified" @click="save(paper)">{{paper.saved_paper_id?'已收藏':'收藏论文'}}</el-button></article>
   <div class="venue-pagination"><el-button :disabled="loading||form.page<=1" @click="form.page--;search(false)">上一页</el-button><span>第 {{result.page}} 页候选</span><el-button :disabled="loading||form.page*form.page_size>=result.total" @click="form.page++;search(false)">下一页</el-button></div>
  </template>
 </section>
</template>
<style scoped>.venue-search{padding:26px}.venue-form{display:flex;gap:18px;flex-wrap:wrap;align-items:center}.venue-form>.el-input{width:100%}.venue-form label{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px}.venue-paper{border-top:1px solid #e5ebf1;margin-top:22px;padding-top:15px}.venue-paper h3{line-height:1.7;font-size:16px}.venue-paper details{margin-bottom:14px;line-height:1.7}.venue-pagination{display:flex;gap:16px;align-items:center;margin-top:22px}</style>
