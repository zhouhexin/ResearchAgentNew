import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Panel from '../src/components/ClaimReviewPanel.vue'
import {api} from '../src/api/index.ts'
import {useTaskStore} from '../src/stores/tasks.ts'
import {useEvidenceStore} from '../src/stores/evidence.ts'
const cache=new Map(),clone=v=>JSON.parse(JSON.stringify(v))
globalThis.localStorage={getItem:k=>cache.get(k)||null,setItem:(k,v)=>cache.set(k,String(v)),removeItem:k=>cache.delete(k)}
globalThis.document={hidden:false}
const node=(tag,text='')=>({tag,text,props:{},children:[],parent:null})
const renderer=createRenderer({createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
 setText:(n,t)=>{n.text=t},setElementText:(n,t)=>{n.text=t;n.children=[]},patchProp:(n,k,p,v)=>{n.props[k]=v},parentNode:n=>n.parent,
 nextSibling:n=>{const list=n.parent?.children||[];return list[list.indexOf(n)+1]||null},
 insert(n,p,a){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1)}n.parent=p;const i=a?p.children.indexOf(a):-1;p.children.splice(i<0?p.children.length:i,0,n)},
 remove(n){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1);n.parent=null}}})
const Button=defineComponent({props:['disabled','loading'],setup:(p,{attrs,slots})=>()=>h('button',{...attrs,disabled:p.disabled||p.loading},slots.default?.())})
const Tag=defineComponent({setup:(p,{slots})=>()=>h('span',slots.default?.())})
const Select=defineComponent({props:['modelValue'],emits:['update:modelValue'],setup:(p,{attrs,emit,slots})=>()=>h('select',{...attrs,value:p.modelValue,onChange:e=>emit('update:modelValue',e.target.value)},slots.default?.())})
const Option=defineComponent({props:['value','label'],setup:p=>()=>h('option',{value:p.value},p.label)})
const walk=n=>[n,...n.children.flatMap(walk)],text=n=>n.text+n.children.map(text).join('')
const button=(root,label)=>walk(root).find(n=>n.tag==='button'&&text(n).includes(label))
const flush=async()=>{for(let i=0;i<10;i++){await nextTick();await new Promise(r=>setImmediate(r))}}
let starts=[],records=[],lookupHook=null,lookups=0
api.claimReviews=async id=>{lookups++;return lookupHook?lookupHook(id):{items:clone(records.filter(r=>r.analysis_id===id))}}
api.claimReview=async id=>clone(records.find(r=>r.id===id))
api.task=async id=>clone(records.find(r=>r.task.id===id).task)
api.evidence=async id=>({evidence_id:id,paper_id:1,page_number:1,quote:'原文连续\n引句及其完整上下文。',document_id:10,verification:'location_verified'})
function makeRecord(completed=1,id='review-one',analysisId='analysis-one'){
 return{id,analysis_id:analysisId,paper_id:1,document_id:10,content_sha256:'hash',created_at:'2026-11-16',model_name:'test-model',is_current:true,total:2,completed,quote_protocol:'references-v2',legacy_reused:1,
  task:{id:`task-${id}-${completed}`,type:'claim_review',status:completed===2?'succeeded':'failed',stage:completed===2?'评审已完成':'已保存一条，可继续',progress:100,updated_at:String(completed)},
  items:[0,1].map(i=>({sample_id:`item-${i}`,category:'experiments',finding_index:i,statement:`结论 ${i+1}`,
   evidence:[{evidence_id:'E1',page_number:1,quote:'原文连续\n引句',context:'原文连续\n引句及其完整上下文。'}],
   status:i<completed?'succeeded':'failed',review:i<completed?{item_id:`item-${i}`,verdict:i===0?'supported':'insufficient',reason:i===0?'原文支持结论。':'未提供所需实验条件。',
    evidence_assessments:[{evidence_id:'E1',relation:'supports',reason:'逐条依据。',quoted_spans:['原文连续\n引句']}]}:null,
   error:i<completed?null:{code:'CLAIM_REVIEW_INVALID',message:'引句未通过核验'}}))}
}
api.startClaimReview=async id=>{starts.push(id);records=[makeRecord(starts.length===1?1:2)];return{task_id:records[0].task.id,review_id:records[0].id,reused:false}}
function mount(initial={id:'analysis-one',is_current:true,paper:{title:'测试论文'},document:{source_url:'本地导入 PDF'}},initialReviewId){
 const root=node('root'),pinia=createPinia(),analysis=ref(initial)
 const app=renderer.createApp({setup:()=>()=>h(Panel,{analysis:analysis.value,initialReviewId})})
 app.use(pinia);app.component('ElButton',Button);app.component('ElTag',Tag);app.component('ElSelect',Select);app.component('ElOption',Option);app.mount(root)
 return{root,analysis,pinia,close(){useTaskStore(pinia).stop();app.unmount()}}
}
cache.set('rp-data-mode','api')
let view=mount();await flush()
assert.equal(starts.length,0)
assert.match(text(view.root),/尚未发起辅助评审/)
button(view.root,'辅助语义评审').props.onClick();await flush()
assert.deepEqual(starts,['analysis-one'])
assert.match(text(view.root),/已完成 1 \/ 2 条/)
assert.match(text(view.root),/引用由后端直接提取/)
assert.match(text(view.root),/复用旧版已核验的 1 条结果/)
assert.match(text(view.root),/模型评审：支持/)
assert.match(text(view.root),/评审未完成/)
assert.match(text(view.root),/CLAIM_REVIEW_INVALID/)
assert.match(text(view.root),/原文连续\n引句/)
walk(view.root).find(n=>n.props['aria-label']==='查看原文 [1，第1页]').props.onClick();await flush()
assert.equal(useEvidenceStore(view.pinia).visible,true)
assert.equal(useEvidenceStore(view.pinia).reference,'P1:E1')
assert.deepEqual(useEvidenceStore(view.pinia).citation.quotes,['原文连续\n引句及其完整上下文。'])
button(view.root,'继续未完成评审').props.onClick();await flush()
assert.equal(starts.length,2)
assert.match(text(view.root),/已完成 2 \/ 2 条/)
assert.match(text(view.root),/模型评审：证据不足/)
assert.ok(!text(view.root).includes('CLAIM_REVIEW_INVALID'))
view.close();view=mount();await flush()
assert.equal(starts.length,2)
assert.match(text(view.root),/已完成 2 \/ 2 条/)
view.analysis.value={...view.analysis.value,is_current:false};await flush()
assert.equal(button(view.root,'辅助语义评审').props.disabled,true)
assert.match(text(view.root),/当前为旧版分析/)
assert.ok(!walk(view.root).some(n=>n.props['aria-label']?.startsWith('查看原文')))
view.close()
console.log('PASS 显式发起 → 逐条结果/原文 → 引用打开 → 失败继续 → 刷新恢复 → 旧分析禁用')

let resolveOld
lookupHook=id=>id==='old'?new Promise(resolve=>{resolveOld=()=>resolve({items:[makeRecord(2,'review-old','old')]})}):{items:[]}
view=mount({id:'old',is_current:true});await flush()
view.analysis.value={id:'new',is_current:true};await flush();resolveOld();await flush()
assert.match(text(view.root),/尚未发起辅助评审/)
assert.ok(!text(view.root).includes('结论 1'))
view.close();lookupHook=null
console.log('PASS 切换分析时旧请求不能覆盖当前评审')

cache.set('rp-data-mode','demo');const before=lookups;view=mount();await flush()
assert.equal(lookups,before)
assert.equal(button(view.root,'辅助语义评审').props.disabled,true)
assert.match(text(view.root),/切换到真实 API/)
view.close()
console.log('PASS 演示模式不发起评审或读取真实评审')
