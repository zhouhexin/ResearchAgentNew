import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/LibraryExecutionWorkspace.vue'
import {api} from '../src/api/index.ts'
import {useTaskStore} from '../src/stores/tasks.ts'
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
const field=(root,label)=>walk(root).find(n=>n.props['aria-label']===label)
const flush=async()=>{for(let i=0;i<12;i++){await nextTick();await new Promise(r=>setImmediate(r))}}
const papers=[1,2].map(id=>({id,title:'库内论文'+id,has_fulltext:true,has_analysis:true}))
const queries=[],starts=[]
api.papers=async p=>{queries.push(p);return{items:papers,total:2}}
api.analyses=async id=>[{id:'analysis-'+id,is_current:true,document:{selected_chunks:4,total_chunks:4}}]
api.planExecutions=async()=>({items:[]})
api.startLibraryExecution=async(id,body)=>{starts.push({id,body});return{task_id:'task',execution_id:'library-execution'}}
const record=ref({id:'plan-library',revision:1}),root=node('root'),pinia=createPinia()
useTaskStore(pinia).track=async()=>{}
const app=renderer.createApp({setup:()=>()=>h(Workspace,{record:record.value})})
app.use(pinia);app.component('ElButton',Button);app.mount(root);await flush()
assert.equal(queries[0].has_fulltext,true)
assert.equal(button(root,'确认库内选文').props.disabled,true)
field(root,'选择库内论文 P1').props.onChange({target:{checked:true}})
field(root,'选择库内论文 P2').props.onChange({target:{checked:true}})
await flush()
assert.equal(button(root,'确认库内选文').props.disabled,false)
const versions=field(root,'P1 分析版本')
assert(walk(versions).some(n=>n.props.value==='analysis-1'),JSON.stringify(walk(versions).map(n=>({tag:n.tag,props:n.props,text:n.text}))))
versions.props.onChange({target:{value:'analysis-1'}})
await nextTick()
button(root,'确认库内选文').props.onClick();await flush()
assert.deepEqual(starts[0],{id:'plan-library',body:{expected_revision:1,papers:[{paper_id:1,analysis_id:'analysis-1'},{paper_id:2}],analysis_scope:'full',review_after_analysis:false}})
record.value={id:'another-plan',revision:2};await flush()
assert.equal(button(root,'确认库内选文').props.disabled,true)
assert.equal(field(root,'P1 分析版本'),undefined)
let rejectOld
api.analyses=()=>new Promise((resolve,reject)=>{rejectOld=reject})
field(root,'选择库内论文 P1').props.onChange({target:{checked:true}})
await nextTick()
field(root,'选择库内论文 P1').props.onChange({target:{checked:false}})
api.analyses=async()=>[{id:'fresh-analysis',is_current:true,document:{selected_chunks:4,total_chunks:4}}]
field(root,'选择库内论文 P1').props.onChange({target:{checked:true}})
await flush()
rejectOld(new Error('迟到的旧请求失败'))
await flush()
assert(walk(field(root,'P1 分析版本')).some(n=>n.props.value==='fresh-analysis'))
app.unmount()
console.log('PASS 文献库查询 → 至少两篇门槛 → 明确分析版本 → 无检索记录直接执行 → 切换计划清除旧选文')
