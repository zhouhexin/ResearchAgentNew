import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/RereadSuggestion.vue'
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

const requests=[],records=[],previews=[],resumes=[],rechecks=[],branches=[]
api.rereadHistory=async()=>({items:structuredClone(records)})
api.rereadPreview=async(id,body)=>{previews.push({id,body});return{id:'preview-one',paper_id:1,analysis_id:'base-one',document_id:10,total_chunks:30,per_batch_max_chars:4000,candidates:[{evidence_id:'E10',already_read:false,page_number:3,section_hint:'Experiments',characters:24,text:'Exact original evidence.'}]}}
api.startReread=async body=>{requests.push(body);records.unshift({request_id:'request-one',task:{id:'reread-one',paper_id:1,reread_question_id:'Q1',reread_suggestion_index:0,status:'failed',stage:'补读中断',error:{code:'MODEL_TIMEOUT',message:'timeout',retryable:true}},budget:{calls_used:1,max_calls:8,deadline_epoch:2000000000}});return{task_id:'reread-one',request_id:'request-one'}}
api.resumeReread=async id=>{resumes.push(id);records[0].task={...records[0].task,status:'succeeded',stage:'补读完成',result_id:'new-analysis',error:null};return{task_id:'reread-one',request_id:id}}
api.recheckReread=async id=>{rechecks.push(id);return{task_id:'coverage-new',execution_id:'branch-one',plan_id:'plan'}}
const result=ref({id:'coverage-one',is_current:true}),question={question_id:'Q1',question:'方法在什么条件下适用？',suggestions:[{action:'read_existing',paper_ids:[1],target:'实验条件（待定位）',rationale:'补读正文'}]}
const root=node('root'),app=renderer.createApp({setup:()=>()=>h(Workspace,{result:result.value,question,suggestionIndex:0,onRechecked:id=>branches.push(id)})})
app.component('ElButton',Button);app.component('RouterLink',{props:['to'],setup:(p,{slots})=>()=>h('a',{href:p.to},slots.default?.())});app.mount(root);await flush()
assert.equal(previews.length,0);assert.equal(requests.length,0)
button(root,'定位原文片段').props.onClick();await flush()
assert.equal(previews.length,1);assert.equal(requests.length,0)
assert.match(text(root),/Exact original evidence/);assert.match(text(root),/未读/)
assert.equal(button(root,'确认范围与预算').props.disabled,true)
walk(root).find(n=>n.tag==='input'&&n.props.type==='checkbox').props.onChange({target:{checked:true}});await flush()
assert.equal(button(root,'确认范围与预算').props.disabled,false)
button(root,'确认范围与预算').props.onClick();await flush()
assert.deepEqual(requests[0],{preview_id:'preview-one',evidence_ids:['E10'],max_calls:8,total_seconds:600})
assert.match(text(root),/调用 1 \/ 8/)
button(root,'继续原补读').props.onClick();await flush()
assert.deepEqual(resumes,['request-one'])
assert(walk(root).some(n=>n.tag==='a'&&n.props.href?.includes('new-analysis')))
assert.equal(rechecks.length,0)
button(root,'用新分析重新检查覆盖').props.onClick();await flush()
assert.deepEqual(rechecks,['reread-one']);assert.deepEqual(branches,['branch-one'])
result.value={id:'other-coverage',is_current:false};records.length=0;await flush()
assert.equal(button(root,'定位原文片段').props.disabled,true)
assert.doesNotMatch(text(root),/Exact original evidence/)
app.unmount()
console.log('PASS 预览无模型 → 选择真实片段 → 明确确认预算 → 失败按原请求续跑 → 新分析链接 → 明确重查覆盖 → 切换报告清理预览')
