import type {ResearchPlan} from './planning'
import type {SearchPaper,TaskInfo,TaskStatus} from './research'
export type ScreeningDecision='included'|'excluded'|'uncertain'
export interface CriterionCheck {criterion_id:string;verdict:'meets'|'fails'|'unknown';quote:string;explanation:string}
export interface Screening {decision:ScreeningDecision;checks:CriterionCheck[];basis:'title_abstract';abstract_truncated:boolean}
export interface SearchCandidate extends SearchPaper {candidate_id:string;query_numbers:number[]}
export interface ResearchSearchRun {id:string;plan_id:string;revision:number;goal:string;plan:ResearchPlan;per_query:number;task_id:string;status:TaskStatus;error:TaskInfo['error'];created_at:string;updated_at:string;attempts:number;model_name:string;notice:string;queries:{number:number;query:string;status:'pending'|'succeeded';items:SearchPaper[];total:number|null}[];candidates:SearchCandidate[];screenings:Record<string,Screening>;duplicate_count:number;skipped_count:number}
export interface ResearchSearchResponse {run:ResearchSearchRun|null;history:Pick<ResearchSearchRun,'id'|'revision'|'status'|'created_at'|'per_query'|'model_name'>[]}
