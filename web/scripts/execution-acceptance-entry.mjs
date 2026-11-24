import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/ExecutionAcceptance.vue'
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




const inspections=[],saves=[]
api.coverageTasks=async()=>({items:[]});api.planReports=async()=>({items:[]});api.acceptanceHistory=async()=>({items:[]})
const inspected={fingerprint:'a'.repeat(64),execution_status:'succeeded',checks:[{key:'coverage',status:'not_selected',detail:'未选择产物'}],artifacts:[],missing_review_analysis_ids:['analysis-one'],limits:[]}
api.inspectAcceptance=async(plan,execution,body)=>{inspections.push(body);return structuredClone(inspected)}
api.saveAcceptance=async(plan,execution,body)=>{saves.push(body);return{id:'acceptance-one',created_at:'now',reviewer:body.reviewer}}
api.acceptance=async()=>({id:'acceptance-one',reviewer:'用户',created_at:'now',inputs_unchanged:false,inspection:inspected,manual:saves[0].manual})
const execution=ref({id:'execution-one',plan_id:'plan-one'})
const root=node('root'),app=renderer.createApp({setup:()=>()=>h(Workspace,{execution:execution.value})})
app.component('ElButton',Button);app.mount(root);await flush()
assert.equal(inspections.length,0);assert.equal(saves.length,0)
button(root,'检查当前材料').props.onClick();await flush()
assert.deepEqual(inspections,[{}]);assert.match(text(root),/未选择、未验证/)
const manualSelects=walk(root).filter(n=>n.tag==='select').slice(2)
assert(manualSelects.every(n=>n.props.value==='not_checked'))
assert.equal(button(root,'保存本次验收').props.disabled,true)
walk(root).find(n=>n.tag==='input').props.onInput({target:{value:'用户'}});await flush()
manualSelects[0].props.onChange({target:{value:'passed'}});await flush()
assert.equal(button(root,'保存本次验收').props.disabled,true)
walk(root).find(n=>n.tag==='textarea').props.onInput({target:{value:'已对照第一页原文。'}});await flush()
assert.equal(button(root,'保存本次验收').props.disabled,false)
button(root,'保存本次验收').props.onClick();await flush()
assert.equal(saves.length,1);assert.equal(saves[0].expected_fingerprint,'a'.repeat(64))
assert.equal(saves[0].manual.filter(v=>v.verdict==='not_checked').length,3)
assert.match(text(root),/旧声明须重新核对/)
assert(walk(root).some(n=>n.tag==='a'&&n.props.href?.includes('acceptance-one/files/md')))
execution.value={id:'other',plan_id:'plan-one'};await flush()
assert.doesNotMatch(text(root),/已保存 acceptance-one/)
app.unmount()
console.log('PASS 显式无模型检查 → 未选未验证 → 人工默认未检查 → 通过需理由 → 保存历史 → 变化提示/导出 → 切换执行清理')
