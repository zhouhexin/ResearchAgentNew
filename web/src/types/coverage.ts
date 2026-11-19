import type {TaskInfo} from './research'
export interface CoverageCitation {ref_id:string;paper_id:number;paper_title:string;page_number:number|null;source_spans:{span_id:string;start_char:number;end_char:number;text:string}[]}
export interface CoverageAssessment {question_id:string;question:string;status:'answered'|'partial'|'unanswered'|'conflicting';answer:string;reason:string;gaps:string[];citations:CoverageCitation[];suggestions:{action:'read_existing'|'search_later';paper_ids:number[];target:string;rationale:string}[]}
export interface CoverageResult {id:string;is_current:boolean;request_id:string;analysis_ids:string[];review_ids:string[];reading_coverage:{paper_id:number;title:string;reading_scope:string;selected_chunks:number;total_chunks:number}[];report:{questions:CoverageAssessment[];limitations:string[];human_review_status:string;suggestions_executed:false}}
export interface CoverageTask {request_id:string;task:TaskInfo}
