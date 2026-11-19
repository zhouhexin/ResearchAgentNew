import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/PlanExecutionWorkspace.vue'
import {api} from '../src/api/index.ts'
import {useTaskStore} from '../src/stores/tasks.ts'
import proposal from '../../test/fixtures/research_plan.json'
const cache=new Map([['rp-data-mode','api']])
globalThis.localStorage={getItem:k=>cache.get(k)||null,setItem:(k,v)=>cache.set(k,String(v))}
globalThis.document={hidden:false}
const node=(tag,text='')=>({tag,text,props:{},children:[],parent:null})
const renderer=createRenderer({createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
 setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},patchProp:(n,k,p,v)=>{n.props[k]=v},parentNode:n=>n.parent,
 nextSibling:n=>{const list=n.parent?.children||[];return list[list.indexOf(n)+1]||null},
 insert(n,p,a){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1)}n.parent=p;const i=a?p.children.indexOf(a):-1;p.children.splice(i<0?p.children.length:i,0,n)},
 remove(n){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1);n.parent=null}}})

const Button=defineComponent({props:['disabled','loading'],setup:(p,{attrs,slots})=>()=>h('button',{...attrs,disabled:p.disabled||p.loading},slots.default?.())})
const Link=defineComponent({props:['to'],setup:(p,{slots})=>()=>h('a',{href:p.to},slots.default?.())})
const walk=n=>[n,...n.children.flatMap(walk)],text=n=>n.text+n.children.map(text).join('')
const button=(root,label)=>walk(root).find(n=>n.tag==='button'&&text(n).includes(label))
const flush=async()=>{for(let i=0;i<12;i++){await nextTick();await new Promise(r=>setImmediate(r))}}
const root=node('root'),pinia=createPinia(),calls=[]
let runs=[]
const run={id:'execution',plan_id:'plan',revision:1,plan:proposal,analysis_scope:'full',review_after_analysis:true,task_id:'parent',status:'failed',created_at:'2026-11-18',papers:[{
 candidate_id:'one',paper_id:1,title:'论文一',screening:{decision:'included'},fulltext:{status:'succeeded',document_id:10},analysis:{status:'succeeded',analysis_id:'fixed-analysis'},
 review:{status:'failed',task_id:'review-task',review_id:'fixed-review',error:{code:'MODEL_TIMEOUT',message:'评审超时'}}}]}
api.planExecutions=async()=>({items:structuredClone(runs)})
api.startPlanExecution=async(id,body)=>{calls.push(body);runs=[run];return{task_id:'parent',execution_id:'execution',reused:false}}
api.resumePlanExecution=async()=>{calls.push('resume');runs=[{...run,status:'succeeded',papers:[{...run.papers[0],review:{status:'succeeded',review_id:'fixed-review',completed_claims:2,total_claims:2}}]}];return{task_id:'resumed',execution_id:'execution'}}
useTaskStore(pinia).track=async()=>{}
const app=renderer.createApp({setup:()=>()=>h(Workspace,{record:{id:'plan',revision:1},searchRun:{id:'search',revision:1,status:'succeeded',plan:proposal},selectedIds:['one','two']})})
app.use(pinia);app.component('ElButton',Button);app.component('RouterLink',Link);app.mount(root)
await flush()
const checkbox=walk(root).find(n=>n.props['aria-label']==='分析后自动辅助评审')
assert(checkbox)
checkbox.props.onChange({target:{checked:true}})
await nextTick()
assert.match(text(root),/增加等待时间和用量/)
button(root,'确认所选论文并执行').props.onClick();await flush()
assert.equal(calls[0].review_after_analysis,true)
assert.match(text(root),/MODEL_TIMEOUT/)
assert(walk(root).some(n=>n.tag==='a'&&n.props.href?.includes('analysisId=fixed-analysis')))
button(root,'继续本轮未完成部分').props.onClick();await flush()
assert.equal(calls[1],'resume')
assert.match(text(root),/已核验 2 \/ 2 条/)
assert(walk(root).some(n=>n.tag==='a'&&n.props.href?.includes('claimReviewId=fixed-review')))
app.unmount()
console.log('PASS 显式启用自动评审 → 分析成功/评审失败分开显示 → 继续执行 → 固定版本评审链接')
