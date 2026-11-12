import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/ResearchPlanWorkspace.vue'
import {api} from '../src/api/index.ts'
import {useTaskStore} from '../src/stores/tasks.ts'
import proposal from '../../test/fixtures/research_plan.json'

const cache=new Map(),clone=v=>JSON.parse(JSON.stringify(v))
globalThis.localStorage={getItem:k=>cache.get(k)||null,setItem:(k,v)=>cache.set(k,String(v)),removeItem:k=>cache.delete(k)}
globalThis.document={hidden:false}
const node=(tag,text='')=>({tag,text,props:{},children:[],parent:null})
const renderer=createRenderer({createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
 setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},patchProp:(n,k,p,v)=>{n.props[k]=v},parentNode:n=>n.parent,
 nextSibling:n=>{const list=n.parent?.children||[];return list[list.indexOf(n)+1]||null},
 insert(n,p,a){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1)}n.parent=p;const i=a?p.children.indexOf(a):-1;p.children.splice(i<0?p.children.length:i,0,n)},
 remove(n){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1);n.parent=null}}})
const Input=defineComponent({inheritAttrs:false,props:['modelValue','disabled','type'],emits:['update:modelValue'],setup:(p,{attrs,emit})=>()=>h(p.type==='textarea'?'textarea':'input',{...attrs,value:p.modelValue,disabled:p.disabled,onInput:e=>emit('update:modelValue',e.target.value)})})
const NumberInput=defineComponent({inheritAttrs:false,props:['modelValue','disabled'],emits:['update:modelValue'],setup:(p,{attrs,emit})=>()=>h('input',{...attrs,value:p.modelValue,disabled:p.disabled,onInput:e=>emit('update:modelValue',Number(e.target.value))})})
const Button=defineComponent({props:['disabled','loading'],setup:(p,{attrs,slots})=>()=>h('button',{...attrs,disabled:p.disabled||p.loading},slots.default?.())})
const Tag=defineComponent({setup:(p,{slots})=>()=>h('span',slots.default?.())})
const walk=n=>[n,...n.children.flatMap(walk)],text=n=>n.text+n.children.map(text).join('')
const button=(root,label)=>walk(root).find(n=>n.tag==='button'&&text(n).includes(label))
const input=(root,label)=>walk(root).find(n=>n.props['aria-label']===label)
const change=(root,label,value)=>input(root,label).props.onInput({target:{value}})
const flush=async()=>{for(let i=0;i<10;i++){await nextTick();await new Promise(r=>setImmediate(r))}}
let starts=[],saves=[],tasks=new Map(),plans=new Map(),status='succeeded',getHook=null
function reset(mode='api'){cache.clear();cache.set('rp-data-mode',mode);starts=[];saves=[];tasks=new Map();plans=new Map();status='succeeded';getHook=null}
function createRecord(id,goal){return{id,goal,plan:clone(proposal),revision:1,status:'draft',source_task_id:'task-'+id,
 created_at:'2026-11-10',updated_at:'2026-11-10',planning_date:'2026-11-10',model_name:'test-model',
 capabilities:{search_sources:['arxiv'],execution_available:false,notice:'本阶段只生成计划，尚未执行文献研究。'}}}
api.startResearchPlan=async goal=>{
 starts.push(goal);const id='plan-'+starts.length,value=createRecord(id,goal)
 if(status==='succeeded')plans.set(id,value)
 tasks.set(value.source_task_id,{id:value.source_task_id,type:'research_planning',research_goal:goal,status,
  result_id:status==='succeeded'?id:null,stage:'研究计划状态',progress:100,error:status==='failed'?{code:'MODEL_TIMEOUT',message:'规划超时'}:null})
 return{task_id:value.source_task_id}
}
api.task=async id=>clone(tasks.get(id))
api.researchPlan=async id=>getHook?getHook(id):clone(plans.get(id))
api.planReports=async()=>({items:[]})
api.planExecutions=async()=>({items:[]})
api.planSearch=async()=>({run:null,history:[]})
api.saveResearchPlan=async(id,plan,revision)=>{
 saves.push({id,plan:clone(plan),revision});const record=plans.get(id)
 if(record.revision!==revision)throw Object.assign(new Error('计划已在其他页面修改'),{status:409})
 Object.assign(record,{plan:clone(plan),revision:revision+1,edited_by_user:true});return clone(record)
}
function mount(initial){
 const root=node('root'),pinia=createPinia(),planId=ref(initial)
 const app=renderer.createApp({setup:()=>()=>h(Workspace,{initialPlanId:planId.value})})
 app.use(pinia);app.component('ElInput',Input);app.component('ElInputNumber',NumberInput);app.component('ElButton',Button);app.component('ElTag',Tag);app.component('ElCheckbox',Tag);app.component('RouterLink',Tag);app.mount(root)
 return{root,planId,close(){useTaskStore(pinia).stop();app.unmount()}}
}
const goal='整理2020–2025年机器人视觉研究，分析3篇论文，比较精度和能耗并生成证据报告。'
reset();let view=mount();await flush()
assert.equal(starts.length,0)
change(view.root,'研究目标',goal);await flush()
button(view.root,'理解目标并制定计划').props.onClick();await flush()
assert.deepEqual(starts,[goal]);assert.match(text(view.root),/尚未执行文献研究/)
assert.equal(input(view.root,'研究主题').props.value,proposal.topic)
change(view.root,'研究主题','用户修改后的主题')
change(view.root,'研究问题','第一个研究问题\n第二个研究问题')
change(view.root,'起始年份','2021');await flush()
button(view.root,'保存计划修改').props.onClick();await flush()
assert.equal(saves[0].plan.topic,'用户修改后的主题')
assert.deepEqual(saves[0].plan.research_questions,['第一个研究问题','第二个研究问题'])
assert.equal(saves[0].plan.scope.year_basis,'edited');assert.equal(saves[0].revision,1)
assert.match(text(view.root),/第 2 版/)
assert.equal(starts.length,1)
const venueSelector=input(view.root,'计划检索范围');venueSelector.props.onChange({target:{value:'cvpr'}});await flush()
assert.equal(button(view.root,'开始检索与初筛').props.disabled,true)
button(view.root,'保存计划修改').props.onClick();await flush()
assert.equal(saves.at(-1).plan.scope.venue_slug,'cvpr')
assert.deepEqual(saves.at(-1).plan.scope.requested_sources,['openalex','cvf'])
input(view.root,'计划检索 CVPR').props.onChange({target:{checked:false}});await flush()
const beforeEmpty=saves.length;button(view.root,'保存计划修改').props.onClick();await flush()
assert.equal(saves.length,beforeEmpty);assert.match(text(view.root),/请至少选择一个会议/)
input(view.root,'计划检索 AAAI').props.onChange({target:{checked:true}});await flush()
input(view.root,'计划检索 IJCAI').props.onChange({target:{checked:true}});await flush()
button(view.root,'保存计划修改').props.onClick();await flush()
assert.equal(saves.at(-1).plan.scope.venue_slug,null)
assert.deepEqual(saves.at(-1).plan.scope.venue_slugs,['aaai','ijcai'])
assert.deepEqual(saves.at(-1).plan.scope.requested_sources,['openalex','aaai_proceedings','ijcai_proceedings'])
for(const name of ['ACL','EMNLP','SIGIR','NeurIPS','Artificial Intelligence','TOIS','ECCV','ICML','ICLR']){input(view.root,'计划检索 '+name).props.onChange({target:{checked:true}});await flush()}
button(view.root,'保存计划修改').props.onClick();await flush()
assert.equal(saves.at(-1).plan.scope.venue_slugs.length,11)
assert.ok(saves.at(-1).plan.scope.requested_sources.includes('openreview'))
assert.ok(saves.at(-1).plan.scope.requested_sources.includes('crossref'))
view.close();view=mount();await flush()
assert.equal(input(view.root,'研究主题').props.value,'用户修改后的主题');assert.equal(starts.length,1)
plans.get('plan-1').revision+=1;plans.get('plan-1').plan.topic='其他页面保存的主题'
change(view.root,'研究目的','另一次编辑');await flush()
button(view.root,'保存计划修改').props.onClick();await flush()
assert.match(text(view.root),/其他页面修改/)
button(view.root,'重新读取服务器版本').props.onClick();await flush()
assert.equal(input(view.root,'研究主题').props.value,'其他页面保存的主题')
assert.ok(!text(view.root).includes('计划已在其他页面修改'))
change(view.root,'研究目标',goal+'请重点解释方法差异。');await flush()
assert.equal(button(view.root,'保存计划修改').props.disabled,true)
assert.match(text(view.root),/上方研究目标已改变/)
view.close();console.log('PASS 输入目标 → 生成计划 → 编辑范围和问题 → 版本保存 → 刷新恢复 → 并发冲突处理')

reset();plans.set('old',createRecord('old','旧目标'));plans.set('new',createRecord('new','新目标'))
let finishOld
getHook=id=>id==='old'?new Promise(resolve=>{finishOld=()=>resolve(clone(plans.get('old')))}):clone(plans.get(id))
view=mount('old');await flush();view.planId.value='new';await flush();finishOld();await flush()
assert.equal(input(view.root,'研究目标').props.value,'新目标')
assert.equal(input(view.root,'研究主题').props.value,proposal.topic)
assert.ok(text(view.root).includes('原始目标：新目标'))
view.close();console.log('PASS 切换计划时旧响应不覆盖当前目标')

reset();status='failed';view=mount();await flush();change(view.root,'研究目标',goal);await flush()
button(view.root,'理解目标并制定计划').props.onClick();await flush()
assert.match(text(view.root),/规划超时/);assert.equal(input(view.root,'研究目标').props.value,goal)
status='succeeded';button(view.root,'理解目标并制定计划').props.onClick();await flush()
assert.equal(starts.length,2);assert.ok(input(view.root,'研究主题'))
view.close();console.log('PASS 规划失败保留输入，重试可恢复')

reset('demo');view=mount();await flush();change(view.root,'研究目标',goal);await flush()
assert.equal(button(view.root,'理解目标并制定计划').props.disabled,true)
assert.equal(starts.length,0);assert.match(text(view.root),/演示模式不调用模型或伪造研究计划/)
view.close();console.log('PASS 演示模式不伪造研究计划或执行结果')
