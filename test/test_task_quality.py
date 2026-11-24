from copy import deepcopy
from pathlib import Path
import json
import pytest
from pydantic import ValidationError
from evaluation.task_quality import Dataset, Run, InvalidEvaluation, load, score, compare, write_once
from run_task_evaluation import main

ROOT=Path(__file__).resolve().parents[1]/'evaluation/examples/task_quality_v1'

@pytest.fixture
def demo():return load(ROOT/'dataset.json',Dataset),load(ROOT/'baseline.json',Run),load(ROOT/'candidate.json',Run)


def test_failed_and_missing_tasks_stay_in_denominators_and_usage_unknown(demo):
 dataset,baseline,candidate=demo
 result=score(dataset,baseline)
 assert result['metrics']['task_denominator']==4
 assert result['metrics']['statuses']=={'succeeded':2,'failed':1,'missing':1}
 assert result['metrics']['requirement_denominator']==4
 assert result['metrics']['unreviewed_requirements']==2
 assert result['metrics']['usage']['tokens']=={'known_count':0,'total_count':4,'known_sum':None}
 assert result['metrics']['usage']['calls']=={'known_count':3,'total_count':4,'known_sum':3}
 assert result['by_sample_kind']['synthetic']['task_denominator']==4
 assert result['rows'][-1]['error_code']=='RESULT_MISSING'


def test_literal_citation_and_runtime_and_human_scores_are_separate(demo):
 dataset,baseline,candidate=demo
 result=compare(dataset,baseline,candidate)
 assert result['left']['metrics']['execution_success_fraction']=={'numerator':2,'denominator':4}
 assert result['right']['metrics']['execution_success_fraction']=={'numerator':3,'denominator':4}
 assert result['right']['rows'][0]['source_status']=='failed'
 assert result['right']['rows'][0]['status']=='succeeded'
 assert result['promotion_decision']=='not_evaluated' and result['changed_pairs']==2
 assert 'source_span_mismatch' in str(result)


@pytest.mark.parametrize('token',['group','paper','document','text'])
def test_related_material_cannot_cross_split_even_if_group_renamed(demo,token):
 dataset,_,_=demo;raw=dataset.model_dump();a,b=raw['cases'][:2];b['split']='holdout'
 if token=='group':b['group_id']=a['group_id']
 elif token=='paper':b['materials'][0]['paper_key']=a['materials'][0]['paper_key']
 elif token=='document':b['materials'][0]['document_sha256']=a['materials'][0]['document_sha256']
 else:
  b['materials'][0]['text']=a['materials'][0]['text'];b['materials'][0]['text_sha256']=a['materials'][0]['text_sha256']
 with pytest.raises(ValidationError,match='泄漏'):Dataset.model_validate(raw)


@pytest.mark.parametrize('change',['duplicate','foreign','wrong_input','wrong_dataset','bad_repetition','unknown_requirement'])
def test_bad_run_identity_cannot_be_scored(demo,change):
 dataset,baseline,_=demo;raw=baseline.model_dump()
 if change=='duplicate':raw['outcomes'].append(deepcopy(raw['outcomes'][0]))
 elif change=='foreign':raw['outcomes'][0]['case_id']='unknown'
 elif change=='wrong_input':raw['outcomes'][0]['input_hash']='a'*64
 elif change=='wrong_dataset':raw['dataset_sha256']='a'*64
 elif change=='bad_repetition':raw['outcomes'][0]['repetition']=3
 else:raw['outcomes'][0]['human_judgments'][0]['requirement_id']='invented'
 with pytest.raises(InvalidEvaluation):score(dataset,Run.model_validate(raw))


def test_withdrawal_denies_score_and_annotation_revision_requires_new_run(demo):
 dataset,baseline,_=demo
 for change in ('withdraw','revision'):
  raw=dataset.model_dump()
  if change=='withdraw':raw['cases'][0]['permission']['allowed']=False
  else:raw['annotation_version']='2'
  changed=Dataset.model_validate(raw)
  with pytest.raises(InvalidEvaluation):score(changed,baseline)
  if change=='withdraw':
   raw_run=baseline.model_dump();raw_run['dataset_sha256']=changed.sha256
   with pytest.raises(InvalidEvaluation,match='授权'):score(changed,Run.model_validate(raw_run))


def test_holdout_requires_explicit_opt_in_and_modes_not_mixed(demo):
 dataset,baseline,candidate=demo
 raw=baseline.model_dump();raw.update(split='holdout',outcomes=[])
 with pytest.raises(InvalidEvaluation,match='留出'):score(dataset,Run.model_validate(raw))
 held=score(dataset,Run.model_validate(raw),True)
 assert held['metrics']['statuses']=={'missing':2}
 for change in ({'mode':'live_search'},{'kind':'recorded_run'},{'repetitions':1}):
  other=candidate.model_copy(update=change)
  with pytest.raises(InvalidEvaluation):compare(dataset,baseline,other)


def test_machine_inspected_labels_count_separately(demo):
 dataset,baseline,_=demo;raw=baseline.model_dump()
 raw['outcomes'][0]['human_judgments'][0].update(context='after_model_inspection',previous_verdict='unmet')
 report=score(dataset,Run.model_validate(raw))
 assert report['metrics']['independent_human']=={'met':1}
 assert report['metrics']['after_model_inspection']=={'met':1}
 assert report['rows'][0]['human_judgments'][0]['previous_verdict']=='unmet'


def test_cli_outputs_immutable_reports_and_never_calls_model(tmp_path,monkeypatch,capsys):
 monkeypatch.setattr('agent.llm_runtime.analysis_llm',lambda **kw:pytest.fail('offline command must not call model'))
 args=['--dataset',str(ROOT/'dataset.json'),'--run',str(ROOT/'candidate.json'),'--baseline',str(ROOT/'baseline.json'),'--output-dir',str(tmp_path/'report')]
 assert main(args)==0
 saved=(tmp_path/'report/report.json').read_text()
 assert 'not_evaluated' in saved and (tmp_path/'report/report.md').is_file()
 assert main(args)==1 and (tmp_path/'report/report.json').read_text()==saved
 assert main(['--dataset',str(ROOT/'dataset.json'),'--check-only'])==0
 assert '"model_calls": 0' in capsys.readouterr().out


def test_tampered_material_and_extra_model_label_not_accepted_as_human(demo):
 dataset,baseline,_=demo;raw=dataset.model_dump();raw['cases'][0]['materials'][0]['text']='changed'
 with pytest.raises(ValidationError):Dataset.model_validate(raw)
 raw=baseline.model_dump();raw['outcomes'][0]['model_judgment']='met'
 with pytest.raises(ValidationError):Run.model_validate(raw)


def test_failed_task_judgment_is_visible_but_not_counted_as_successful_quality(demo):
 dataset,baseline,_=demo;raw=baseline.model_dump()
 raw['outcomes'][1]['human_judgments']=[deepcopy(raw['outcomes'][0]['human_judgments'][0])]
 result=score(dataset,Run.model_validate(raw))
 assert result['metrics']['independent_human']=={'met':2}
 assert result['metrics']['non_success_human']=={'met':1}
 assert result['metrics']['unreviewed_requirements']==1
