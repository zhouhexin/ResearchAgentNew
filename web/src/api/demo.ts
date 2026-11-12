import seed from '../data/seed.json'
import type {AnalysisResult,DocumentSummary,EvidenceRecord,Health,PageResult,PaperSummary,ReviewResult,SearchPaper,TaskInfo} from '../types/research'
import {readStorage} from './utils'
import {ApiFailure} from './http'
const analyses=seed.analyses as unknown as AnalysisResult[]
const basePapers:PaperSummary[]=analyses.map(a=>({id:a.paper.id,title:a.paper.title,doi:a.paper.doi,authors:[],year:null,openalex_id:null,citation_count:0,has_fulltext:true,has_analysis:true,abstract:a.analysis.research_questions.map(x=>x.statement).join(' ')}))
let papers=readStorage<PaperSummary[]>('rp-demo-papers-v1',basePapers)
let tasks=readStorage<TaskInfo[]>('rp-demo-tasks-v1',[])
const review=seed.review as ReviewResult['draft']
const save=()=>{localStorage.setItem('rp-demo-papers-v1',JSON.stringify(papers));localStorage.setItem('rp-demo-tasks-v1',JSON.stringify(tasks))}
function page<T>(items:T[],params:Record<string,unknown>):PageResult<T>{const n=Number(params.page||1),size=Number(params.page_size||20);return{items:items.slice((n-1)*size,n*size),total:items.length,page:n,page_size:size}}
const docs:DocumentSummary[]=analyses.map(a=>({id:a.document.id,paper_id:a.paper.id,source_url:'本地导入 PDF',source_type:'pdf',parser:'pymupdf',page_count:null,content_sha256:a.document.content_sha256,chunk_count:a.document.total_chunks,pdf_file_url:null}))
const evidence:EvidenceRecord[]=analyses.flatMap(a=>Object.entries(a.evidence_index).map(([id,s])=>({evidence_id:id,paper_id:a.paper.id,paper_title:a.paper.title,document_id:a.document.id,source_url:'本地导入 PDF',page_number:s.page_number,section_hint:s.section_hint||null,quote:s.text,start_char:0,end_char:s.text.length,verification:'unverified' as const})))
function createTask(type:TaskInfo['type'],result:string){const t:TaskInfo={id:'demo_'+crypto.randomUUID(),type,status:'queued',progress:0,stage:'等待演示任务',result_id:result,error:null,created_at:new Date().toISOString(),updated_at:new Date().toISOString()};tasks.unshift(t);save();return{task_id:t.id}}
export async function demoRequest<T>(method:string,path:string,body?:unknown,params:Record<string,unknown>={}):Promise<T>{await new Promise(r=>setTimeout(r,180));const data=body as any;let result:any;
if(path==='/system/health')result={api:true,database:false,model_configured:false,model_name:'未连接模型 · 演示模式',stats:{papers:papers.length,documents:docs.length,analyses:analyses.length,reviews:1}} satisfies Health;
else if(path==='/system/model-check'&&method==='GET')result=null;
else if(path==='/system/model-check')throw new ApiFailure('MODEL_NOT_CONNECTED','请切换到真实 API 模式后检测模型连接。');
else if(path==='/reviews/questions')throw new ApiFailure('MODEL_NOT_CONNECTED','演示模式不调用模型推荐研究问题，请切换到真实 API 模式。');
else if(path.startsWith('/translations/abstract'))throw new ApiFailure('MODEL_NOT_CONNECTED','中文翻译需要真实 API 模式。');
else if(path.startsWith('/venues')||path.endsWith('/publication'))throw new ApiFailure('MODEL_NOT_CONNECTED','CVPR 检索和发表核验需要真实 API 模式。');
else if(path.startsWith('/agent/plans'))throw new ApiFailure('MODEL_NOT_CONNECTED','演示模式不生成或保存研究计划，请切换到真实 API 模式。');
else if(path==='/papers/search'){
let list:SearchPaper[]=[{source:'arxiv',arxiv_id:'1706.03762',title:'Attention Is All You Need',authors:['Ashish Vaswani','Noam Shazeer'],year:2017,doi:'10.48550/arxiv.1706.03762',openalex_id:null,url:'https://arxiv.org/abs/1706.03762',abstract:'演示记录：实时论文及完整摘要请通过真实 API 检索。',citation_count:null,identity_verified:true,saved_paper_id:null}];
list=list.filter(p=>!params.q||(p.title+' '+p.abstract).toLowerCase().includes(String(params.q).toLowerCase()));
if(params.start_year)list=list.filter(p=>p.year!==null&&p.year>=Number(params.start_year));if(params.end_year)list=list.filter(p=>p.year!==null&&p.year<=Number(params.end_year));
list=list.map(p=>({...p,saved_paper_id:papers.find(x=>x.doi===p.doi)?.id||null}));result=page(list,params)}
else if(path==='/papers/batch'){let created=0,existing=0,rejected=0;for(const p of data.papers as SearchPaper[]){if(!p.identity_verified||!(p.doi||p.openalex_id)){rejected++;continue}if(papers.some(x=>(p.doi&&x.doi===p.doi)||(p.openalex_id&&x.openalex_id===p.openalex_id))){existing++;continue}papers.push({...p,citation_count:p.citation_count??0,id:Math.max(0,...papers.map(x=>x.id))+1,has_fulltext:false,has_analysis:false});created++}save();result={created,existing,rejected}}
else if(path==='/papers'){let list=papers.filter(p=>!params.q||(p.title+' '+p.doi).toLowerCase().includes(String(params.q).toLowerCase()));if(params.has_fulltext!==undefined&&params.has_fulltext!=='')list=list.filter(p=>p.has_fulltext===Boolean(params.has_fulltext));if(params.has_analysis!==undefined&&params.has_analysis!=='')list=list.filter(p=>p.has_analysis===Boolean(params.has_analysis));if(params.year)list=list.filter(p=>p.year===Number(params.year));result=page(list,params)}
else if(/^\/papers\/\d+\/documents$/.test(path)&&method==='GET')result=docs.filter(x=>x.paper_id===Number(path.split('/')[2]));
else if(/^\/papers\/\d+$/.test(path))result=papers.find(p=>p.id===Number(path.split('/')[2]));
else if(/^\/documents\/\d+\/chunks$/.test(path))result=page(evidence.filter(e=>e.document_id===Number(path.split('/')[2])),params);
else if(path.startsWith('/evidence/'))result=evidence.find(e=>e.evidence_id===path.split('/')[2]);
else if(path==='/analyses'&&method==='GET')result=analyses.filter(a=>!params.paper_id||a.paper.id===Number(params.paper_id));
else if(path==='/analyses'&&method==='POST'){const a=analyses.find(a=>a.paper.id===data.paper_id);if(!a)throw new ApiFailure('DEMO_UNAVAILABLE','此论文没有演示分析。请连接后端生成真实结果。');result=createTask('paper_analysis',a.id)}
else if(path.startsWith('/analyses/progress/'))result=null;
else if(path.startsWith('/analyses/'))result=analyses.find(a=>a.id===path.split('/')[2]);
else if(path==='/reviews/compare'&&method==='POST'){if(data.paper_ids.length<2||data.paper_ids.length>5||new Set(data.paper_ids).size!==data.paper_ids.length)throw new ApiFailure('INVALID_SELECTION','请选择 2–5 篇不同论文');const chosen=data.paper_ids.map((id:number)=>analyses.find(a=>a.paper.id===id));if(chosen.some((a:AnalysisResult|undefined)=>!a))throw new ApiFailure('ANALYSIS_REQUIRED','所选论文需要有效分析');result={paper_ids:data.paper_ids,analysis_ids:chosen.map((a:AnalysisResult)=>a.id),matrix:chosen.map((a:AnalysisResult)=>({paper_id:a.paper.id,title:a.paper.title,analysis_id:a.id,analysis:a.analysis}))}}
else if(path==='/reviews'&&method==='POST'){if(data.paper_ids.length<2||data.paper_ids.length>5||new Set(data.paper_ids).size!==data.paper_ids.length)throw new ApiFailure('INVALID_SELECTION','请选择 2–5 篇不同论文');if(data.paper_ids.some((id:number)=>!analyses.some(a=>a.paper.id===id)))throw new ApiFailure('ANALYSIS_REQUIRED','所选论文需要有效分析');if(data.topic&&!/单光子|深度|姿态|三维/.test(data.topic))throw new ApiFailure('DEMO_TOPIC','当前演示报告仅支持单光子三维成像相关主题，请更换主题或连接真实 API。');result=createTask('review_generation','review_demo')}
else if(path==='/reviews/review_demo')result={id:'review_demo',paper_ids:[2,3],topic:'单光子三维成像',draft:review,matrix:analyses.map(a=>({paper_id:a.paper.id,title:a.paper.title,analysis:a.analysis}))} satisfies ReviewResult;
else if(path==='/tasks'){for(const t of tasks)update(t);result=page(tasks.filter(t=>!params.status||t.status===params.status),params)}
else if(path.startsWith('/tasks/')){const t=tasks.find(t=>t.id===path.split('/')[2]);if(t)update(t);result=t}
else if(path==='/agent/chat')throw new ApiFailure('MODEL_NOT_CONNECTED','演示模式不生成模型回答。请切换到 API 模式并连接后端；已保留你的问题。');
else if(method==='POST'&&path.endsWith('/documents/fetch'))throw new ApiFailure('DOWNLOAD_NOT_CONNECTED','演示模式不下载论文。请在设置中切换到真实 API。');
else if(method==='POST'&&path.includes('/documents'))throw new ApiFailure('PARSER_NOT_CONNECTED','演示模式支持本地预览，不执行解析或入库。请连接后端后上传。');
else throw new ApiFailure('DEMO_UNAVAILABLE','此操作需要真实后端服务');
if(result===undefined)throw new ApiFailure('NOT_FOUND','未找到对应记录',404);return result as T}
function update(t:TaskInfo){if(['succeeded','failed','cancelled'].includes(t.status))return;const elapsed=Date.now()-Date.parse(t.created_at);t.status=elapsed>2500?'succeeded':'running';t.progress=elapsed>2500?100:40;t.stage=elapsed>2500?'演示结果已加载':'正在加载已导出的本地示例';t.updated_at=new Date().toISOString();save()}
