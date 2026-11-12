<script setup lang="ts">
import {ref,watch,onBeforeUnmount} from 'vue'
import {api,mode} from '../api'
import type {PublicationInfo} from '../types/research'
import PublicationStatus from './PublicationStatus.vue'
const props=defineProps<{paperId:number}>(),items=ref<(PublicationInfo&{year:number;venue:{slug:string;name:string}})[]>([]),error=ref('')
let version=0
async function load(){
 const current=++version;items.value=[];error.value=''
 if(mode()==='demo')return
 try{const result=await api.paperPublication(props.paperId);if(current===version)items.value=result.items}
 catch(e){if(current===version)error.value=(e as Error).message}
}
watch(()=>props.paperId,load,{immediate:true})
onBeforeUnmount(()=>{version++})
</script>
<template><section v-if="items.length||error" class="panel publication-panel"><h3>发表记录</h3><p v-if="error" class="error-text">{{error}} <el-button text @click="load">重试</el-button></p><article v-for="(item,index) in items" :key="index"><p>{{item.venue.name}} · {{item.year}}</p><PublicationStatus :publication="item"/></article></section></template>
<style scoped>.publication-panel{padding:20px;margin:18px 0}</style>
