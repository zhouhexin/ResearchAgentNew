import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/ExecutionReviewLinks.vue'
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




const lookups=[],starts=[],links=[],branches=[]
const review={id:'review-one',analysis_id:'analysis-one',is_current:true,model_name:'fixed-A',total:2,completed:1,quote_protocol:'references-v2',task:{id:'task-one',status:'failed',stage:'一条未完成',error:{retryable:true,message:'timeout'}}}
api.executionReviewVersions=async(plan,execution)=>{lookups.push([plan,execution]);return{model_name:'fixed-A',read_timeout:30,sdk_retries:0,items:[{paper_id:3,title:'补充论文',analysis_id:'analysis-one',claim_count:2,reviews:starts.length?[structuredClone(review)]:[]}]}}
api.startExecutionReview=async(plan,execution,body)=>{starts.push({plan,execution,body});if(body.resume_review_id){review.completed=2;review.task.status='succeeded';review.task.error=null}return{task_id:'task-one',review_id:'review-one'}}
api.linkExecutionReviews=async(plan,execution,body)=>{links.push({plan,execution,body});return{execution_id:'review-branch',task_id:'coverage-one',review_selection_id:'selection-one'}}
const execution=ref({id:'execution-one',plan_id:'plan-one',plan:{research_questions:['原计划问题']}})
const root=node('root'),app=renderer.createApp({setup:()=>()=>h(Workspace,{execution:execution.value,onRechecked:id=>branches.push(id)})})
app.component('ElButton',Button);app.component('RouterLink',{props:['to'],setup:(p,{slots})=>()=>h('a',{href:p.to},slots.default?.())});app.mount(root);await flush()
assert.equal(lookups.length,0);assert.equal(starts.length,0)
button(root,'查看或刷新').props.onClick();await flush()
assert.match(text(root),/fixed-A/);assert.match(text(root),/2 条待核对/)
assert.equal(button(root,'确认评审版本').props.disabled,true)
button(root,'确认评审此分析').props.onClick();await flush()
assert.deepEqual(starts[0],{plan:'plan-one',execution:'execution-one',body:{analysis_id:'analysis-one'}})
assert.equal(walk(root).filter(n=>n.tag==='option').length,1)
button(root,'继续此评审').props.onClick();await flush()
assert.deepEqual(starts[1].body,{analysis_id:'analysis-one',resume_review_id:'review-one'})
walk(root).find(n=>n.tag==='select').props.onChange({target:{value:'review-one'}})
walk(root).find(n=>n.tag==='textarea').props.onInput({target:{value:'确认后的覆盖问题'}});await flush()
button(root,'确认评审版本').props.onClick();await flush()
assert.deepEqual(links[0].body,{review_ids:['review-one'],research_questions:['确认后的覆盖问题']})
assert.deepEqual(branches,['review-branch'])
execution.value={id:'other',plan_id:'plan-one',plan:{research_questions:['新问题']}};await flush()
assert.doesNotMatch(text(root),/补充论文/)
app.unmount()
console.log('PASS 只读查询 → 逐篇确认 → 失败按原评审续跑 → 完整版本才可选择 → 问题确认 → 新分支联动 → 切换执行清理')
