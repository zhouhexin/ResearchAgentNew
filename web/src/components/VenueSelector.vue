<script setup lang="ts">
import {computed,ref,useId} from 'vue'
import type {VenueSummary} from '../types/research'
import {safeUrl} from '../api/utils'

const props=defineProps<{venues:VenueSummary[];modelValue:string[];disabled?:boolean}>()
const emit=defineEmits<{(e:'update:modelValue',value:string[]):void}>()
const expanded=ref(false),keyword=ref(''),panelId=useId()
const names:Record<string,string>={computer_vision:'计算机视觉',natural_language_processing:'自然语言处理',machine_learning:'机器学习',artificial_intelligence:'人工智能',information_retrieval:'信息检索'}
const label=(venue:VenueSummary)=>venue.venue_type==='journal'?venue.name:venue.acronym
const selected=computed(()=>props.venues.filter(venue=>props.modelValue.includes(venue.slug)))
const filteredVenues=computed(()=>{
 const query=keyword.value.trim().toLocaleLowerCase()
 return props.venues.filter(venue=>!query||`${venue.acronym} ${venue.name} ${names[venue.field]||'其他方向'}`.toLocaleLowerCase().includes(query))
})
function toggle(slug:string,checked:boolean){
 if(props.disabled)return
 emit('update:modelValue',checked?[...new Set([...props.modelValue,slug])]:props.modelValue.filter(value=>value!==slug))
}
</script>

<template>
 <section class="venue-selector" aria-label="检索范围（可多选）">
  <div class="selector-header">
   <div><strong>检索范围</strong><span class="selection-count">已选 {{modelValue.length}} / {{venues.length}}</span></div>
   <button type="button" class="expand-button" :aria-expanded="expanded" :aria-controls="panelId" @click="expanded=!expanded">{{expanded?'收起范围':'展开选择'}} <span aria-hidden="true">{{expanded?'⌃':'⌄'}}</span></button>
  </div>
  <div class="selected-venues" aria-live="polite">
   <span v-if="!modelValue.length" class="empty-selection">尚未选择会议或期刊，请展开选择。</span>
   <span v-for="venue in selected.slice(0,4)" :key="venue.slug" class="selected-chip">{{label(venue)}}</span>
   <span v-if="selected.length>4" class="more-selected">另 {{selected.length-4}} 项</span>
  </div>
  <div v-if="expanded" :id="panelId" class="selector-panel">
   <div class="selector-toolbar">
    <input :value="keyword" @input="keyword=($event.target as HTMLInputElement).value" type="search" class="venue-filter" placeholder="搜索会议、期刊或研究方向" aria-label="筛选会议或期刊"/>
    <button type="button" class="clear-button" :disabled="disabled||!modelValue.length" @click="emit('update:modelValue',[])">清空选择</button>
   </div>
   <div class="venue-list">
    <div class="venue-columns" aria-hidden="true"><span>会议 / 期刊</span><span class="venue-field">研究方向</span><span>类型</span><span>链接</span></div>
    <div v-for="venue in filteredVenues" :key="venue.slug" class="venue-row" :class="{'is-selected':modelValue.includes(venue.slug)}">
     <label :title="venue.name"><input type="checkbox" :checked="modelValue.includes(venue.slug)" :disabled="disabled" :aria-label="`检索 ${venue.acronym}`" @change="toggle(venue.slug,($event.target as HTMLInputElement).checked)"/><span class="venue-label">{{label(venue)}}</span></label>
     <span class="venue-field">{{names[venue.field]||'其他方向'}}</span>
     <span class="venue-type">{{venue.venue_type==='journal'?'期刊':'会议'}}</span>
     <a v-if="safeUrl(venue.official_url)" :href="safeUrl(venue.official_url)!" target="_blank" rel="noopener noreferrer" :aria-label="`${label(venue)} 官方网站`">官网 ↗</a><span v-else>—</span>
    </div>
    <p v-if="!filteredVenues.length" class="no-matches">没有匹配的会议或期刊。清除搜索词可查看全部范围。</p>
   </div>
   <p class="selector-hint">支持多选；筛选列表和收起面板都会保留已选范围。</p>
  </div>
 </section>
</template>

<style scoped>
.venue-selector{margin:20px 0 24px;border:1px solid #e1e7ef;border-radius:6px;background:#fff;min-width:0}
.selector-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px 8px}.selector-header strong{font-size:14px;color:#293b50}.selection-count{margin-left:12px;font-size:12px;color:#68798c}
button{font:inherit;cursor:pointer}.expand-button,.clear-button{border:0;background:transparent;color:#315f91;font-size:13px;padding:6px 4px;white-space:nowrap}.expand-button span{margin-left:6px}.clear-button:disabled{color:#9ba5b0;cursor:default}
.selected-venues{display:flex;align-items:center;flex-wrap:wrap;gap:6px;padding:0 16px 12px;min-height:24px}.selected-chip{font-size:12px;line-height:1.6;padding:2px 8px;background:#f1f4f7;color:#526278;border-radius:3px;overflow-wrap:anywhere}.more-selected,.empty-selection{font-size:12px;color:#697b8f}
.selector-panel{border-top:1px solid #e8edf3;padding:14px 16px}.selector-toolbar{display:flex;align-items:center;gap:16px;margin-bottom:12px}.venue-filter{font:inherit;font-size:13px;padding:8px 10px;border:1px solid #cdd8e4;border-radius:4px;flex:1;min-width:0;background:#fff;color:#293b50}
.venue-list{max-height:400px;overflow-y:auto;overscroll-behavior:contain}.venue-columns,.venue-row{display:grid;grid-template-columns:minmax(0,1fr) 112px 48px 52px;gap:16px;align-items:center;padding:0 12px}.venue-columns{height:32px;font-size:12px;color:#738296;background:#f7f9fb;position:sticky;top:0}.venue-row{height:44px;border-bottom:1px solid #edf0f4;font-size:13px;color:#64748b}.venue-row:last-child{border-bottom:0}.venue-row:hover{background:#f8fafc}.venue-row.is-selected{background:#f3f7fb}.venue-row label{display:flex;align-items:center;gap:10px;min-width:0;cursor:pointer;height:100%;margin:0}.venue-row input{margin:0;flex:none;width:14px;height:14px;accent-color:#356b9f}.venue-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#293b50;font-weight:500}.venue-field,.venue-type{font-size:12px;white-space:nowrap}.venue-row a{font-size:12px;color:#476c93;text-decoration:none;white-space:nowrap}.venue-row a:hover{text-decoration:underline}
.selector-hint,.no-matches{font-size:12px;color:#738296;line-height:1.7}.selector-hint{margin:10px 0 0}.no-matches{padding:12px}
button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid #397bb8;outline-offset:3px}
@media(max-width:600px){.selector-header{padding:12px 12px 8px}.selection-count{margin-left:8px}.selected-venues{padding:0 12px 12px}.selector-panel{padding:12px}.venue-columns,.venue-row{grid-template-columns:minmax(0,1fr) 36px 48px;gap:10px;padding:0 8px}.venue-field{display:none}.selector-toolbar{gap:8px}}
</style>
