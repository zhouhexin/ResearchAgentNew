import type {ResearchPlan} from './planning'
import type {ReadingSelection,TaskInfo,TaskStatus} from './research'
export interface ExecutionStep {scope?:string;reading_selection?:ReadingSelection;status:TaskStatus|'pending';task_id?:string;document_id?:number;analysis_id?:string;reused?:boolean;error?:TaskInfo['error'];task?:TaskInfo|null}
export interface ResearchExecution {analysis_scope?:'full'|'fast';id:string;plan_id:string;revision:number;plan:ResearchPlan;search_run_id:string;task_id:string;status:TaskStatus;error:TaskInfo['error'];created_at:string;attempts:number;papers:{candidate_id:string;title:string;paper_id:number|null;fulltext:ExecutionStep;analysis:ExecutionStep;screening:{decision:string}}[]}
