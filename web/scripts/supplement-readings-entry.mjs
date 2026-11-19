import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/SupplementReadings.vue'
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



const lookups=[],requests=[],branches=[]
api.supplementReadings=async id=>{lookups.push(id);return{remaining_slots:1,items:[{candidate_id:'arxiv:one',title:'补充论文',screening:{decision:'excluded'},analyses:[{paper_id:3,analysis_id:'analysis-three',scope:'fast',selected_chunks:8,total_chunks:30}]}]}}
api.adoptSupplementReadings=async(id,body)=>{requests.push({id,body});if(requests.length===1)throw new Error('覆盖服务暂时失败');return{execution_id:'branch-new',task_id:'coverage-new',reading_selection_id:'choice-one'}}
const requestId=ref('search-one'),current=ref(true)
const root=node('root'),app=renderer.createApp({setup:()=>()=>h(Workspace,{requestId:requestId.value,current:current.value,onRechecked:id=>branches.push(id)})})
app.component('ElButton',Button);app.mount(root);await flush()
assert.equal(lookups.length,0);assert.equal(requests.length,0)
button(root,'关联已完成').props.onClick();await flush()
assert.deepEqual(lookups,['search-one']);assert.match(text(root),/初筛建议排除/)
assert.equal(button(root,'确认新增论文').props.disabled,true)
walk(root).find(n=>n.tag==='select').props.onChange({target:{value:'analysis-three'}})
walk(root).find(n=>n.tag==='textarea').props.onInput({target:{value:'补充实验对照，已人工核对摘要。'}});await flush()
assert.equal(button(root,'确认新增论文').props.disabled,false)
button(root,'确认新增论文').props.onClick();await flush()
assert.match(text(root),/覆盖服务暂时失败/)
assert.deepEqual(requests[0],{id:'search-one',body:{readings:[{candidate_id:'arxiv:one',analysis_id:'analysis-three'}],reason:'补充实验对照，已人工核对摘要。'}})
button(root,'确认新增论文').props.onClick();await flush()
assert.deepEqual(requests[1],requests[0]);assert.deepEqual(branches,['branch-new'])
requestId.value='search-other';current.value=false;await flush()
assert.doesNotMatch(text(root),/补充论文 ·/)
assert.equal(button(root,'关联已完成').props.disabled,true)
app.unmount()
console.log('PASS 显式查询有效版本 → 初筛风险提示 → 选择及理由门槛 → 失败保留选择 → 原输入重试 → 新分支联动 → 切换请求清理')
