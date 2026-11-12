import assert from 'node:assert/strict'
import {createPinia,setActivePinia} from 'pinia'
import {api} from '../src/api/index.ts'
import {useEvidenceStore} from '../src/stores/evidence.ts'
const result=await api.search({q:'Attention',page:1,page_size:20})
assert.ok(result.items.length>0)
const unsaved=result.items.find(p=>!p.saved_paper_id&&p.identity_verified)
if(unsaved){assert.equal((await api.savePapers([unsaved])).created,1);assert.equal((await api.savePapers([unsaved])).existing,1)}
console.log('PASS 检索 → 收藏 → 重复去重')
setActivePinia(createPinia())
const store=useEvidenceStore()
await store.open('P2:E21');assert.equal(store.evidence.paper_id,2)
await store.open('P3:E21');assert.equal(store.evidence,null);assert.match(store.error,/所属论文不一致/)
await Promise.all([store.open('P2:E21'),store.open('P3:E46')]);assert.equal(store.evidence.paper_id,3)
console.log('PASS 引用归属核验与并发请求隔离')
await assert.rejects(()=>api.startReview([2,2],'单光子'),/不同论文/)
await assert.rejects(()=>api.startReview([2,3],'Transformer 医学图像分割'),/演示报告仅支持/)
const comparison=await api.comparePapers([2,3]);assert.deepEqual(comparison.paper_ids,[2,3]);assert.equal(comparison.analysis_ids.length,2)
const task=await api.startReview([2,3],'单光子三维成像',comparison.analysis_ids)
assert.equal((await api.task(task.task_id)).status,'running')
const now=Date.now;Date.now=()=>now()+5000
try{assert.equal((await api.task(task.task_id)).status,'succeeded')}finally{Date.now=now}
const review=await api.review('review_demo');assert.equal(review.paper_ids.length,2)
for(const section of Object.values(review.draft).filter(Array.isArray))for(const point of section)for(const ref of point.evidence_refs){await store.open(ref);assert.equal(store.error,'',ref)}
console.log('PASS 综述选择限制、任务生命周期、全部综述引用')
await assert.rejects(()=>api.chat('问题',null),/不生成模型回答/)
console.log('PASS 演示模式不伪造模型回答')
assert.equal(await api.latestModelCheck({mode:'structured'}),null)
await assert.rejects(()=>api.startModelCheck({mode:'structured'}),/真实 API 模式/)
console.log('PASS 演示模式不伪造模型检测结果')
await assert.rejects(()=>api.suggestReviewQuestions([2,3],comparison.analysis_ids),/演示模式不调用模型推荐/)
console.log('PASS 演示模式不伪造研究问题推荐')
await assert.rejects(()=>api.startResearchPlan('研究机器人视觉评测'),/演示模式不生成或保存研究计划/)
await assert.rejects(()=>api.startPlanSearch('plan-test',1,5),/演示模式不生成或保存研究计划/)
await assert.rejects(()=>api.planSearch('plan-test'),/演示模式不生成或保存研究计划/)
await assert.rejects(()=>api.cvprSearch({query:'depth'}),/需要真实 API/)
await assert.rejects(()=>api.venueSearch({query:'depth',venues:'cvpr,iccv'}),/需要真实 API/)
await assert.rejects(()=>api.startAbstractTranslation('Paper','Original abstract'),/需要真实 API/)
console.log('PASS 演示模式不伪造研究计划')

const {http}=await import('../src/api/http.ts')
localStorage.setItem('rp-data-mode','api')
const adapter=http.defaults.adapter
try{for(const status of [422,429,500]){http.defaults.adapter=async()=>{throw {response:{status,data:{error:{code:'TEST_'+status,message:'示例错误 '+status}}}}};await assert.rejects(()=>api.papers(),e=>e.code==='TEST_'+status&&e.status===status)}}finally{http.defaults.adapter=adapter;localStorage.removeItem('rp-data-mode')}
console.log('PASS API 422 / 429 / 500 错误映射')

localStorage.setItem('rp-data-mode','api')
try{
 const calls=[]
 http.defaults.adapter=async config=>{
  calls.push(config)
  return {data:config.method==='post'?{task_id:'model-check-test',reused:false}:null,status:200,statusText:'OK',headers:{},config}
 }
 assert.equal((await api.startModelCheck({mode:'structured',http_backend:'requests',timeout:60})).task_id,'model-check-test')
 assert.equal(await api.latestModelCheck({mode:'chat',http_backend:'sdk'}),null)
 assert.equal(calls[0].url,'/system/model-check')
 assert.deepEqual(JSON.parse(calls[0].data),{mode:'structured',http_backend:'requests',timeout:60})
 assert.deepEqual(calls[1].params,{mode:'chat',http_backend:'sdk'})
}finally{http.defaults.adapter=adapter;localStorage.removeItem('rp-data-mode')}
console.log('PASS 模型检测的配置选择、后台任务响应和历史查询')

localStorage.setItem('rp-data-mode','api')
try{
 const calls=[]
 http.defaults.adapter=async config=>{calls.push(config);return {data:{task_id:'question-test'},status:202,statusText:'Accepted',headers:{},config}}
 assert.equal((await api.suggestReviewQuestions([2,3],['a2','a3'])).task_id,'question-test')
 assert.equal(calls[0].url,'/reviews/questions')
 assert.deepEqual(JSON.parse(calls[0].data),{paper_ids:[2,3],analysis_ids:['a2','a3']})
}finally{http.defaults.adapter=adapter;localStorage.removeItem('rp-data-mode')}
console.log('PASS 问题推荐提交固定的论文和分析版本')

localStorage.setItem('rp-data-mode','api')
try{
 const calls=[]
 http.defaults.adapter=async config=>{calls.push(config);return {data:{task_id:'planning-test'},status:202,statusText:'Accepted',headers:{},config}}
 assert.equal((await api.startResearchPlan('研究机器人视觉评测')).task_id,'planning-test')
 assert.equal(calls[0].url,'/agent/plans')
 assert.deepEqual(JSON.parse(calls[0].data),{goal:'研究机器人视觉评测'})
 await api.saveResearchPlan('plan-test',{topic:'修改后的研究主题'},2)
 assert.equal(calls[1].method,'put');assert.equal(calls[1].url,'/agent/plans/plan-test')
 assert.equal(JSON.parse(calls[1].data).expected_revision,2)
 await api.startPlanSearch('plan-test',2,5)
 assert.equal(calls[2].url,'/agent/plans/plan-test/search')
 assert.deepEqual(JSON.parse(calls[2].data),{expected_revision:2,per_query:5})
 await api.planSearch('plan-test','search-old')
 assert.equal(calls[3].params.run_id,'search-old')
 await api.cvprSearch({query:'depth',start_year:2024,end_year:2024,verified_main_only:true,page_size:1})
 assert.equal(calls[4].url,'/venues/cvpr/papers');assert.equal(calls[4].params.verified_main_only,true)
 assert.equal(calls[4].timeout,240000)
 await api.startPlanSearch('plan-test',2,5,true)
 assert.equal(JSON.parse(calls[5].data).refresh,true)
 await api.startAbstractTranslation('Paper','Original abstract.\nAccuracy 2.5%.')
 assert.equal(calls[6].url,'/translations/abstract')
 assert.deepEqual(JSON.parse(calls[6].data),{title:'Paper',abstract:'Original abstract.\nAccuracy 2.5%.'})
 await api.lookupAbstractTranslation('Paper','Original abstract.')
 assert.equal(calls[7].url,'/translations/abstract/lookup')
 await api.venueSearch({query:'depth',venues:'cvpr,iccv',page_size:5})
 assert.equal(calls[8].url,'/venues/papers');assert.equal(calls[8].params.venues,'cvpr,iccv')
 await api.venues();assert.equal(calls[9].url,'/venues')
}finally{http.defaults.adapter=adapter;localStorage.removeItem('rp-data-mode')}
console.log('PASS 研究计划生成与带版本号的保存请求')

localStorage.setItem('rp-data-mode','api')
try{
 const calls=[]
 http.defaults.adapter=async config=>{calls.push(config);return{data:{task_id:'exec-task',execution_id:'exec-1'},status:202,statusText:'Accepted',headers:{},config}}
 await api.startPlanExecution('plan-x',{expected_revision:3,search_run_id:'search-x',candidate_ids:['C1','C2']})
 assert.equal(calls[0].url,'/agent/plans/plan-x/executions')
 assert.deepEqual(JSON.parse(calls[0].data),{expected_revision:3,search_run_id:'search-x',candidate_ids:['C1','C2']})
 await api.planExecutions('plan-x','search-x');assert.equal(calls[1].params.search_run_id,'search-x')
 await api.resumePlanExecution('plan-x','exec-1');assert.equal(calls[2].url,'/agent/plans/plan-x/executions/exec-1/resume')
}finally{http.defaults.adapter=adapter;localStorage.removeItem('rp-data-mode')}
console.log('PASS 全文分析执行请求只提交计划版本、检索记录和候选编号；恢复固定执行记录')
