import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Workspace from '../src/components/ResearchSearchWorkspace.vue'
import CVPRWorkspace from '../src/components/VenueSearchWorkspace.vue'
import AbstractTranslation from '../src/components/AbstractTranslation.vue'
import {api} from '../src/api/index.ts'
import {venueNames} from '../src/types/planning.ts'
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
const NumberInput=defineComponent({inheritAttrs:false,props:['modelValue','disabled'],emits:['update:modelValue'],setup:(p,{attrs,emit})=>()=>h('input',{...attrs,value:p.modelValue,disabled:p.disabled,onInput:e=>emit('update:modelValue',Number(e.target.value))})})
const Input=defineComponent({inheritAttrs:false,props:['modelValue','disabled'],emits:['update:modelValue'],setup:(p,{attrs,emit})=>()=>h('input',{...attrs,value:p.modelValue,disabled:p.disabled,onInput:e=>emit('update:modelValue',e.target.value)})})
const Checkbox=defineComponent({props:['modelValue','disabled'],emits:['update:modelValue'],setup:(p,{emit,slots})=>()=>h('label',[h('input',{type:'checkbox',checked:p.modelValue,disabled:p.disabled,onChange:e=>emit('update:modelValue',e.target.checked)}),slots.default?.()])})
const Button=defineComponent({props:['disabled','loading'],setup:(p,{attrs,slots})=>()=>h('button',{...attrs,disabled:p.disabled||p.loading},slots.default?.())})
const Tag=defineComponent({setup:(p,{slots})=>()=>h('span',slots.default?.())})
const walk=n=>[n,...n.children.flatMap(walk)],text=n=>n.text+n.children.map(text).join('')
const button=(root,label)=>walk(root).find(n=>n.tag==='button'&&text(n).includes(label))
const input=(root,label)=>walk(root).find(n=>n.props['aria-label']===label)
const flush=async()=>{for(let i=0;i<12;i++){await nextTick();await new Promise(r=>setImmediate(r))}}
const record={id:'plan-search',revision:1,goal:'机器人视觉研究',plan:clone(proposal)}
const makeRun=()=>({id:'search-1',plan_id:record.id,revision:1,plan:clone(proposal),goal:record.goal,per_query:10,
 task_id:'search-task',status:'succeeded',error:null,model_name:'test-model',created_at:'2026-11-10',notice:'摘要初筛，不代表全文核验。',
 queries:[{number:1,query:'robot vision',status:'succeeded',total:100,items:[]}],duplicate_count:1,skipped_count:0,
 candidates:[1,2,3].map(i=>({candidate_id:'C'+i,title:'Robot paper '+i,year:2024,authors:['Author'],arxiv_id:'2401.0000'+i,query_numbers:[1],url:'https://arxiv.org/abs/2401.0000'+i,abstract:'Robot vision abstract '+i})),
 screenings:{C1:{decision:'included',checks:[{criterion_id:'T',verdict:'meets',quote:'Robot vision',explanation:'与主题相关'}]},C2:{decision:'uncertain',checks:[]},C3:{decision:'excluded',checks:[]}}})
let run=null,history=[],starts=[],saved=[],requests=[],startHook=null,getHook=null,tasks=new Map()
function reset(){cache.clear();cache.set('rp-data-mode','api');run=null;history=[];starts=[];saved=[];requests=[];startHook=null;getHook=null;tasks=new Map()}
function publish(value){run=value;history=[{id:value.id,revision:value.revision,status:value.status,per_query:value.per_query,model_name:value.model_name,created_at:value.created_at}];tasks.set(value.task_id,{id:value.task_id,type:'research_search',status:value.status,search_run_id:value.id,stage:'检索任务',error:value.error,updated_at:'1'})}
api.planReports=async()=>({items:[]})
api.planExecutions=async()=>({items:[]})
api.planSearch=async(id,run_id)=>{requests.push([id,run_id]);return getHook?getHook(id,run_id):{run:clone(run),history:clone(history)}}
api.startPlanSearch=async(id,revision,per_query,refresh=false)=>{starts.push({id,revision,per_query,...(refresh?{refresh:true}:{})});if(startHook)return startHook();publish(makeRun());return{task_id:run.task_id,run_id:run.id,reused:false}}
api.task=async id=>clone(tasks.get(id))
api.lookupAbstractTranslation=async()=>null
api.savePapers=async papers=>{saved.push(clone(papers));return{created:papers.length,existing:0,rejected:0}}
function mount(initialRecord=record,initialRunId,component=Workspace){
 const root=node('root'),pinia=createPinia(),recordRef=ref(clone(initialRecord)),disabled=ref(false),runId=ref(initialRunId)
 const app=renderer.createApp({setup:()=>()=>h(component,{record:recordRef.value,disabled:disabled.value,initialRunId:runId.value})})
 app.use(pinia);app.component('ElInput',Input);app.component('ElCheckbox',Checkbox);app.component('ElInputNumber',NumberInput);app.component('ElButton',Button);app.component('ElTag',Tag);app.component('RouterLink',Tag);app.mount(root)
 return{root,record:recordRef,disabled,runId,store:useTaskStore(pinia),close(){useTaskStore(pinia).stop();app.unmount()}}
}

reset();let view=mount();await flush()
assert.equal(starts.length,0)
view.disabled.value=true;await flush();assert.equal(button(view.root,'开始检索与初筛').props.disabled,true)
view.disabled.value=false;await flush()
input(view.root,'每个检索词结果数').props.onInput({target:{value:'5'}});await flush()
button(view.root,'开始检索与初筛').props.onClick();await flush()
assert.deepEqual(starts,[{id:'plan-search',revision:1,per_query:5}])
assert.match(text(view.root),/去重后 3 篇/);assert.match(text(view.root),/建议纳入/);assert.match(text(view.root),/待确认/)
assert.match(text(view.root),/与主题相关/);assert.match(text(view.root),/Robot vision/)
const select=input(view.root,'筛选状态');select.props.onChange({target:{value:'uncertain'}});await flush()
assert.match(text(view.root),/Robot paper 2/);assert.ok(!text(view.root).includes('Robot paper 1'))
input(view.root,'选择论文 Robot paper 2').props.onChange({target:{checked:true}});await flush()
button(view.root,'收藏所选论文').props.onClick();await flush()
assert.equal(saved[0][0].candidate_id,'C2');assert.match(text(view.root),/已新增 1 篇/)
view.close();view=mount();await flush()
assert.match(text(view.root),/Robot paper 1/);assert.equal(starts.length,1)
view.close();console.log('PASS 保存计划门槛 → 真实请求参数 → 初筛分类及依据 → 收藏 → 刷新恢复')

reset();const failed=makeRun();failed.status='failed';failed.error={code:'MODEL_TIMEOUT',message:'模型超时，检查点已保留'};failed.screenings={C1:failed.screenings.C1};publish(failed)
view=mount();await flush();assert.match(text(view.root),/模型超时/)
assert.ok(button(view.root,'继续未完成检索与初筛'))
button(view.root,'继续未完成检索与初筛').props.onClick();await flush()
assert.equal(starts.length,1);assert.match(text(view.root),/初筛完成 3 篇/)
view.close();console.log('PASS 失败任务可查看部分结果并继续未完成初筛')

reset();const running=makeRun();running.status='running';running.screenings={};publish(running)
view=mount();await flush();assert.equal(button(view.root,'开始检索与初筛').props.disabled,true)
run.status='succeeded';run.screenings=makeRun().screenings
view.store.tasks[run.task_id]={...view.store.tasks[run.task_id],status:'succeeded',updated_at:'2'};await flush()
assert.match(text(view.root),/初筛完成 3 篇/)
assert.equal(button(view.root,'开始检索与初筛').props.disabled,false)
view.close();console.log('PASS 活动任务禁止重复提交，轮询完成后刷新初筛结果')

reset();const old=makeRun();old.revision=1;publish(old)
view=mount({...record,revision:2},old.id);await flush()
assert.match(text(view.root),/历史结果/);button(view.root,'开始检索与初筛').props.onClick();await flush()
assert.equal(starts[0].revision,2)
view.close();console.log('PASS 历史结果显示原计划版本，新执行使用当前保存版本')

reset();let finishOld
getHook=id=>id==='old'?new Promise(resolve=>{finishOld=()=>resolve({run:makeRun(),history:[]})}):{run:null,history:[]}
view=mount({...record,id:'old'});await flush();view.record.value={...record,id:'new'};await flush();finishOld();await flush()
assert.ok(!text(view.root).includes('Robot paper 1'))
view.close();console.log('PASS 切换计划后旧检索响应不覆盖新结果')

reset();cache.set('rp-data-mode','demo');view=mount();await flush()
assert.equal(button(view.root,'开始检索与初筛').props.disabled,true);assert.equal(requests.length,0)
view.close();console.log('PASS 演示模式不调用检索接口或伪造初筛结果')

reset();const cvprRequests=[],cvprPapers=['main','workshop','unknown'].map((track,i)=>({candidate_id:'cvpr-'+i,source:'openalex',venue_slug:'cvpr',openalex_id:'https://openalex.org/W'+i,
 title:'CVPR depth paper '+i,year:2024,authors:['Author'],url:'https://openaccess.thecvf.com/',abstract:'Depth abstract.',identity_verified:true,
 publication:{track,verification_status:track==='unknown'?'indexed_unverified':'official_verified',verification_url:track==='unknown'?null:'https://openaccess.thecvf.com/',reason:'根据官方记录核对',verified_at:null}}))
api.venues=async()=>({items:Object.entries(venueNames).map(([slug,name])=>({slug,acronym:name,name,venue_type:['tois','artificial-intelligence'].includes(slug)?'journal':'conference',field:({cvpr:'computer_vision',iccv:'computer_vision',eccv:'computer_vision',acl:'natural_language_processing',emnlp:'natural_language_processing',icml:'machine_learning',iclr:'machine_learning',neurips:'machine_learning',aaai:'artificial_intelligence',ijcai:'artificial_intelligence','artificial-intelligence':'artificial_intelligence',sigir:'information_retrieval',tois:'information_retrieval'})[slug],official_url:'https://example.org/'}))})
api.venueSearch=async params=>{cvprRequests.push(clone(params));return{items:clone(params.verified_main_only?cvprPapers.slice(0,1):cvprPapers),total:42,page:params.page,page_size:params.page_size,notice:'本页候选，逐篇核验。'}}
view=mount(record,undefined,CVPRWorkspace);await flush()
assert.equal(input(view.root,'检索 CVPR'),undefined,'默认收起会议列表')
assert.match(text(view.root),/已选 1 \/ 13/)
assert.equal(button(view.root,'展开选择').props['aria-expanded'],false)
button(view.root,'展开选择').props.onClick();await flush()
assert.equal(input(view.root,'检索 CVPR').props.checked,true)
assert.match(text(view.root),/计算机视觉/);assert.match(text(view.root),/自然语言处理/);assert.match(text(view.root),/信息检索/)
input(view.root,'筛选会议或期刊').props.onInput({target:{value:'ICML'}});await flush()
assert.ok(input(view.root,'检索 ICML'));assert.equal(input(view.root,'检索 CVPR'),undefined)
assert.match(text(view.root),/已选 1 \/ 13/)
input(view.root,'筛选会议或期刊').props.onInput({target:{value:'no-matching-venue'}});await flush()
assert.match(text(view.root),/没有匹配的会议或期刊/)
button(view.root,'收起范围').props.onClick();await flush()
assert.equal(input(view.root,'筛选会议或期刊'),undefined)
assert.match(text(view.root),/CVPR/)
button(view.root,'展开选择').props.onClick();await flush()
input(view.root,'筛选会议或期刊').props.onInput({target:{value:''}});await flush()
assert.equal(input(view.root,'检索 CVPR').props.checked,true)
assert.equal(cvprRequests.length,0)
input(view.root,'顶刊顶会关键词').props.onInput({target:{value:'monocular depth'}});await flush()
button(view.root,'检索论文').props.onClick();await flush()
assert.equal(cvprRequests[0].query,'monocular depth');assert.equal(cvprRequests[0].page_size,5)
assert.match(text(view.root),/CVF 官方确认主会/);assert.match(text(view.root),/CVF 官方确认 Workshop/);assert.match(text(view.root),/索引记录，待核验/)
const checkbox=walk(view.root).find(n=>n.tag==='input'&&n.props.type==='checkbox'&&!n.props['aria-label'])
checkbox.props.onChange({target:{checked:true}});await flush()
button(view.root,'检索论文').props.onClick();await flush()
assert.equal(cvprRequests.at(-1).verified_main_only,true)
assert.ok(!text(view.root).includes('CVF 官方确认 Workshop'));assert.ok(!text(view.root).includes('CVPR depth paper 2'))
button(view.root,'收藏论文').props.onClick();await flush()
assert.equal(saved[0][0].venue_slug,'cvpr');assert.match(text(view.root),/新增 1 篇/)
input(view.root,'检索 ICCV').props.onChange({target:{checked:true}});await flush()
button(view.root,'检索论文').props.onClick();await flush();assert.equal(cvprRequests.at(-1).venues,'cvpr,iccv')
input(view.root,'检索 CVPR').props.onChange({target:{checked:false}});await flush()
button(view.root,'检索论文').props.onClick();await flush();assert.equal(cvprRequests.at(-1).venues,'iccv')
input(view.root,'检索 ICCV').props.onChange({target:{checked:false}});await flush()
const requestsBeforeEmpty=cvprRequests.length;button(view.root,'检索论文').props.onClick();await flush()
assert.equal(cvprRequests.length,requestsBeforeEmpty);assert.match(text(view.root),/请选择至少一个/)
input(view.root,'检索 AAAI').props.onChange({target:{checked:true}});await flush()
input(view.root,'检索 IJCAI').props.onChange({target:{checked:true}});await flush()
const publisherPapers=['aaai','ijcai'].map((slug,i)=>({...clone(cvprPapers[0]),venue_slug:slug,openalex_id:'https://openalex.org/W'+(20+i),title:slug.toUpperCase()+' paper',publication:{...clone(cvprPapers[0].publication),track:i?'other':'main',verification_provider:slug.toUpperCase()+' Proceedings'}}))
api.venueSearch=async params=>{cvprRequests.push(clone(params));return{items:clone(publisherPapers),total:2,page:params.page,page_size:params.page_size,notice:'官方论文集核验'}}
button(view.root,'检索论文').props.onClick();await flush()
assert.equal(cvprRequests.at(-1).venues,'aaai,ijcai')
assert.match(text(view.root),/AAAI Proceedings 官方确认主会/);assert.match(text(view.root),/IJCAI Proceedings 确认其他 Track/)
assert.ok(!text(view.root).includes('CVF 官方确认'))
button(view.root,'收藏论文').props.onClick();await flush();assert.equal(saved.at(-1)[0].venue_slug,'aaai')
input(view.root,'检索 CVPR').props.onChange({target:{checked:true}});await flush()
input(view.root,'检索 ICCV').props.onChange({target:{checked:true}});await flush()
button(view.root,'检索论文').props.onClick();await flush();assert.equal(cvprRequests.at(-1).venues,'aaai,ijcai,cvpr,iccv')
input(view.root,'检索 AAAI').props.onChange({target:{checked:false}});await flush()
input(view.root,'检索 CVPR').props.onChange({target:{checked:false}});await flush()
input(view.root,'检索 ICCV').props.onChange({target:{checked:false}});await flush()
button(view.root,'检索论文').props.onClick();await flush();assert.equal(cvprRequests.at(-1).venues,'ijcai')
input(view.root,'检索 IJCAI').props.onChange({target:{checked:false}});await flush()
const newSlugs=['acl','emnlp','sigir','neurips','artificial-intelligence','tois','eccv','icml','iclr']
for(const slug of newSlugs){input(view.root,'检索 '+venueNames[slug]).props.onChange({target:{checked:true}});await flush()}
api.venueSearch=async params=>{cvprRequests.push(clone(params));return{items:[{...clone(cvprPapers[0]),venue_slug:'tois',title:'TOIS journal paper',publication:{track:'journal',verification_status:'official_verified',verification_provider:'Crossref 出版记录',reason:'已核对出版记录'}}],total:1,page:1,page_size:5,notice:'期刊核验'}}
button(view.root,'检索论文').props.onClick();await flush()
assert.deepEqual(cvprRequests.at(-1).venues.split(','),newSlugs)
assert.match(text(view.root),/确认期刊发表/);assert.ok(!text(view.root).includes('CVF 官方确认主会'))
button(view.root,'收藏论文').props.onClick();await flush();assert.equal(saved.at(-1)[0].venue_slug,'tois')
assert.match(text(view.root),/另 5 项/)
button(view.root,'收起范围').props.onClick();await flush()
assert.match(text(view.root),/已选 9 \/ 13/);assert.equal(input(view.root,'检索 TOIS'),undefined)
button(view.root,'展开选择').props.onClick();await flush()
assert.equal(input(view.root,'检索 TOIS').props.checked,true)
button(view.root,'清空选择').props.onClick();await flush()
assert.match(text(view.root),/尚未选择会议或期刊/)
assert.ok(!text(view.root).includes('TOIS journal paper'),'切换选择清除旧结果')
const beforeClearSearch=cvprRequests.length;button(view.root,'检索论文').props.onClick();await flush()
assert.equal(cvprRequests.length,beforeClearSearch);assert.match(text(view.root),/请选择至少一个/)
view.close();console.log('PASS 13个会议/期刊多选 → 正式核验与期刊标签 → 过滤 → 收藏参数')

reset();cache.set('rp-data-mode','demo');view=mount(record,undefined,CVPRWorkspace);await flush()
assert.equal(button(view.root,'检索论文').props.disabled,true);view.close()
console.log('PASS CVPR 演示模式禁止调用真实检索')

reset();const cvprRun=makeRun();cvprRun.plan.scope.venue_slug='cvpr';publish(cvprRun)
view=mount({...record,plan:{...clone(proposal),scope:{...clone(proposal.scope),venue_slug:'cvpr'}}});await flush()
button(view.root,'重新检索并核验').props.onClick();await flush()
assert.equal(starts.at(-1).refresh,true);view.close()
console.log('PASS CVPR 显式重新核验提交 refresh，旧快照由后端保留')

// 使用同一实际 Vue 组件检查原文/译文分离、缓存恢复、轮询和请求竞态。
let translationStarts=[],translationRecords=new Map(),translationMode='succeeded',translationLookup=null,translationGets=0
const original='Original abstract.\nAccuracy improves by 2.5%.',zh='原文摘要的中文翻译。准确率提高了2.5%。'
const sourceKey=(title,abstract)=>JSON.stringify([title,abstract])
api.lookupAbstractTranslation=async(title,abstract)=>translationLookup?translationLookup(title,abstract):clone(translationRecords.get(sourceKey(title,abstract))||null)
api.abstractTranslation=async id=>{translationGets++;return clone([...translationRecords.values()].find(v=>v.record.id===id))}
api.startAbstractTranslation=async(title,abstract)=>{
 translationStarts.push({title,abstract});const id='translation-'+translationStarts.length,taskId='translation-task-'+translationStarts.length
 const task={id:taskId,type:'abstract_translation',translation_id:id,status:translationMode,stage:'中文翻译',error:translationMode==='failed'?{code:'MODEL_TIMEOUT',message:'翻译超时'}:null}
 const value={record:{id,title,original_abstract:abstract,translation_zh:translationMode==='succeeded'?zh:null,task_id:taskId,model_name:'test-model'},task}
 translationRecords.set(sourceKey(title,abstract),value);tasks.set(taskId,task)
 return{task_id:taskId,translation_id:id,reused:false}
}
function mountTranslation(values){
 const root=node('root'),pinia=createPinia(),inputs=ref(values)
 const app=renderer.createApp({setup:()=>()=>h(AbstractTranslation,inputs.value)})
 app.use(pinia);app.component('ElButton',Button);app.mount(root)
 return{root,inputs,store:useTaskStore(pinia),close(){useTaskStore(pinia).stop();app.unmount()}}
}
reset();view=mountTranslation({title:'Paper',abstract:original});await flush()
assert.equal(translationStarts.length,0);assert.ok(text(view.root).includes(original))
button(view.root,'生成中文翻译').props.onClick();await flush()
assert.deepEqual(translationStarts,[{title:'Paper',abstract:original}])
assert.ok(text(view.root).includes(original));assert.ok(text(view.root).includes(zh));assert.match(text(view.root),/引用请核对原文/)
assert.ok(translationGets<5,'成功状态不能触发无限刷新')
view.close();view=mountTranslation({title:'Paper',abstract:original});await flush()
assert.ok(text(view.root).includes(zh));assert.equal(translationStarts.length,1)
view.inputs.value={title:'Paper',abstract:'A changed abstract.'};await flush()
assert.ok(!text(view.root).includes(zh));assert.ok(text(view.root).includes('A changed abstract.'))
view.close();console.log('PASS 点击才翻译 → 原文与译文并存 → 缓存恢复 → 摘要改变清除旧译文')

reset();translationRecords.clear();translationMode='running';view=mountTranslation({title:'Pending',abstract:original});await flush()
button(view.root,'生成中文翻译').props.onClick();await flush()
assert.equal(button(view.root,'生成中文翻译').props.disabled,true);assert.ok(text(view.root).includes(original))
const pending=translationRecords.get(sourceKey('Pending',original));pending.record.translation_zh=zh;pending.task.status='succeeded'
view.store.tasks[pending.task.id]=clone(pending.task);await flush()
assert.ok(text(view.root).includes(zh));view.close();console.log('PASS 翻译期间保留原文，后台任务完成后自动显示中文')

reset();translationRecords.clear();translationMode='failed';view=mountTranslation({title:'Retry',abstract:original});await flush()
button(view.root,'生成中文翻译').props.onClick();await flush()
assert.match(text(view.root),/翻译超时/);assert.ok(text(view.root).includes(original));assert.ok(!text(view.root).includes(zh))
translationMode='succeeded';button(view.root,'重试中文翻译').props.onClick();await flush()
assert.ok(text(view.root).includes(zh));view.close();console.log('PASS 翻译失败可重试，原文摘要始终保留')

reset();let finishLookup
translationLookup=(title,abstract)=>title==='Old'?new Promise(resolve=>{finishLookup=()=>resolve({record:{id:'old',title,original_abstract:abstract,translation_zh:'过期译文',task_id:'old-task'},task:null})}):null
view=mountTranslation({title:'Old',abstract:original});await flush();view.inputs.value={title:'New',abstract:'New source.'};await flush();finishLookup();await flush()
assert.ok(!text(view.root).includes('过期译文'));assert.ok(text(view.root).includes('New source.'));view.close();translationLookup=null
console.log('PASS 切换论文后延迟返回的旧译文不会覆盖新摘要')

reset();view=mountTranslation({abstract:'  '});await flush();assert.equal(button(view.root,'生成中文翻译').props.disabled,true);view.close()
cache.set('rp-data-mode','demo');const startsBeforeDemo=translationStarts.length
view=mountTranslation({abstract:original});await flush();assert.equal(button(view.root,'生成中文翻译').props.disabled,true)
assert.equal(translationStarts.length,startsBeforeDemo);view.close();console.log('PASS 无摘要和演示模式不生成或伪造译文')

// 从候选选择到固定计划版本的全文/分析执行；后台结果以实际响应恢复。
reset();publish(makeRun());let executionRuns=[],executionStarts=[],executionResumes=[]
api.planExecutions=async()=>({items:clone(executionRuns)})
api.startPlanExecution=async(id,body)=>{
 executionStarts.push({id,body:clone(body)})
 const execution={id:'execution-1',revision:1,plan_id:id,search_run_id:body.search_run_id,plan:clone(proposal),created_at:'2026-11-11',task_id:'execution-task',status:'failed',error:{code:'PLAN_EXECUTION_INCOMPLETE',message:'部分论文未完成'},papers:body.candidate_ids.map((candidate_id,i)=>({candidate_id,title:'Robot paper '+(i+1),paper_id:i+1,screening:{decision:i?'uncertain':'included'},fulltext:{status:'succeeded',document_id:10+i,reused:true},analysis:i?{status:'failed',error:{code:'MODEL_TIMEOUT',message:'模型超时'}}:{status:'succeeded',analysis_id:'analysis-1'}}))}
 executionRuns=[execution];tasks.set('execution-task',{id:'execution-task',type:'research_execution',status:'failed',error:execution.error,updated_at:'1'})
 return{task_id:'execution-task',execution_id:'execution-1',reused:false}
}
api.resumePlanExecution=async(id,execution_id)=>{
 executionResumes.push({id,execution_id});executionRuns[0].status='succeeded';executionRuns[0].error=null;executionRuns[0].papers[1].analysis={status:'succeeded',analysis_id:'analysis-2'}
 tasks.set('execution-task-2',{id:'execution-task-2',type:'research_execution',status:'succeeded',updated_at:'2'})
 return{task_id:'execution-task-2',execution_id,reused:false}
}
view=mount();await flush()
assert.equal(button(view.root,'确认所选论文并执行').props.disabled,true)
input(view.root,'选择论文 Robot paper 1').props.onChange({target:{checked:true}});await flush()
assert.equal(button(view.root,'确认所选论文并执行').props.disabled,true)
input(view.root,'选择论文 Robot paper 2').props.onChange({target:{checked:true}});await flush()
assert.equal(button(view.root,'确认所选论文并执行').props.disabled,false)
view.disabled.value=true;await flush();assert.equal(button(view.root,'确认所选论文并执行').props.disabled,true)
view.disabled.value=false;await flush();button(view.root,'确认所选论文并执行').props.onClick();await flush()
assert.deepEqual(executionStarts,[{id:record.id,body:{expected_revision:1,search_run_id:'search-1',candidate_ids:['C1','C2']}}])
assert.match(text(view.root),/模型超时/);assert.match(text(view.root),/1 \/ 2 篇分析完成/)
assert.match(text(view.root),/复用已有结果/)
view.close();view=mount();await flush()
assert.match(text(view.root),/模型超时/);assert.equal(executionStarts.length,1)
button(view.root,'继续本轮未完成部分').props.onClick();await flush()
assert.deepEqual(executionResumes,[{id:record.id,execution_id:'execution-1'}])
assert.match(text(view.root),/2 \/ 2 篇分析完成/);assert.match(text(view.root),/下方继续比较、综述和导出/)
view.close()
view=mount({...record,revision:2});await flush()
input(view.root,'选择论文 Robot paper 1').props.onChange({target:{checked:true}})
input(view.root,'选择论文 Robot paper 2').props.onChange({target:{checked:true}});await flush()
assert.equal(button(view.root,'确认所选论文并执行').props.disabled,true)
view.close();console.log('PASS 选文数量/版本门槛 → 固定候选执行 → 失败进度刷新恢复 → 继续未完成 → 明确仅完成全文与分析')

// 快速模式提交独立范围，刷新后继续显示其阅读覆盖。
reset();publish(makeRun());executionRuns=[];executionStarts=[]
view=mount();await flush()
input(view.root,'计划论文阅读模式').props.onChange({target:{value:'fast'}});await flush()
input(view.root,'选择论文 Robot paper 1').props.onChange({target:{checked:true}})
input(view.root,'选择论文 Robot paper 2').props.onChange({target:{checked:true}});await flush()
button(view.root,'确认所选论文并执行').props.onClick();await flush()
assert.equal(executionStarts[0].body.analysis_scope,'fast')
executionRuns[0].analysis_scope='fast'
executionRuns[0].papers[0].analysis.reading_selection={scope:'fast',policy_version:'test',budget:18,strategy:'section_priority',selected_chunks:18,total_chunks:141,selected_pages:[1,3,6],sections:[{category:'method',label:'方法与技术方案',available_chunks:30,selected_chunks:6}],missing_sections:['结论与局限'],note:'仅覆盖选定正文，未通读全文'}
button(view.root,'刷新执行进度').props.onClick();await flush()
assert.match(text(view.root),/18 \/ 141/);assert.match(text(view.root),/方法与技术方案/)
assert.match(text(view.root),/未识别到章节标题/);assert.match(text(view.root),/未通读全文/)
view.close();console.log('PASS 计划快速阅读模式请求 → 覆盖页码/章节/缺失提示 → 独立结果标签')

reset();publish(makeRun());executionRuns=[{...clone(executionRuns[0]),status:'succeeded'}]
let reportRecords=[],reportStarts=[]
const planMatrix={paper_ids:[1,2],analysis_ids:['analysis-1','analysis-2'],matrix:[1,2].map(i=>({paper_id:i,title:'固定论文 '+i,analysis_id:'analysis-'+i,analysis:{research_questions:[],methodologies:[],contributions:[],experiments:[],limitations:[]}}))}
api.planComparison=async()=>clone(planMatrix)
api.planReports=async()=>({items:clone(reportRecords)})
api.startPlanReport=async(id,execution,body)=>{
 reportStarts.push({id,execution,body:clone(body)})
 reportRecords=[{id:'report-fixed',revision:1,status:'succeeded',research_question:body.research_question,review_id:'review-fixed',comparison:clone(planMatrix),task:{id:'report-task',status:'succeeded'},context:{comparison_dimensions:body.comparison_dimensions,reading_coverage:[{paper_id:1,scope:'fast',selected_chunks:18,total_chunks:141}]}}]
 return{task_id:'report-task',report_id:'report-fixed',reused:false}
}
api.review=async()=>({id:'review-fixed',topic:'确认后的研究问题',paper_ids:[1,2],analysis_ids:['analysis-1','analysis-2'],is_current:true,draft:{title:'按计划生成的报告',background:[],common_methods:[],comparisons:[],limitations:[],tentative_directions:[]},matrix:planMatrix.matrix})
view=mount();await flush()
assert.equal(input(view.root,'本轮综述研究问题').props.value,proposal.research_questions.join('\n'))
input(view.root,'本轮综述研究问题').props.onInput({target:{value:'确认后的研究问题'}})
input(view.root,'本轮综述比较维度').props.onInput({target:{value:'精度\n能耗'}});await flush()
button(view.root,'查看本轮比较矩阵').props.onClick();await flush();assert.match(text(view.root),/固定论文 1/)
button(view.root,'确认问题并生成本轮综述').props.onClick();await flush()
assert.deepEqual(reportStarts[0],{id:record.id,execution:'execution-1',body:{research_question:'确认后的研究问题',comparison_dimensions:['精度','能耗']}})
assert.match(text(view.root),/按计划生成的报告/);assert.match(text(view.root),/快速阅读/)
const md=walk(view.root).find(n=>n.tag==='a'&&text(n)==='导出 Markdown')
assert.ok(md.props.href.includes('/reviews/review-fixed/files/md'))
view.close();view=mount();await flush()
assert.equal(input(view.root,'本轮综述研究问题').props.value,'确认后的研究问题')
assert.match(text(view.root),/按计划生成的报告/);assert.equal(reportStarts.length,1)
view.close();console.log('PASS 本轮完成 → 问题与维度确认 → 固定分析比较 → 综述 → 导出链接 → 刷新恢复')
