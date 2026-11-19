"""发布内容与单次运行配置分离；未保留的旧代码明确拒绝恢复。"""
from importlib.metadata import distributions
import json
import platform
from typing import Literal

from pydantic import Field, field_validator

from agent.runtime_snapshot import (
    FrozenModel, ROOT, RuntimeUnavailable, assert_code_available, canonical,
    code_manifest, fingerprint, safe_manifest,
)


def dependencies():
    return {'python': platform.python_version(), 'platform': platform.system(), 'machine': platform.machine(),
            'packages': sorted({(item.metadata['Name'], item.version) for item in distributions()
                                if item.metadata['Name']})}


class AgentVersion(FrozenModel):
    protocol: Literal['agent-version-v1'] = 'agent-version-v1'
    role: Literal['research', 'analysis', 'review']
    # JSON 字符串避免 frozen Pydantic 对象内仍有可修改的嵌套 dict/list。
    code_json: str
    dependencies_json: str
    contracts_json: str
    tools_json: str
    quote_protocol: str = Field(min_length=1)

    @field_validator('code_json', 'dependencies_json', 'contracts_json', 'tools_json')
    @classmethod
    def canonical_json(cls, value):
        return canonical(safe_manifest(json.loads(value)))

    @property
    def id(self):
        return 'version_' + fingerprint(self.model_dump(mode='json'))

    @classmethod
    def capture(cls, *, role, contracts, tools, quote_protocol, root=ROOT):
        """contracts 须由调用方提供实际 Prompt/Schema 内容，不接受仅有摘要的合同。"""
        if not contracts or any(not isinstance(item, dict) or not item.get('prompt') or not item.get('schema')
                                for item in contracts.values()):
            raise ValueError('版本必须保存实际 Prompt 与 Schema 内容。')
        return cls(role=role, code_json=canonical(code_manifest(root)),
                   dependencies_json=canonical(dependencies()), contracts_json=canonical(contracts),
                   tools_json=canonical(tools), quote_protocol=quote_protocol)

    def assert_available(self, *, root=ROOT):
        assert_code_available(json.loads(self.code_json), root)
        if canonical(dependencies()) != self.dependencies_json:
            raise RuntimeUnavailable('Python 或依赖版本已改变，不能恢复原执行版本。')

    def contract(self, name):
        return json.loads(self.contracts_json)[name]


def pinned_llm_factory(snapshot, *, version, root=ROOT):
    """供批次/重试循环复用；每次创建客户端均检查原代码与运行配置。"""
    from agent.llm_runtime import analysis_llm
    from agent.runtime_snapshot import RuntimeSnapshot
    snapshot = RuntimeSnapshot.model_validate(snapshot)
    version = AgentVersion.model_validate(version)

    def factory(**_legacy_defaults):
        version.assert_available(root=root)
        return analysis_llm(runtime_snapshot=snapshot)

    return factory
