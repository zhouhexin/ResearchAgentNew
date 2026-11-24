import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import Workspace from '../src/components/ReportChecks.vue'
import {api} from '../src/api/index.ts'
const cache=new Map([['rp-data-mode','api']])
globalThis.localStorage={getItem:k=>cache.get(k)||null}
globalThis.Document=class {};globalThis.ShadowRoot=class {}
globalThis.document=Object.assign(new Document(),{activeElement:null})
const node=(tag,text='')=>({tag,tagName:tag.toUpperCase(),text,props:{},children:[],parent:null,addEventListener(){},removeEventListener(){},getRootNode(){return globalThis.document}})
const renderer=createRenderer({createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
 setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},patchProp:(n,k,p,v)=>{n.props[k]=v},parentNode:n=>n.parent,
 nextSibling:n=>{const list=n.parent?.children||[];return list[list.indexOf(n)+1]||null},
 insert(n,p,a){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1)}n.parent=p;const i=a?p.children.indexOf(a):-1;p.children.splice(i<0?p.children.length:i,0,n)},
 remove(n){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1);n.parent=null}}})
const Button=defineComponent({props:['disabled'],setup:(p,{attrs,slots})=>()=>h('button',{...attrs,disabled:Boolean(p.disabled)},slots.default?.())})
const walk=n=>[n,...n.children.flatMap(walk)],text=n=>n.text+n.children.map(text).join('')
const button=(root,label)=>walk(root).find(n=>n.tag==='button'&&text(n).includes(label))
const flush=async()=>{for(let i=0;i<12;i++){await nextTick();await new Promise(r=>setImmediate(r))}}
const starts=[],handoffs=[]
let items=[]
api.reportChecks=async()=>({items})
api.startReportCheck=async(id,kind,resume,budget)=>{
 starts.push({id,kind,resume,budget});items=[{id:'task',report_check_request_id:'request',check_kind:kind,status:'failed',stage:'保留失败项',result_id:'result'}]
 return{task_id:'task',request_id:'request'}
}
api.reportCheck=async()=>({id:'result',check_kind:'independent_review',complete:false,source_unchanged:true,denominator:2,failed:1,
 items:[{status:'succeeded',unit:{unit_id:'comparisons:0:claim',statement:'比较结论'},result:{review:{verdict:'insufficient',reason:'需要核对条件。'},
 citations:[{ref_id:'P1:E2',page_number:null,start_char:0,end_char:4,text:'冻结原文'}]}}]})
api.handoffReport=async(id,body)=>{handoffs.push({id,body});return{artifact_id:'handoff',issues:[{}]}}
const reportId=ref('report-one'),root=node('root'),app=renderer.createApp({setup:()=>()=>h(Workspace,{reportId:reportId.value})})
app.component('ElButton',Button);app.mount(root);await flush()
assert.equal(starts.length,0)
button(root,'开始独立审查').props.onClick();await flush()
assert.deepEqual(starts[0],{id:'report-one',kind:'independent_review',resume:undefined,budget:{max_calls:100,total_seconds:1800}})
button(root,'查看结果与原文').props.onClick();await flush()
assert.match(text(root),/未完成 1 项/);assert.match(text(root),/冻结原文/);assert.match(text(root),/无页码/)
assert.equal(button(root,'保存并交给 Evolution').props.disabled,true)
const inputs=walk(root).filter(n=>n.tag==='input')
inputs.find(n=>n.props.maxlength==='100')['props']['onUpdate:modelValue']('用户')
walk(root).find(n=>n.tag==='textarea').props['onUpdate:modelValue']('保持实验条件。')
inputs.find(n=>n.props.type==='checkbox').props['onUpdate:modelValue'](true);await flush()
button(root,'保存并交给 Evolution').props.onClick();await flush()
assert.equal(handoffs.length,1);assert.deepEqual(handoffs[0].body.result_ids,['result'])
assert.equal(handoffs[0].body.permission.allowed,true)
assert.match(text(root),/实验尚未运行/)
button(root,'在原预算内继续').props.onClick();await flush()
assert.equal(starts[1].resume,'request')
items=[];reportId.value='report-two';await flush()
assert.doesNotMatch(text(root),/冻结原文/);assert.doesNotMatch(text(root),/已交接/)
app.unmount()
console.log('PASS 检查默认不执行 → 显式预算 → 历史原文/失败分母 → 独立用途授权交接 → 原预算继续 → 切换报告清理')
