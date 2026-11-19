import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/SupplementSearch.vue'
import {api} from '../src/api/index.ts'
const cache=new Map([['rp-data-mode','api']])
globalThis.localStorage={getItem:k=>cache.get(k)||null,setItem:(k,v)=>cache.set(k,String(v))}
globalThis.document={hidden:false}
const node=(tag,text='')=>({tag,text,props:{},children:[],parent:null})
const renderer=createRenderer({createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
 setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},patchProp:(n,k,p,v)=>{n.props[k]=v},parentNode:n=>n.parent,
 nextSibling:n=>{const list=n.parent?.children||[];return list[list.indexOf(n)+1]||null},
 insert(n,p,a){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1)}n.parent=p;const i=a?p.children.indexOf(a):-1;p.children.splice(i<0?p.children.length:i,0,n)},
 remove(n){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1);n.parent=null}}})



const Button=defineComponent({props:['disabled','loading'],setup:(p,{attrs,slots})=>()=>h('button',{...attrs,disabled:Boolean(p.disabled||p.loading)},slots.default?.())})
const walk=n=>[n,...n.children.flatMap(walk)],text=n=>n.text+n.children.map(text).join('')
const button=(root,label)=>walk(root).find(n=>n.tag==='button'&&text(n).includes(label))
const flush=async()=>{for(let i=0;i<12;i++){await nextTick();await new Promise(r=>setImmediate(r))}}


const requests=[],records=[],resumes=[],saves=[]
api.supplementSearchHistory=async()=>({items:structuredClone(records)})
api.supplementSearch=async()=>({run:{notice:'arXiv 预印本，非全文结论',candidates:[{candidate_id:'arxiv:2601.00001',title:'Energy comparison',abstract:'Exact abstract.',source:'arxiv',doi:'10.48550/arxiv.2601.00001'}],screenings:{}}})
api.startSupplementSearch=async(id,body)=>{requests.push({id,body});records.unshift({request_id:'request-one',task:{id:'task-one',supplement_question_id:'Q1',supplement_suggestion_index:0,status:'failed',stage:'初筛中断',error:{retryable:true,message:'模型暂时失败'}},budget:{calls_used:2,max_calls:8}});return{task_id:'task-one',request_id:'request-one'}}
api.resumeSupplementSearch=async id=>{resumes.push(id);records[0].task.status='succeeded';return{task_id:'task-two',request_id:id}}
api.savePapers=async papers=>{saves.push(papers);return{created:1,existing:0,rejected:0}}
const result=ref({id:'coverage-one',is_current:true}),question={question_id:'Q1',question:'缺少哪些对照？',suggestions:[{action:'search_later',target:'能耗对照',paper_ids:[]}]}
const root=node('root'),app=renderer.createApp({setup:()=>()=>h(Workspace,{result:result.value,question,suggestionIndex:0})})
app.component('ElButton',Button);app.mount(root);await flush()
assert.equal(requests.length,0);assert.equal(button(root,'确认范围与预算').props.disabled,true)
walk(root).find(n=>n.tag==='textarea').props.onInput({target:{value:'robot energy'}});await flush()
button(root,'确认范围与预算').props.onClick();await flush()
assert.deepEqual(requests[0],{id:'coverage-one',body:{question_id:'Q1',suggestion_index:0,queries:['robot energy'],per_query:5,max_calls:8,total_seconds:300,source:'arxiv'}})
assert.match(text(root),/Exact abstract/);assert.match(text(root),/调用 2 \/ 8/)
assert.equal(saves.length,0)
button(root,'继续原补检索').props.onClick();await flush();assert.deepEqual(resumes,['request-one'])
button(root,'收藏此候选').props.onClick();await flush();assert.equal(saves.length,1)
assert.match(text(root),/不自动下载/)
result.value={id:'other',is_current:false};records.length=0;await flush()
assert.doesNotMatch(text(root),/Exact abstract/)
assert.equal(button(root,'确认范围与预算').props.disabled,true)
app.unmount()
console.log('PASS 打开不检索 → 确认查询和预算 → 部分结果/失败显示 → 原请求续跑 → 显式收藏 → 历史报告禁用')
