import type {TaskInfo} from './research'
export type ClaimVerdict='supported'|'contradicted'|'insufficient'|'needs_review'
export const claimVerdicts:Record<ClaimVerdict,string>={supported:'支持',contradicted:'矛盾',insufficient:'证据不足',needs_review:'需要人工核对'}
export interface ClaimReviewItem {
 sample_id:string;category:string;finding_index:number;statement:string
 evidence:{evidence_id:string;page_number:number;quote:string;context:string}[]
 status:'not_attempted'|'succeeded'|'failed'|'precheck_failed'
 review:{item_id:string;verdict:ClaimVerdict;reason:string;evidence_assessments:{evidence_id:string;relation:string;reason:string;quoted_spans:string[]}[]}|null
 error:{code:string;message:string}|null
 quote_protocol?:'references-v2'|'verbatim-v1'|'legacy-import';legacy_source_review_id?:string|null
}
export interface ClaimReviewRecord {
 id:string;analysis_id:string;paper_id:number;document_id:number;content_sha256:string
 created_at:string;model_name:string;policy_version:string;context_policy:string;is_current:boolean
 task:TaskInfo;items:ClaimReviewItem[];total:number;completed:number
 quote_protocol?:'references-v2'|'verbatim-v1';legacy_reused?:number
}
