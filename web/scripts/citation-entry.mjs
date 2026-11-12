import assert from 'node:assert/strict'
import {createRenderer,defineComponent,h,nextTick} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory} from 'vue-router'
import Body from '../src/components/ReviewReportBody.vue'
import Drawer from '../src/components/EvidenceDrawer.vue'
import {useEvidenceStore} from '../src/stores/evidence.ts'
import {api} from '../src/api/index.ts'

const node=(tag,text='')=>({tag,text,props:{},children:[],parent:null})
const renderer=createRenderer({createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
 setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},patchProp:(n,k,p,v)=>{n.props[k]=v},parentNode:n=>n.parent,
 nextSibling:n=>{const list=n.parent?.children||[];return list[list.indexOf(n)+1]||null},
 insert(n,p,a){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1)}n.parent=p;const i=a?p.children.indexOf(a):-1;p.children.splice(i<0?p.children.length:i,0,n)},
 remove(n){if(n.parent){const list=n.parent.children;list.splice(list.indexOf(n),1);n.parent=null}}})
const Tag=defineComponent({setup:(p,{slots})=>()=>h('div',slots.default?.())})
const walk=n=>[n,...n.children.flatMap(walk)],text=n=>n.text+n.children.map(text).join('')
const flush=async()=>{for(let i=0;i<8;i++){await nextTick();await new Promise(r=>setImmediate(r))}}
const quote='完整原文引句，超过旧的三百字符截断限制。'.repeat(25)+'THE END'
const citation={ref_id:'P28:E689',paper_id:28,paper_number:1,paper_title:'First paper',page_number:6,source_url:'fixture',footnote_id:'evidence-1',quotes:[quote,'Second exact quote.'],quote_kind:'analysis_quotes'}
const other={...citation,ref_id:'P29:E640',paper_id:29,paper_number:2,paper_title:'Second paper',page_number:null,footnote_id:'evidence-2',quotes:['Other quotation.']}
const draft={title:'Citation check',background:[{claim:'结论',evidence_refs:[citation.ref_id,other.ref_id],paragraphs:[{text:'解释段落',evidence_refs:[citation.ref_id]}]}],common_methods:[],comparisons:[],limitations:[],tentative_directions:[]}
const review={id:'test',paper_ids:[28,29],topic:'test',draft,citation_index:{[citation.ref_id]:citation,[other.ref_id]:other},matrix:[{paper_id:28,title:'First paper'},{paper_id:29,title:'Second paper'}]}
const root=node('root'),pinia=createPinia(),router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:Tag}]})
await router.push('/reviews')
const app=renderer.createApp({setup:()=>()=>h('main',[h(Body,{draft,review}),h(Drawer)])})
app.use(pinia).use(router)
for(const name of ['ElDrawer','ElTag','ElButton','ElSkeleton','ElInputNumber'])app.component(name,Tag)
let calls=0,fail=false,changed=false,pdfAvailable=true,pageUnknown=false
api.evidence=async id=>{calls++;if(fail)throw Error('来源暂时不可用');return{evidence_id:id,paper_id:id==='E689'?28:29,paper_title:'Current paper',document_id:41,page_number:pageUnknown?null:6,page_count:12,source_type:'pdf',pdf_file_url:pdfAvailable?'/api/v1/documents/41/file':null,quote:changed?'Changed source text.':'context prefix '+quote+' Second exact quote.',source_url:'fixture',verification:'location_verified',start_char:0,end_char:20}}
app.mount(root);await flush()
let buttons=walk(root).filter(n=>n.tag==='button')
assert.equal(text(buttons[0]),'[1，第6页]')
assert.equal(text(buttons[1]),'[2，无页码]')
assert.equal(text(buttons[2]),'[1，第6页]')
assert.match(text(root),/参考文献/)
assert.match(text(root),/\[1\] First paper/)
buttons[0].props.onClick();await flush()
const store=useEvidenceStore(pinia)
assert.equal(store.reference,'P28:E689');assert.equal(store.evidence.paper_id,28)
let blocks=walk(root).filter(n=>n.tag==='blockquote')
assert.equal(text(blocks[0]),quote);assert.equal(text(blocks[1]),'Second exact quote.')
assert.equal(calls,1)
const frame=()=>walk(root).find(n=>n.tag==='iframe')
assert.match(frame().props.src,/\/api\/v1\/documents\/41\/file#page=6&toolbar=1$/)
const next=walk(root).find(n=>typeof n.props.onClick==='function'&&text(n)==='下一页')
next.props.onClick();await flush()
assert.match(frame().props.src,/#page=7&toolbar=1$/)
// 再点同一引用也回到原始引用页，而非停在用户翻到的第七页。
buttons[0].props.onClick();await flush()
assert.match(frame().props.src,/#page=6&toolbar=1$/)
pdfAvailable=false;await store.open(citation.ref_id,citation);await flush()
assert.equal(frame(),undefined)
assert.match(text(root),/原 PDF 文件暂不可读取/)
assert.equal(text(walk(root).find(n=>n.tag==='blockquote')),quote)
pdfAvailable=true;pageUnknown=true;await store.open(citation.ref_id,citation);await flush()
assert.match(frame().props.src,/#page=1&toolbar=1$/)
assert.match(text(root),/未记录页码/)
pageUnknown=false
changed=true;await store.open(citation.ref_id,citation);await flush()
assert.match(text(root),/历史快照/)
assert.match(text(root),/当前原文片段与报告引句不一致/)
fail=true;await store.open(citation.ref_id,citation);await flush()
assert.match(text(root),/来源暂时不可用/)
assert.equal(text(walk(root).find(n=>n.tag==='blockquote')),quote)
fail=false;await store.open('P28:E689');await flush()
assert.equal(store.citation,null)
assert.equal(walk(root).filter(n=>n.tag==='blockquote').length,0)
app.unmount()
console.log('PASS citation click opens the exact PDF/page, page controls and reopening reset, missing PDF fallback, full quotes and frozen history')
