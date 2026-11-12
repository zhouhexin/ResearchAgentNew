import {http} from './http'
import type {ResearchExecution} from '../types/execution'
import {demoRequest} from './demo'
import {cleanParams} from './utils'
import type {ResearchPlan,ResearchPlanRecord} from '../types/planning'
import type {ResearchSearchResponse} from '../types/research-search'
import type {AbstractTranslationResponse} from '../types/translation'
import type {AnalysisResult,DocumentSummary,EvidenceRecord,Health,PageResult,PaperSummary,ReviewResult,ReviewComparison,SearchPaper,VenueSummary,TaskInfo,HttpBackend,ModelCheckMode} from '../types/research'
export const mode=()=>localStorage.getItem('rp-data-mode')||import.meta.env.VITE_DATA_MODE||'demo'
async function request<T>(method:string,path:string,data?:unknown,params?:Record<string,unknown>):Promise<T>{const cleanedParams=cleanParams(params);if(mode()==='demo')return demoRequest<T>(method,path,data,cleanedParams);return(await http.request<T>({method,url:path,data,params:cleanedParams,...(data instanceof FormData?{timeout:120000}:path.startsWith('/venues/')?{timeout:240000}:path==='/agent/chat'?{timeout:600000}:{})})).data}
export const api={
 lookupAbstractTranslation:(title:string,abstract:string)=>request<AbstractTranslationResponse|null>('POST','/translations/abstract/lookup',{title,abstract}),
 startAbstractTranslation:(title:string,abstract:string)=>request<{task_id:string;translation_id:string;reused:boolean}>('POST','/translations/abstract',{title,abstract}),
 abstractTranslation:(id:string)=>request<AbstractTranslationResponse>('GET',`/translations/abstract/${encodeURIComponent(id)}`),
 startResearchPlan:(goal:string)=>request<{task_id:string;reused?:boolean}>('POST','/agent/plans',{goal}),
 researchPlan:(id:string)=>request<ResearchPlanRecord>('GET',`/agent/plans/${encodeURIComponent(id)}`),
 saveResearchPlan:(id:string,plan:ResearchPlan,expected_revision:number)=>request<ResearchPlanRecord>('PUT',`/agent/plans/${encodeURIComponent(id)}`,{plan,expected_revision}),
 startPlanSearch:(id:string,expected_revision:number,per_query:number,refresh=false)=>request<{task_id:string;run_id:string;reused:boolean}>('POST',`/agent/plans/${encodeURIComponent(id)}/search`,{expected_revision,per_query,...(refresh?{refresh:true}:{})}),
 planSearch:(id:string,run_id?:string)=>request<ResearchSearchResponse>('GET',`/agent/plans/${encodeURIComponent(id)}/search`,undefined,{run_id}),
 startPlanExecution:(id:string,body:{expected_revision:number;search_run_id:string;candidate_ids:string[];analysis_scope?:'full'|'fast'})=>request<{task_id:string;execution_id:string;reused:boolean}>('POST',`/agent/plans/${encodeURIComponent(id)}/executions`,body),
 resumePlanExecution:(id:string,execution_id:string)=>request<{task_id:string;execution_id:string;reused:boolean}>('POST',`/agent/plans/${encodeURIComponent(id)}/executions/${encodeURIComponent(execution_id)}/resume`),
 planExecutions:(id:string,search_run_id?:string)=>request<{items:ResearchExecution[]}>('GET',`/agent/plans/${encodeURIComponent(id)}/executions`,undefined,{search_run_id}),
 planComparison:(id:string,execution:string)=>request<ReviewComparison>('GET',`/agent/plans/${encodeURIComponent(id)}/executions/${encodeURIComponent(execution)}/comparison`),
 startPlanReport:(id:string,execution:string,body:{research_question:string;comparison_dimensions:string[]})=>request<{task_id:string;report_id:string;reused:boolean}>('POST',`/agent/plans/${encodeURIComponent(id)}/executions/${encodeURIComponent(execution)}/reports`,body),
 planReports:(id:string,execution:string)=>request<{items:unknown[]}>('GET',`/agent/plans/${encodeURIComponent(id)}/executions/${encodeURIComponent(execution)}/reports`),
 health:()=>request<Health>('GET','/system/health'),
 latestModelCheck:(p:{mode:ModelCheckMode;http_backend?:HttpBackend})=>request<TaskInfo|null>('GET','/system/model-check',undefined,p),
 startModelCheck:(body:{mode:ModelCheckMode;http_backend?:HttpBackend;timeout?:number})=>request<{task_id:string;reused?:boolean}>('POST','/system/model-check',body),
 search:(p:Record<string,unknown>)=>request<PageResult<SearchPaper>>('GET','/papers/search',undefined,p),
 venues:()=>request<{items:VenueSummary[]}>('GET','/venues'),
 venueSearch:(p:Record<string,unknown>)=>request<PageResult<SearchPaper>&{notice:string}>('GET','/venues/papers',undefined,p),
 cvprSearch:(p:Record<string,unknown>)=>request<PageResult<SearchPaper>&{notice:string}>('GET','/venues/cvpr/papers',undefined,p),
 paperPublication:(id:number)=>request<{items:(import('../types/research').PublicationInfo&{year:number;venue:{slug:string;name:string}})[]}>('GET',`/papers/${id}/publication`),
 savePapers:(papers:SearchPaper[])=>request<{created:number;existing:number;rejected:number}>('POST','/papers/batch',{papers}),
 papers:(p:Record<string,unknown>={})=>request<PageResult<PaperSummary>>('GET','/papers',undefined,p),
 paper:(id:number)=>request<PaperSummary>('GET',`/papers/${id}`),documents:(id:number)=>request<DocumentSummary[]>('GET',`/papers/${id}/documents`),
 chunks:(id:number,p:Record<string,unknown>)=>request<PageResult<EvidenceRecord>>('GET',`/documents/${id}/chunks`,undefined,p),
 evidence:(id:string)=>request<EvidenceRecord>('GET',`/evidence/${encodeURIComponent(id)}`),
 upload:(id:number,file:File)=>{const f=new FormData();f.append('file',file);f.append('confirmed','true');return request<{task_id:string}>('POST',`/papers/${id}/documents`,f)},
 fetchFulltext:(id:number)=>request<{task_id:string}>('POST',`/papers/${id}/documents/fetch`),
 importUrl:(id:number,url:string)=>request<{task_id:string}>('POST',`/papers/${id}/documents/from-url`,{url}),
 analyses:(id:number)=>request<AnalysisResult[]>('GET','/analyses',undefined,{paper_id:id}),analysis:(id:string)=>request<AnalysisResult>('GET',`/analyses/${encodeURIComponent(id)}`),
 analysisProgress:(id:number)=>request<TaskInfo|null>('GET',`/analyses/progress/${id}`),
 startAnalysis:(id:number,max_chunks:number,options:Record<string,unknown>={})=>request<{task_id:string;reused?:boolean;source_reparsed?:boolean}>('POST','/analyses',{paper_id:id,max_chunks,...options}),
 comparePapers:(paper_ids:number[],analysis_ids?:string[])=>request<ReviewComparison>('POST','/reviews/compare',{paper_ids,...(analysis_ids?{analysis_ids}:{})}),
 suggestReviewQuestions:(paper_ids:number[],analysis_ids:string[])=>request<{task_id:string;reused?:boolean}>('POST','/reviews/questions',{paper_ids,analysis_ids}),
 startReview:(paper_ids:number[],topic:string,analysis_ids?:string[])=>request<{task_id:string;reused?:boolean}>('POST','/reviews',{paper_ids,topic,analysis_ids}),review:(id:string)=>request<ReviewResult>('GET',`/reviews/${encodeURIComponent(id)}`),
 tasks:(p:Record<string,unknown>={})=>request<PageResult<TaskInfo>>('GET','/tasks',undefined,p),task:(id:string)=>request<TaskInfo>('GET',`/tasks/${encodeURIComponent(id)}`),
 chat:(message:string,session_id:string|null)=>request<{session_id:string;answer:string;citations:string[];tools_used:{name:string;status:string}[];citation_status:string}>('POST','/agent/chat',{message,session_id}),
 reviewFileUrl:(id:string,format:string)=>`${import.meta.env.VITE_API_BASE_URL||'/api/v1'}/reviews/${encodeURIComponent(id)}/files/${format}`,
}
