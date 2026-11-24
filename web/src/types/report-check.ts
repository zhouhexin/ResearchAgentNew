import type {TaskInfo} from './research'
export type CheckKind='independent_review'|'original_replay'
export interface ReportCheckTask extends Omit<TaskInfo,'type'> {type:'report_check';check_kind:CheckKind;report_check_request_id:string}
export interface ReportCheckResult {
 id:string;report_id:string;check_kind:CheckKind;complete:boolean;source_unchanged:boolean;denominator:number;failed:number
 stop_error?:{code:string;message:string;retryable?:boolean}|null
 items:{status:string;unit:{unit_id:string;statement?:string};error?:{code:string;message:string}|null;result?:{
  review?:{verdict:string;reason:string};diagnostics?:string[];same_draft?:boolean
  citations?:{ref_id:string;page_number:number|null;start_char:number;end_char:number;text:string}[]
 }|null}[]
}
