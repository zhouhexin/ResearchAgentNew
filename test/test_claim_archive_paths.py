"""实际长路径原子归档，保持原文件名与内容，不改变评审或缓存判定。"""
import json
import os
from pathlib import Path

from evaluation.claim_quality import atomic_json


def test_failed_review_archive_keeps_full_hash_on_long_path(tmp_path):
    target=tmp_path/('r'*70)/('s'*50)/'history'/'research_questions-0'/('a'*64+'.json')
    assert len(str(target))>260
    value={'status':'failed','error':{'code':'CONTROLLED_FAILURE'},'original_preserved':True}
    atomic_json(target,value)
    readable=Path('\\\\?\\'+str(target.absolute())) if os.name=='nt' else target
    assert json.loads(readable.read_text(encoding='utf-8'))==value
    assert not list(readable.parent.glob('.claim-*'))
