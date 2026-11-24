"""离线来源核验、可恢复语义评审及独立人工标签对照。"""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from time import monotonic

import fitz
from openai import APIConnectionError, APIStatusError, APITimeoutError

from agent.claim_verifier import (CLAIM_REVIEW_PROMPT, POLICY_VERSION, VERDICTS, ClaimReview,
                                  ReviewError, ReviewPrecheckError, prepare_claim, review_claim,
                                  restore_review_quotes, validate_review_output, layout_candidates, confirm_layout_quotes)
from agent.codex_model import CodexExecutionError
from agent.claim_reference_verifier import (QUOTE_PROTOCOL, SPAN_POLICY, SCHEMA_POLICY, REFERENCE_REVIEW_PROMPT,
    ReferenceClaimReview, reference_payload, response_schema, materialize_review, review_claim_references)


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def atomic_json(path, value):
    path = Path(path)
    # Windows 的失败归档含完整内容哈希，目标路径可能超过 MAX_PATH。
    # 使用同一绝对目标的长路径形式，保留原目录、原文件名和历史协议。
    if os.name == 'nt':
        address = str(path.absolute())
        if not address.startswith('\\\\?\\'):
            path = Path('\\\\?\\UNC\\' + address[2:] if address.startswith('\\\\') else '\\\\?\\' + address)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.claim-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def inside(folder, name):
    path = (folder / name).resolve()
    if not path.is_relative_to(folder):
        raise ReviewPrecheckError('来源路径超出评测材料目录')
    return path


def require(condition, message):
    if not condition:
        raise ReviewPrecheckError(message)


def load_dataset(folder, sample_ids=None, *, split=None, max_input_chars=24000):
    """不执行材料包内的 Python；独立检查 PDF、文本快照和本次送评的全部引句。"""
    folder = Path(folder).resolve()
    manifest = json.loads((folder / 'manifest.json').read_text())
    inputs = read_jsonl(folder / 'reviewer_inputs.jsonl')
    require(fingerprint(inputs) == manifest['reviewer_inputs_sha256'], '模型输入文件与冻结版本不一致')
    require(len(inputs) == manifest['samples'], '材料数量与冻结版本不一致')
    sources = json.loads((folder / 'source_snapshots.json').read_text())
    ids = [item['sample_id'] for item in inputs]
    require(len(set(ids)) == len(ids), '材料编号重复')
    require(not (sample_ids is not None and split is not None), '不能同时指定样本编号和集合')
    if split is not None:
        require(split in {'development', 'validation_reserved'}, '未知评测集合')
        paper_keys = {source['paper_key'] for source in manifest['sources'] if source['split'] == split}
        selected = [item['sample_id'] for item in inputs if item['paper_key'] in paper_keys]
    else:
        selected = list(sample_ids) if sample_ids is not None else list(manifest['pilot_ids'])
    require(bool(selected) and len(set(selected)) == len(selected) and set(selected) <= set(ids), '送评编号为空、重复或不存在')
    index = {item['sample_id']: item for item in inputs}
    checked_sources = set()
    for key in selected:
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', key) is not None, '非法样本编号')
        item = index[key]
        require(set(item) == {'sample_id', 'paper_key', 'paper_title', 'statement', 'evidence', 'input_hash'}, '模型输入含额外字段')
        payload = {field: item[field] for field in item if field != 'input_hash'}
        require(fingerprint(payload) == item['input_hash'], '结论或证据输入哈希不一致')
        source = sources[item['paper_key']]
        fixed = next((value for value in manifest['sources'] if value['paper_key'] == item['paper_key']), None)
        require(fixed is not None and all(source.get(field) == value for field, value in fixed.items()), '来源与冻结清单不一致')
        require(item['paper_title'] == source['title'], '论文标题与来源不一致')
        if item['paper_key'] not in checked_sources:
            pdf_path = inside(folder, source['pdf_file'])
            require(sha256(pdf_path.read_bytes()).hexdigest() == source['pdf_sha256'], 'PDF 版本已改变')
            require(sha256(source['text'].encode()).hexdigest() == source['content_sha256'], '文本版本已改变')
            with fitz.open(pdf_path) as pdf:
                require(len(pdf) == source['page_count'], 'PDF 页数已改变')
                pages = {str(i + 1): page.get_text().replace('\x00', '\ufffd').strip() for i, page in enumerate(pdf)}
                pages = {number: text for number, text in pages.items() if text}
            require(pages == {number: page['text'] for number, page in source['pages'].items()}, '逐页文本与 PDF 不一致')
            require('\n\n'.join(pages.values()) == source['text'], '全文快照与 PDF 不一致')
            checked_sources.add(item['paper_key'])
        for evidence in item['evidence']:
            page_number = evidence['page_number']
            require(type(page_number) is int and str(page_number) in source['pages'], '引文页码无效')
            page = source['pages'][str(page_number)]
            begin, end = evidence['start_char'], evidence['end_char']
            require(type(begin) is int and type(end) is int and 0 <= begin < end <= len(source['text']), '引文字符范围无效')
            require(source['text'][begin:end] == evidence['quote'], '引句与实际原文不匹配')
            require(page['start_char'] <= begin < end <= page['end_char'], '引句超出记录页码')
            require(source['text'][page['start_char']:page['end_char']] == page['text'], '页面字符位置不一致')
            require(evidence['context'] == page['text'] and evidence['context_scope'] == 'entire_pdf_page', '上下文与对应页面不一致')
            require(evidence['context_start_char'] == page['start_char'] and evidence['context_end_char'] == page['end_char'], '上下文位置不一致')
            require(evidence['page_start_char'] == begin - page['start_char'] and evidence['page_end_char'] == end - page['start_char'], '页面内字符位置不一致')
            require(evidence['pdf_sha256'] == source['pdf_sha256'] and evidence['content_sha256'] == source['content_sha256'], '引文与来源版本不一致')
            expected_id = f"LOCAL:{source['pdf_sha256'][:12]}:P{page_number}:{begin-page['start_char']}-{end-page['start_char']}"
            require(evidence['evidence_id'] == expected_id and evidence['database_evidence_id'] is None, '离线证据编号与位置不一致')
            require(evidence['source_url'] == source['pdf_file'], '引文与 PDF 来源不一致')
        prepare_claim(item, max_input_chars=max_input_chars)
    return manifest, [index[key] for key in selected]


def load_annotations(path, manifest, items):
    """仅供调度器/评分器使用；该返回值不得进入模型消息。"""
    bundle = json.loads(Path(path).read_text())
    require(bundle['dataset_id'] == manifest['dataset_id'] and bundle['reviewer_inputs_sha256'] == manifest['reviewer_inputs_sha256'], '人工标注属于其他材料版本')
    selected = {item['sample_id']: item for item in items}
    result, seen = {}, set()
    for annotation in bundle['annotations']:
        key = annotation['sample_id']
        require(key not in seen, '人工标注编号重复')
        seen.add(key)
        if key not in selected:
            continue
        require(annotation['input_hash'] == selected[key]['input_hash'], '人工标注与送评材料不一致')
        require(annotation['annotation_status'] in {'reviewed', 'unreviewed', 'disputed'}, '人工标注状态无效')
        if annotation['annotation_status'] != 'reviewed':
            continue
        require(annotation['gold_label'] in VERDICTS and bool(annotation['gold_reason'].strip()), '人工标签或理由无效')
        require(bool(annotation['annotator'].strip()) and bool(annotation['annotated_at']), '人工标注人或时间缺失')
        result[key] = annotation
    return result


def error_info(exc):
    """保存错误类型和安全状态，不保存驱动/HTTP 异常原文。"""
    result = {'error_type': type(exc).__name__}
    from agent.runtime_snapshot import RuntimeUnavailable
    if isinstance(exc, RuntimeUnavailable):
        return dict(result, code=exc.code, message=str(exc)), True
    if isinstance(exc, CodexExecutionError):
        result.update(code=exc.code, message=str(exc), diagnostic=exc.diagnostic)
        return result, True
    if isinstance(exc, APITimeoutError):
        return dict(result, code='MODEL_TIMEOUT', message='语义评审模型请求超时，已保存结果可继续。'), True
    if isinstance(exc, APIConnectionError):
        return dict(result, code='MODEL_CONNECTION_ERROR', message='语义评审模型连接失败，尚未完成的项目保留。'), True
    if isinstance(exc, APIStatusError):
        return dict(result, code='MODEL_HTTP_ERROR', http_status=exc.status_code, message='模型服务返回 HTTP 错误。'), exc.status_code in {401, 403, 429} or exc.status_code >= 500
    if isinstance(exc, ReviewError):
        return dict(result, code=exc.code, message=str(exc), diagnostic=exc.diagnostics), False
    if isinstance(exc, ValueError):
        return dict(result, code='MODEL_CONFIG_ERROR', message='模型或运行配置无效。'), True
    return dict(result, code='CLAIM_REVIEW_FAILED', message='语义评审未完成，请检查模型配置和运行环境。'), True


def review_run_key(items, config, *, method='json_schema', max_input_chars=24000):
    protocol = config.get('quote_protocol', 'verbatim-v1')
    require(protocol in {'verbatim-v1', QUOTE_PROTOCOL}, '未知的评审引用协议')
    payload = {'policy_version': POLICY_VERSION, 'prompt': CLAIM_REVIEW_PROMPT,
                           'schema': ClaimReview.model_json_schema(), 'config': config,
                           'items': [(item['sample_id'], item['input_hash'], fingerprint({
                               field: item.get(field) for field in ('sample_id', 'paper_title', 'statement', 'evidence')}))
                               for item in items],
                           'method': method, 'max_input_chars': max_input_chars}
    if protocol == QUOTE_PROTOCOL:
        payload.update(prompt=REFERENCE_REVIEW_PROMPT, schema=ReferenceClaimReview.model_json_schema(), span_policy=SPAN_POLICY, schema_policy=SCHEMA_POLICY)
    return fingerprint(payload)


def review_request(claim, item, protocol, max_input_chars):
    if protocol == QUOTE_PROTOCOL:
        payload = reference_payload(claim, max_input_chars=max_input_chars)
        return {'policy_version': POLICY_VERSION, 'quote_protocol': QUOTE_PROTOCOL, 'span_policy': SPAN_POLICY,
                'schema_policy': SCHEMA_POLICY, 'response_schema': response_schema(payload).model_json_schema(),
                'system': REFERENCE_REVIEW_PROMPT, 'input': payload,
                'input_hash': item['input_hash']}
    return {'policy_version': POLICY_VERSION, 'system': CLAIM_REVIEW_PROMPT,
            'input': claim.model_dump(mode='json'), 'input_hash': item['input_hash']}


def request_matches(request, claim, item, protocol, max_input_chars=24000):
    expected = review_request(claim, item, protocol, max_input_chars)
    return all(request.get(key) == value for key, value in expected.items())


def compatible_runs(base_dir, items, config, run_key, method, max_input_chars):
    """集合扩大时仅复用同配置、同提示词/结构和相同材料的成功评审。"""
    known = {item['sample_id']: item for item in items}
    candidates = []
    for path in sorted(base_dir.glob('*/run.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            saved = json.loads(path.read_text())
            ids = saved['selected_ids']
            if saved['run_key'] == run_key or saved['config'] != config:
                continue
            if not ids or len(set(ids)) != len(ids) or not set(ids) <= set(known):
                continue
            expected = review_run_key([known[key] for key in ids], config,
                                      method=method, max_input_chars=max_input_chars)
            if expected == saved['run_key'] and path.parent.name == expected[:20]:
                candidates.append((path.parent, expected))
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return candidates


def checked_cache(record, expected_key, item, claim, *, quote_protocol='verbatim-v1'):
    if not isinstance(record, dict) or record.get('sample_id') != item['sample_id']:
        return False
    if record.get('run_key') != expected_key or record.get('input_hash') != item['input_hash'] or record.get('status') != 'succeeded':
        return False
    try:
        review = ClaimReview.model_validate(record['review'])
        validate_review_output(review, claim)
        saved_protocol = record.get('quote_protocol', 'verbatim-v1')
        if quote_protocol == QUOTE_PROTOCOL:
            if saved_protocol == QUOTE_PROTOCOL:
                if record.get('span_policy') != SPAN_POLICY:
                    return False
                rebuilt = materialize_review(record['reference_response'], claim)
                if rebuilt.model_dump(mode='json') != review.model_dump(mode='json'):
                    return False
            elif saved_protocol == 'legacy-import':
                origin = record['legacy_import']
                source = origin['source_record']
                if (not checked_cache(source, origin['source_run_key'], item, claim)
                        or not request_matches(origin['source_request'], claim, item, 'verbatim-v1')
                        or source['review'] != record['review']):
                    return False
            else:
                return False
        elif saved_protocol != 'verbatim-v1':
            return False
        return True
    except (ValueError, KeyError, TypeError):
        return False


def recover_failed_candidate(record, expected_key, item, claim, request):
    """只恢复同一模型请求已保存的完整候选，不产生新语义标签或模型调用。"""
    if not isinstance(record, dict) or record.get('status') != 'failed':
        return None
    if record.get('run_key') != expected_key or record.get('input_hash') != item['input_hash'] or record.get('sample_id') != item['sample_id']:
        return None
    error = record.get('error') or {}
    if error.get('code') != 'CLAIM_REVIEW_INVALID':
        return None
    try:
        if request['system'] != CLAIM_REVIEW_PROMPT or request['input'] != claim.model_dump(mode='json') or request['input_hash'] != item['input_hash']:
            return None
        candidate = ClaimReview.model_validate(error['diagnostic']['unverified_candidate_review'])
        restored = restore_review_quotes(candidate, claim)
        validate_review_output(restored, claim)
    except (ValueError, KeyError, TypeError):
        return None
    return dict(record, status='succeeded', review=restored.model_dump(mode='json'),
                original_model_review=candidate.model_dump(mode='json'), quote_repairs=restored._quote_repairs,
                error=None, resumed=True, recovered_from_diagnostic=True, recovery_model_calls=0,
                updated_at=now())


def run_reviews(items, llm_factory, run_dir, config, *, method='json_schema', max_input_chars=24000, progress=None, verify_input=None):
    """只接收冻结输入，不接收 gold；缓存及保存的完整候选恢复后均重新核验。"""
    base_dir = Path(run_dir)
    results, stopped = [], False
    protocol = config.get('quote_protocol', 'verbatim-v1')
    run_key = review_run_key(items, config, method=method, max_input_chars=max_input_chars)
    other_runs = compatible_runs(base_dir, items, config, run_key, method, max_input_chars)
    run_dir = base_dir / run_key[:20]
    run_dir.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    for number, item in enumerate(items, 1):
        key = item['sample_id']
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', key) is not None, '非法样本编号')
        path = run_dir / 'items' / f'{key}.json'
        cached = None
        if path.exists():
            try:
                cached = json.loads(path.read_text())
                if not isinstance(cached, dict):
                    cached = None
            except (ValueError, OSError):
                pass
        current = {'sample_id': key, 'input_hash': item['input_hash'], 'run_key': run_key,
                   'status': 'not_attempted', 'updated_at': now(), 'attempts': 0, 'resumed': False}
        try:
            claim = prepare_claim(item, max_input_chars=max_input_chars)
            pending_layout = None
            if checked_cache(cached, run_key, item, claim, quote_protocol=protocol):
                current.update(cached, resumed=True)
            else:
                if cached and cached.get('status') == 'failed':
                    # 先保留原失败记录，后续恢复或重试均不能抹去候选与诊断。
                    atomic_json(run_dir/'history'/key/f'{fingerprint(cached)}.json', cached)
                    try:
                        request = json.loads((run_dir/'requests'/f'{key}.json').read_text())
                        recovered = recover_failed_candidate(cached, run_key, item, claim, request) if protocol == 'verbatim-v1' else None
                        if recovered:
                            current.update(recovered)
                    except (ValueError, TypeError, OSError):
                        pass
                    if protocol == 'verbatim-v1' and current['status'] != 'succeeded' and cached.get('run_key') == run_key and cached.get('input_hash') == item['input_hash'] and cached.get('sample_id') == key:
                        try:
                            raw = cached.get('pending_candidate_review') or (cached.get('error') or {}).get('diagnostic', {}).get('unverified_candidate_review')
                            candidate = ClaimReview.model_validate(raw)
                            request = json.loads((run_dir/'requests'/f'{key}.json').read_text())
                            if request['system'] == CLAIM_REVIEW_PROMPT and request['input'] == claim.model_dump(mode='json') and request['input_hash'] == item['input_hash'] and layout_candidates(candidate, claim):
                                pending_layout = candidate
                        except (ValueError, KeyError, TypeError, OSError):
                            pass
            if current['status'] != 'succeeded':
                for previous_dir, previous_key in other_runs:
                    try:
                        previous = json.loads((previous_dir/'items'/f'{key}.json').read_text())
                        request = json.loads((previous_dir/'requests'/f'{key}.json').read_text())
                        if not checked_cache(previous, previous_key, item, claim, quote_protocol=protocol):
                            continue
                        request_protocol = 'verbatim-v1' if previous.get('quote_protocol') == 'legacy-import' else protocol
                        if not request_matches(request, claim, item, request_protocol, max_input_chars):
                            continue
                        current.update(previous, run_key=run_key, resumed=True,
                                       reused_from_run_key=previous_key)
                        atomic_json(run_dir/'requests'/f'{key}.json', dict(request, reused_from_run_key=previous_key))
                        break
                    except (ValueError, KeyError, TypeError, OSError):
                        continue
            if current['status'] != 'succeeded' and not stopped:
                if verify_input:
                    verify_input(item)
                current['attempts'] = (cached.get('attempts', 0) if cached and cached.get('run_key') == run_key else 0) + 1
                tick = monotonic()
                if progress:
                    progress(number, len(items), key, 'requesting')
                if pending_layout is not None:
                    current['pending_candidate_review'] = pending_layout.model_dump(mode='json')
                def layout_audit(record):
                    current['pending_candidate_review'] = record['candidate_review']
                    atomic_json(run_dir/'quote_selections'/f'{key}.json',record)
                request = review_request(claim, item, protocol, max_input_chars)
                atomic_json(run_dir/'requests'/f'{key}.json', request)
                with llm_factory() as llm:
                    if protocol == QUOTE_PROTOCOL:
                        review = review_claim_references(llm, item, method=method, max_input_chars=max_input_chars)
                    elif pending_layout is not None:
                        review = confirm_layout_quotes(llm,pending_layout,claim,method=method,audit=layout_audit)
                    else:
                        review = review_claim(llm, item, method=method, max_input_chars=max_input_chars,layout_audit=layout_audit)
                if verify_input:
                    verify_input(item)
                current.update(status='succeeded', seconds=round(monotonic()-tick, 3),
                               review=review.model_dump(mode='json'), quote_repairs=review._quote_repairs,
                               original_model_review=review._original_model_review,
                               layout_confirmation=review._layout_confirmation,
                               quote_protocol=protocol, span_policy=SPAN_POLICY if protocol == QUOTE_PROTOCOL else None,
                               reference_response=review._reference_response,
                               error=None, updated_at=now())
                current.pop('pending_candidate_review', None)
        except Exception as exc:
            error, global_failure = error_info(exc)
            current.update(status='precheck_failed' if isinstance(exc, ReviewPrecheckError) else 'failed', error=error, updated_at=now())
            stopped = stopped or global_failure
        if current['status'] == 'not_attempted':
            current['error'] = {'code': 'PREVIOUS_MODEL_FAILURE', 'message': '前一模型调用遇到连接、权限或配置问题，本项尚未请求；可重新运行继续。'}
        atomic_json(path, current)
        results.append(current)
        atomic_json(run_dir/'run.json', {'run_key': run_key, 'policy_version': POLICY_VERSION,
                    'config': config, 'selected_samples': len(items), 'selected_ids': [i['sample_id'] for i in items],
                    'updated_at': now(), 'elapsed_seconds': round(monotonic()-started, 3), 'items': results})
        if progress:
            progress(number, len(items), key, 'recovered' if current.get('recovered_from_diagnostic') else 'resumed' if current['resumed'] else current['status'])
    return run_dir, results


def compare_results(items, results, annotations):
    """成功的模型输出才参加分类评分，运行失败单独报告。"""
    by_id = {result['sample_id']: result for result in results}
    matrix = {actual: {predicted: 0 for predicted in VERDICTS} for actual in VERDICTS}
    details, compared, dangerous = [], 0, []
    for item in items:
        result = by_id[item['sample_id']]
        annotation = annotations.get(item['sample_id'])
        prediction = result.get('review', {}).get('verdict') if result['status'] == 'succeeded' else None
        row = {'sample_id': item['sample_id'], 'statement': item['statement'], 'status': result['status'],
               'human_label': annotation['gold_label'] if annotation else None, 'model_label': prediction,
               'human_reason': annotation['gold_reason'] if annotation else None,
               'model_reason': result.get('review', {}).get('reason'), 'correct': None,
               'error': result.get('error'), 'resumed': result.get('resumed', False),
               'quote_repairs': result.get('quote_repairs', []),
               'recovered_from_diagnostic': result.get('recovered_from_diagnostic', False)}
        if annotation and prediction:
            actual = annotation['gold_label']
            matrix[actual][prediction] += 1
            compared += 1
            row['correct'] = actual == prediction
            if actual in {'contradicted', 'insufficient'} and prediction == 'supported':
                dangerous.append(item['sample_id'])
        details.append(row)
    per_class = {}
    for verdict in VERDICTS:
        tp = matrix[verdict][verdict]
        actual = sum(matrix[verdict].values())
        predicted = sum(matrix[other][verdict] for other in VERDICTS)
        per_class[verdict] = {'human_support': actual, 'model_predictions': predicted,
            'precision': tp / predicted if predicted else None, 'recall': tp / actual if actual else None,
            'f1': 2 * tp / (actual + predicted) if actual + predicted else None}
    values = [row['f1'] for row in per_class.values() if row['f1'] is not None]
    eligible = sum(sum(matrix[label].values()) for label in ('contradicted', 'insufficient'))
    human_counts = Counter(annotations[item['sample_id']]['gold_label'] for item in items if item['sample_id'] in annotations)
    complete_labels = all(per_class[label]['human_support'] > 0 for label in VERDICTS)
    return {'scope': 'development_pilot_not_independent_validation',
        'selected': len(items), 'human_labelled': sum(human_counts.values()), 'human_label_counts': dict(human_counts),
        'missing_human_classes': [label for label in VERDICTS if not human_counts[label]],
        'runtime_status_counts': dict(Counter(row['status'] for row in details)),
        'completion_rate': sum(row['status'] == 'succeeded' for row in details)/len(items) if items else None,
        'compared': compared, 'correct': sum(matrix[label][label] for label in VERDICTS),
        'accuracy': sum(matrix[label][label] for label in VERDICTS)/compared if compared else None,
        'confusion_matrix': matrix, 'per_class': per_class,
        'macro_f1_observed_classes': sum(values)/len(values) if values else None,
        'macro_f1_all_four_classes': sum(values)/len(values) if complete_labels else None,
        'supported_precision': per_class['supported']['precision'],
        'false_supported': {'count': len(dangerous), 'eligible_compared': eligible,
                            'rate': len(dangerous)/eligible if eligible else None, 'sample_ids': dangerous},
        'needs_review_count': sum(row['model_label'] == 'needs_review' for row in details),
        'details': details,
        'limitations': ['样本少，且是开发对照，不代表独立验证或跨领域准确率。',
                       '失败或未请求项目不作为任何语义标签；分类指标只在成功且有人工标签的项目上计算。',
                       '无分母指标为 null；缺少人工类别时不报告完整四分类宏平均。']}


def render_comparison(report):
    def metric(value):
        return '不可计算' if value is None else f'{value:.1%}'
    title = {'held_out_paper_validation': '留出论文验证',
             'held_out_paper_post_model_adjudication': '留出论文人工复核对照'}.get(report.get('scope'), '开发材料对照')
    metric_label = ('复核后的标签一致率' if report.get('annotation_context', {}).get('model_influenced_labels')
                    else '成功且有标签项目的准确率')
    lines = ['# 辅助语义评审：' + title, '',
             f"- 选定 {report['selected']} 条，人工标注 {report['human_labelled']} 条，成功参与对照 {report['compared']} 条。",
             f"- 运行状态：{json.dumps(report['runtime_status_counts'], ensure_ascii=False)}。",
             f"- 完成率：{metric(report['completion_rate'])}；{metric_label}：{metric(report['accuracy'])}。",
             f"- 支持标签精确率：{metric(report['supported_precision'])}；将矛盾或不足误判为支持：{report['false_supported']['count']} / {report['false_supported']['eligible_compared']}。",
             '- 人工标签未覆盖类别：' + ('、'.join(report['missing_human_classes']) or '无') + '。',
             '', '## 逐项对照', '']
    for item in report['details']:
        lines += [f"### {item['sample_id']}", '', item['statement'], '',
                  f"- 状态：{item['status']}；人工：{item['human_label'] or '未标注'}；模型：{item['model_label'] or '未完成'}。",
                  f"- 人工理由：{item['human_reason'] or '未填写'}。"]
        if item['model_reason']:
            lines.append('- 模型理由：' + item['model_reason'])
        if item['error']:
            lines.append('- 运行问题：' + item['error']['code'] + ' · ' + item['error']['message'])
        if item.get('quote_repairs'):
            lines.append(f"- 引文排版核对：{len(item['quote_repairs'])} 处已定位并回填连续原文；行尾断词须经候选确认，模型语义判断未改动。")
        if item.get('recovered_from_diagnostic'):
            lines.append('- 从已保存的完整模型输出核验恢复，未重新请求模型；原失败记录已归档。')
        lines.append('')
    context = report.get('annotation_context') or {}
    if context.get('model_influenced_labels'):
        lines += ['## 改标后的结果范围', '',
                  '- 本轮包含查看模型输出后由人工确认的改标，当前指标为复核后的标签一致率，不是独立验证准确率。',
                  '- ' + context.get('note', '原始人工标签和改标前对照另行归档。'), '']
    lines += ['## 范围说明', ''] + ['- '+note for note in report['limitations']]
    return '\n'.join(lines) + '\n'
