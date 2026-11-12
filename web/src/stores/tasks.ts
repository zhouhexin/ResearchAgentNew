import {defineStore} from 'pinia'
import {ref,computed} from 'vue'
import {api,mode} from '../api'
import {readStorage} from '../api/utils'
import type {TaskInfo} from '../types/research'
export const useTaskStore=defineStore('tasks',()=>{const tasks=ref<Record<string,TaskInfo>>({}),error=ref('');const key=()=>`rp-task-ids-${mode()}`;let timer:ReturnType<typeof setTimeout>|undefined;let failures=0,stopped=false;
const active=computed(()=>Object.values(tasks.value).filter(t=>['queued','running'].includes(t.status)));
function persist(){localStorage.setItem(key(),JSON.stringify(active.value.map(t=>t.id)))}
async function refresh(){if(stopped)return;try{const ids=[...new Set([...active.value.map(t=>t.id),...readStorage<string[]>(key(),[])])];for(const id of ids)tasks.value[id]=await api.task(id);error.value='';failures=0;persist()}catch(e){error.value=(e as Error).message;failures++}if(!stopped&&failures<5&&(active.value.length||readStorage<string[]>(key(),[]).length))timer=setTimeout(refresh,document.hidden?10000:Math.min(2500*2**failures,20000))}
async function track(id:string){const ids=readStorage<string[]>(key(),[]);if(!ids.includes(id))localStorage.setItem(key(),JSON.stringify([...ids,id]));clearTimeout(timer);stopped=false;await refresh()}
function resume(){stopped=false;failures=0;clearTimeout(timer);void refresh()}
function stop(){stopped=true;clearTimeout(timer)}
return{tasks,error,active,track,resume,stop}})
