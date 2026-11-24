"""让离线演化执行真实综述协议，不将综述案例降成问答任务。"""
import json

from agent.runtime_snapshot import RuntimeUnavailable
from evaluation.task_quality import Citation, Finding
from review.synthesis import (ReviewDraft, SECTIONS, check_review_request, parse_review_response,
                              validate_review)


def check_feedback(value):
    """反馈仅为机器运行记录；独立人工答案不能借扩展字段进入控制器。"""
    if value is None:
        return
    if (set(value) != {'report_id', 'source_results', 'issues', 'semantic_human_gold', 'original_protocol'}
            or value['semantic_human_gold'] != 'not_collected'
            or value['original_protocol'] != 'review-synthesis-v1'
            or not isinstance(value['report_id'], str) or not isinstance(value['source_results'], list)
            or not isinstance(value['issues'], list)):
        raise ValueError('综述来源反馈必须是固定机器运行记录，不包含人工答案。')
    forbidden = {'requirements', 'human_judgments', 'gold_label', 'gold_reason', 'human_gold',
                 'human_labels', 'answer_key', 'human_answer'}
    def visit(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key.lower() in forbidden or key == 'is_human_gold' and item is not False:
                    raise ValueError('综述来源反馈不能包含人工要求、答案或标签。')
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)
    visit(value)


def check_case(case):
    request = case.review_request
    check_review_request(request)
    materials = {m.id: m for m in case.materials}
    if set(request['allowed_refs']) - materials.keys():
        raise ValueError('原综述引用缺少冻结原文。')
    for paper in request['payload']['papers']:
        for findings in paper['findings'].values():
            for finding in findings:
                for evidence in finding['evidence']:
                    ref = evidence['ref']
                    if (not ref.startswith(f"P{paper['paper_id']}:E") or ref not in materials
                            or materials[ref].paper_key != f"P{paper['paper_id']}"
                            or not evidence['quote'].strip() or evidence['quote'] not in materials[ref].text):
                        raise ValueError('原综述请求引句与论文归属不符。')


def check_binding(case, version):
    request = getattr(case, 'review_request', None)
    if version.quote_protocol not in {'review-synthesis-v1', 'task-replay-pointers-v1'}:
        raise RuntimeUnavailable('未知的离线执行协议。')
    native = version.quote_protocol == 'review-synthesis-v1'
    if native != (request is not None):
        raise RuntimeUnavailable('案例与绑定的综述/问答协议不符，不能相互替代。')
    if native and json.loads(version.tools_json).get('source_request_sha256') != request['sha256']:
        raise RuntimeUnavailable('案例不属于绑定的原综述请求。')


def execution_request(case, strategy):
    check_case(case)
    from evaluation.harness_contracts import skill_guidance
    prompt = case.review_request['prompt']
    guidance = skill_guidance(strategy.skills)
    if strategy.instructions:guidance += '\n' + strategy.instructions
    if strategy.profile != 'standard':
        guidance += '\n' + {'conditions_first':'先核对实验条件与适用边界。',
                            'contradictions_first':'先核对相反证据与条件冲突。'}[strategy.profile]
    if guidance.strip():prompt += '\n本次离线候选通用写作规则：\n' + guidance
    return prompt, case.review_request['payload'], ReviewDraft, case.review_request['method']


def materialize_case(response, case):
    check_case(case)
    draft = parse_review_response(response, case.review_request['method'])
    validate_review(draft, set(case.review_request['allowed_refs']))
    materials = {m.id: m for m in case.materials}
    quotes = {}
    for paper in case.review_request['payload']['papers']:
        for values in paper['findings'].values():
            for finding in values:
                for evidence in finding['evidence']:
                    if evidence['quote'] not in quotes.setdefault(evidence['ref'], []):
                        quotes[evidence['ref']].append(evidence['quote'])
    findings = []
    for section in SECTIONS:
        for point in getattr(draft, section):
            for text, refs in [(point.claim, point.evidence_refs)] + [(p.text, p.evidence_refs) for p in point.paragraphs]:
                citations = []
                for ref in refs:
                    for quote in quotes[ref]:
                        start = materials[ref].text.index(quote)
                        citations.append(Citation(material_id=ref, quote=quote, start_char=start, end_char=start+len(quote)))
                findings.append(Finding(statement=text, citations=citations))
    return json.dumps(draft.model_dump(), ensure_ascii=False), findings


def register_binding(report):
    from agent.agent_version import AgentVersion
    from agent.runtime_snapshot import fingerprint
    from api import provenance, runtime_bindings
    from evaluation.task_replay import Strategy, contract
    from storage.runtime_versions import save_immutable
    binding = runtime_bindings.restore(report['runtime_bindings'], 'review')
    check_review_request(report['synthesis_request'])
    strategy = Strategy(max_output_tokens=binding.settings['output_tokens'])
    version = AgentVersion.capture(role='analysis', contracts=contract(), quote_protocol='review-synthesis-v1',
        tools={'strategy':strategy.model_dump(), 'mode':'frozen', 'source_agent_version_id':binding.version.id,
               'source_request_sha256':report['synthesis_request']['sha256']})
    provenance.transaction(lambda session:save_immutable(session, version))
    return {'version':version.model_dump(), 'runtime':binding.runtime.model_dump(),
            'agent_version_id':version.id, 'runtime_snapshot_id':binding.runtime.id}
