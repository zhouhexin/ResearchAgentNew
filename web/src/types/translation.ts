import type {TaskInfo} from './research'
export interface AbstractTranslationRecord {id:string;title:string;original_abstract:string;source_sha256:string;translation_zh:string|null;model_name:string;task_id:string;created_at:string;completed_at:string|null}
export interface AbstractTranslationResponse {record:AbstractTranslationRecord;task:TaskInfo|null}
