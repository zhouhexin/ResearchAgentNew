"""将已保存的实际综述问题交给Evolution；不从机器标签制造人工答案。"""
from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from api import report_checks
from evaluation.task_quality import Case, Dataset, Material, Permission, Requirement
from evaluation.report_protocol import register_binding
from storage.research_experiments import put


def handoff(report_id, result_ids, permission, requirements):
    permission = Permission.model_validate(permission)
    if not permission.allowed:
        raise RuntimeUnavailable('未获综述材料离线优化授权。')
    if not result_ids or len(result_ids) != len(set(result_ids)):
        raise ValueError('请选择不重复的实际审查或回放结果。')
    report = report_checks.source(report_id)
    issues = []
    source_fingerprints = []
    for identifier in result_ids:
        result = report_checks.detail(report_id, identifier)
        if not result['source_unchanged']:
            raise RuntimeUnavailable('问题来源的综述或材料已变化。')
        source_fingerprints.append({'id': identifier, 'sha256': fingerprint({k:v for k,v in result.items() if k!='source_unchanged'})})
        for item in result['items']:
            if item['status'] != 'succeeded':
                issues.append({'result_id': identifier, 'unit': item['unit'], 'status': item['status'], 'error': item['error']})
            elif result['check_kind'] == 'independent_review':
                assessment = item['result']
                if assessment['review']['verdict'] != 'supported' or assessment['diagnostics']:
                    issues.append({'result_id': identifier, 'unit': item['unit'], 'status': 'needs_human_review',
                        'assessment': assessment, 'is_human_gold': False})
    if not issues:
        raise ValueError('所选记录没有实际失败或待复核问题，不制造优化任务。')
    request = report.get('synthesis_request')
    if not request or not report.get('runtime_bindings'):
        raise RuntimeUnavailable('原请求或配置未知，不能用问答协议替代原综述实验。')
    materials = []
    rows = {row['paper_id']: row for row in report['matrix']}
    for ref in request['allowed_refs']:
        pid = int(ref.split(':')[0][1:])
        value = report['evidence_index'][ref]
        materials.append(Material(id=ref, paper_key=f'P{pid}', document_sha256=rows[pid]['content_sha256'],
            text=value['text'], text_sha256=fingerprint(value['text']), page_number=value.get('page_number')))
    feedback = {'report_id':report_id, 'source_results':source_fingerprints, 'issues':issues,
                'semantic_human_gold':'not_collected', 'original_protocol':'review-synthesis-v1'}
    case = Case(id='report-'+fingerprint(report_id)[:20], revision='1', group_id='report-'+fingerprint(report['analysis_ids'])[:20],
        split='development', kind='natural', topic=report['topic'], task_type='literature_review',
        question=request['payload']['review_focus'], scope='仅冻结综述原请求实际提供的分析与引文；完整原文用于独立辅助核验。',
        permission=permission, materials=materials, requirements=[Requirement.model_validate(v) for v in requirements],
        review_request=request, source_feedback=feedback)
    dataset = Dataset(id='report-development-'+fingerprint(report_id)[:20], revision='1',
                      annotation_version='requirements-only-no-human-gold-v1', cases=[case])
    binding_id = put('task_replay_binding', register_binding(report))
    dataset_id = put('task_dataset', dataset.model_dump())
    value = {'report_id':report_id, 'report_sha256':fingerprint(report), 'source_results':source_fingerprints,
        'dataset_artifact_id':dataset_id, 'baseline_binding_id':binding_id, 'issues':issues,
        'permission':permission.model_dump(), 'production_decision':'retain_baseline', 'experiment_started':False,
        'human_review_completed':False, 'protocol':'review-synthesis-v1'}
    return {'artifact_id':put('report_evolution_handoff', value), **value}
