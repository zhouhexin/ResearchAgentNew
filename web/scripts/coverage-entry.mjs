import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/QuestionCoverageWorkspace.vue'
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
const records=[],requests=[]
api.coverageTasks=async()=>({items:structuredClone(records)})
api.startCoverage=async(plan,execution,body)=>{requests.push(body);records.unshift({request_id:'request-one',task:{id:'task-'+requests.length,status:requests.length===1?'failed':'succeeded',stage:requests.length===1?'覆盖检查失败':'覆盖检查完成',result_id:requests.length===1?null:'result-one',created_at:'now'}});return{task_id:records[0].task.id,request_id:'request-one'}}
api.coverageResult=async()=>({id:'result-one',is_current:true,reading_coverage:[{paper_id:1,title:'论文',reading_scope:'fast',selected_chunks:3,total_chunks:30}],report:{suggestions_executed:false,limitations:['快读仅覆盖部分原文'],questions:[{question_id:'Q1',question:'研究问题',status:'partial',answer:'当前回答只覆盖一部分。',reason:'还缺少实验条件。',gaps:['实验条件不完整。'],citations:[{ref_id:'P1:E1',paper_id:1,paper_title:'论文',page_number:1,source_spans:[{span_id:'P1:E1:S1',text:'Exact original quote.'}]}],suggestions:[{action:'read_existing',paper_ids:[1],target:'实验部分（待定位）',rationale:'补齐条件。'}]}]}})
const root=node('root'),pinia=createPinia(),execution=ref({id:'execution',plan_id:'plan',plan:{research_questions:['初始问题']}})
const app=renderer.createApp({setup:()=>()=>h(Workspace,{execution:execution.value})})
app.use(pinia);app.component('ElButton',Button);app.mount(root);await flush()
assert.equal(requests.length,0)
assert.match(text(root),/不会自动执行/)
walk(root).find(n=>n.props['aria-label']==='覆盖检查研究问题').props.onInput({target:{value:'用户确认的问题'}})
await nextTick()
button(root,'生成覆盖报告').props.onClick();await flush()
assert.deepEqual(requests[0],{research_questions:['用户确认的问题']})
button(root,'按原输入重试').props.onClick();await flush()
assert.deepEqual(requests[1],{resume_request_id:'request-one'})
assert.match(text(root),/部分回答/)
assert.match(text(root),/Exact original quote/)
assert.match(text(root),/待人工确认/)
assert(walk(root).some(n=>n.tag==='a'&&n.props.href?.includes('coverage/result-one/files/md')))
execution.value={id:'other-execution',plan_id:'plan',plan:{research_questions:['另一个问题']}}
records.length=0;await flush()
assert.doesNotMatch(text(root),/Exact original quote/)
app.unmount()
console.log('PASS 显式发起 → 问题编辑 → 失败按原输入重试 → 部分回答/原文/建议展示 → 导出链接 → 切换执行清除旧报告')
