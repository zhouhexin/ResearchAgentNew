"""只在显式授权及人工要求齐备时，从固定有效分析导出任务材料。"""
from pydantic import Field
from agent.runtime_snapshot import fingerprint
from api.analysis_results import load_result
from api.research_execution import verify_library_analysis
from evaluation.task_quality import Strict, Case, Dataset, Material, Permission, Requirement
from storage.research_experiments import put


class ExportCase(Strict):
    id:str
    revision:str
    group_id:str
    split:str
    topic:str
    task_type:str
    question:str
    scope:str
    analysis_ids:list[str]=Field(min_length=1,max_length=5)
    permission:Permission
    requirements:list[Requirement]=Field(min_length=1)


def export_dataset(identifier,revision,annotation_version,specs):
    cases=[];sources=[]
    for spec in specs:
        if not spec.permission.allowed:raise ValueError('未授权材料不导出。')
        materials=[]
        if len(set(spec.analysis_ids))!=len(spec.analysis_ids):raise ValueError('分析ID重复。')
        for key in spec.analysis_ids:
            snapshot=load_result(key)
            if snapshot is None:raise ValueError('分析不存在。')
            verify_library_analysis(snapshot)
            for eid,value in snapshot['evidence_index'].items():
                materials.append(Material(id=f"P{snapshot['paper']['id']}:{eid}",paper_key=f"P{snapshot['paper']['id']}",
                    document_sha256=snapshot['document']['content_sha256'],text=value['text'],text_sha256=fingerprint(value['text']),page_number=value.get('page_number')))
            sources.append({'case_id':spec.id,'analysis_id':key,'analysis_sha256':fingerprint(snapshot),
                'document_id':snapshot['document']['id'],'document_sha256':snapshot['document']['content_sha256'],
                'reading_scope':snapshot.get('batch_summary',{}).get('scope','unknown'),
                'selected_evidence_ids':snapshot.get('selected_evidence_ids'),'exported_scope':'cited_chunks_only'})
        cases.append(Case(**{k:v for k,v in spec.model_dump().items() if k!='analysis_ids'},kind='natural',materials=materials))
    dataset=Dataset(id=identifier,revision=revision,annotation_version=annotation_version,cases=cases)
    dataset_id=put('task_dataset',dataset.model_dump())
    provenance_id=put('task_dataset_sources',{'dataset_artifact_id':dataset_id,'sources':sources})
    return {'dataset_artifact_id':dataset_id,'source_artifact_id':provenance_id,'dataset_sha256':dataset.sha256}
