import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/views/EvaluationView.vue'
import {createMemoryHistory,createRouter} from 'vue-router'
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





// Fake评测工作区交互检查；不调用真实模型。
const writes=[],executions=[],reads=[]
api.evaluationAction=async(action,content)=>{writes.push({action,content});return{job_id:'registered-job'}}
api.evaluationExecute=async job=>{executions.push(job);return{task_id:'eval-task',job_id:job}}
api.evaluationArtifact=async id=>{reads.push(id);return{job_id:'registered-job',semantic_review:'not_collected'}}
api.task=async()=>({id:'eval-task',type:'evaluation_replay',status:'succeeded',stage:'回放完成',result_id:'registered-result'})
const root=node('root'),router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
await router.push('/');await router.isReady()
const app=renderer.createApp({setup:()=>()=>h(Workspace)})
app.use(router);app.component('ElButton',Button);app.mount(root);await flush()
assert.equal(writes.length,0);assert.equal(executions.length,0);assert.equal(reads.length,0)
walk(root).find(n=>n.tag==='textarea').props.onInput({target:{value:'{"id":"permitted-dataset"}'}});await flush()
button(root,'确认并保存').props.onClick();await flush()
assert.deepEqual(writes[0],{action:'datasets',content:{id:'permitted-dataset'}})
assert.equal(button(root,'执行 / 继续').props.disabled,true)
walk(root).find(n=>n.tag==='input'&&n.props.type==='checkbox').props.onChange({target:{checked:true}});await flush()
assert.equal(button(root,'执行 / 继续').props.disabled,false)
button(root,'执行 / 继续').props.onClick();await flush()
assert.deepEqual(executions,['registered-job']);assert.equal(reads.length,0)
button(root,'读取已保存内容').props.onClick();await flush()
assert.deepEqual(reads,['registered-result']);assert.match(text(root),/not_collected/)
app.unmount()
console.log('PASS 默认无模型 → 显式登记 → 确认调用预算 → 任务结果 → 明确读取产物；语义仍待人工复核')
