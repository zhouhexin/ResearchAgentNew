import {defineStore} from 'pinia'
import {ref} from 'vue'
import {api} from '../api'
import {parseCitation} from '../api/utils'
import type {EvidenceRecord,ReviewCitation} from '../types/research'
export const useEvidenceStore=defineStore('evidence',()=>{const visible=ref(false),loading=ref(false),error=ref(''),evidence=ref<EvidenceRecord|null>(null),citation=ref<ReviewCitation|null>(null),reference=ref('');let generation=0;
async function open(refId:string,detail?:ReviewCitation|null){const current=++generation;reference.value=refId;citation.value=detail?.ref_id===refId?detail:null;visible.value=true;loading.value=true;error.value='';evidence.value=null;const parsed=parseCitation(refId);if(!parsed){error.value='无效引用编号';loading.value=false;return}try{const data=await api.evidence(parsed.evidenceId);if(current!==generation)return;if(parsed.paperId!==undefined&&data.paper_id!==parsed.paperId)throw Error('引用所属论文不一致');evidence.value=data}catch(e){if(current===generation)error.value=(e as Error).message}finally{if(current===generation)loading.value=false}}
return{visible,loading,error,evidence,citation,reference,open}})
