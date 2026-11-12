import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick,ref} from 'vue'
import {createPinia} from 'pinia'
import Picker from '../src/components/ReviewQuestionPicker.vue'
import {api} from '../src/api/index.ts'
import {useTaskStore} from '../src/stores/tasks.ts'

// Vue 自定义渲染器驱动真实组件状态和事件；输入/按钮及引用展示使用轻量替身。
const cache=new Map()
globalThis.localStorage={getItem:k=>cache.get(k)||null,setItem:(k,v)=>cache.set(k,String(v)),removeItem:k=>cache.delete(k)}
globalThis.document={hidden:false}
const node=(tag,text='')=>({tag,text,props:{},children:[],parent:null})
const renderer=createRenderer({
 createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
 setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},
 patchProp:(n,key,previous,value)=>{n.props[key]=value},parentNode:n=>n.parent,
 nextSibling:n=>{const list=n.parent?.children||[];return list[list.indexOf(n)+1]||null},
 insert(n,parent,anchor){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1)}n.parent=parent;const i=anchor?parent.children.indexOf(anchor):-1;parent.children.splice(i<0?parent.children.length:i,0,n)},
 remove(n){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1);n.parent=null}},
})
const Input=defineComponent({props:['modelValue','disabled'],emits:['update:modelValue'],setup:(p,{emit})=>()=>h('textarea',{value:p.modelValue,disabled:p.disabled,onInput:e=>emit('update:modelValue',e.target.value)})})
const Button=defineComponent({props:['disabled','loading'],setup:(p,{attrs,slots})=>()=>h('button',{...attrs,disabled:p.disabled||p.loading},slots.default?.())})
const walk=n=>[n,...n.children.flatMap(walk)]
const text=n=>n.text+n.children.map(text).join('')
const button=(root,label)=>walk(root).find(n=>n.tag==='button'&&text(n).includes(label))
const flush=async()=>{for(let i=0;i<7;i++){await nextTick();await new Promise(r=>setImmediate(r))}}
let calls=[],records=new Map(),requestHook=null,nextStatus='succeeded'
function reset(mode='api'){cache.clear();cache.set('rp-data-mode',mode);calls=[];records=new Map();requestHook=null;nextStatus='succeeded'}
api.suggestReviewQuestions=async(paper_ids,analysis_ids)=>{
 calls.push({paper_ids,analysis_ids})
 const id='questions-'+calls.length
 records.set(id,{id,type:'review_questions',paper_ids,analysis_ids,status:nextStatus,stage:'推荐完成',progress:100,result_id:null,error:nextStatus==='failed'?{code:'TEST_FAILURE',message:'推荐失败，请重试'}:null,
  question_suggestions:[{question:`问题 ${paper_ids.join('/')}：方法有何差异？`,rationale:'两篇论文提供了可比较的方法证据。',evidence_refs:paper_ids.map(p=>`P${p}:E${p}`)}]})
 return requestHook?requestHook(id):{task_id:id}
}
api.task=async id=>records.get(id)
function mount(){
 const topic=ref(''),papers=ref([1,3]),versions=ref(['a1','a3']),disabled=ref(false),root=node('root'),pinia=createPinia()
 const app=renderer.createApp({setup:()=>()=>h(Picker,{paperIds:papers.value,analysisIds:versions.value,modelValue:topic.value,disabled:disabled.value,'onUpdate:modelValue':v=>{topic.value=v}})})
 app.use(pinia);app.component('ElInput',Input);app.component('ElButton',Button);app.mount(root)
 return{root,topic,papers,versions,disabled,close(){useTaskStore(pinia).stop();app.unmount()}}
}

reset()
let view=mount();await flush()
assert.deepEqual(calls,[{paper_ids:[1,3],analysis_ids:['a1','a3']}])
assert.equal(view.topic.value,'')
button(view.root,'问题 1/3').props.onClick();await flush()
assert.match(view.topic.value,/问题 1\/3/)
walk(view.root).find(n=>n.tag==='textarea').props.onInput({target:{value:'用户修改后的研究问题'}});await flush()
assert.equal(view.topic.value,'用户修改后的研究问题')
button(view.root,'重新推荐').props.onClick();await flush()
assert.equal(calls.length,2);assert.equal(view.topic.value,'用户修改后的研究问题')
view.close()
view=mount();await flush()
assert.equal(calls.length,2);assert.equal(view.topic.value,'用户修改后的研究问题')
view.disabled.value=true;await flush();assert.equal(button(view.root,'问题 1/3').props.disabled,true)
view.close();console.log('PASS 自动推荐 → 明确选择 → 编辑 → 重新推荐保留输入 → 刷新恢复')

reset();let finishOld
requestHook=id=>id==='questions-1'?new Promise(resolve=>{finishOld=()=>resolve({task_id:id})}):{task_id:id}
view=mount();await flush()
view.papers.value=[2,3];view.versions.value=['a2','a3'];await flush()
finishOld();await flush()
assert.ok(button(view.root,'问题 2/3'));assert.equal(button(view.root,'问题 1/3'),undefined)
assert.deepEqual(calls[1],{paper_ids:[2,3],analysis_ids:['a2','a3']})
view.close();console.log('PASS 切换论文后旧请求迟到，不覆盖新问题')

reset();nextStatus='failed';view=mount();await flush()
assert.match(text(view.root),/推荐失败，请重试/)
walk(view.root).find(n=>n.tag==='textarea').props.onInput({target:{value:'失败后自行填写的问题'}});await flush()
nextStatus='succeeded';button(view.root,'重新推荐').props.onClick();await flush()
assert.equal(view.topic.value,'失败后自行填写的问题');assert.ok(button(view.root,'问题 1/3'))
view.close();console.log('PASS 推荐失败仍可编辑，重试后保留手动问题')

reset('demo');view=mount();await flush()
assert.equal(calls.length,0);assert.match(text(view.root),/演示模式不调用模型/)
view.close();console.log('PASS 演示模式不伪造推荐或自动调用模型')
