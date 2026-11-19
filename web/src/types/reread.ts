import type {TaskInfo} from './research'
export interface RereadCandidate {evidence_id:string;page_number:number|null;section_hint:string|null;already_read:boolean;lexical_matches:number;characters:number;text:string}
export interface RereadPreview {id:string;paper_id:number;analysis_id:string;document_id:number;candidates:RereadCandidate[];total_chunks:number;per_batch_max_chars:number;focus:{question:string;target:string}}
export interface RereadTask {request_id:string;task:TaskInfo;budget:{calls_used:number;max_calls:number;deadline_epoch:number}|null}
