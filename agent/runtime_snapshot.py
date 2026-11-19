"""P0 运行配置契约。只保存可回放配置及凭据指纹，不保存凭据。"""
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_KEY = ROOT/'outputs/runtime_identity.key'
NETWORK_KEYS = ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy',
                'OPENAI_PROXY','NO_PROXY','no_proxy','SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE',
                'CURL_CA_BUNDLE','CODEX_HOME','OPENAI_ORG_ID','OPENAI_ORGANIZATION','OPENAI_PROJECT_ID',
                'OPENAI_API_VERSION')


class RuntimeUnavailable(ValueError):
    code = 'RUNTIME_SNAPSHOT_UNAVAILABLE'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return sha256(canonical(value).encode()).hexdigest()


def identity_key(*, create=False):
    path = IDENTITY_KEY
    if not path.exists() and create:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(os.urandom(32))
            try:os.link(temporary,path)
            except FileExistsError:pass
        finally:Path(temporary).unlink(missing_ok=True)
    if not path.is_file() or len(value := path.read_bytes()) != 32:
        raise RuntimeUnavailable('运行身份校验文件不可用，不能恢复原凭据引用。')
    return value


def identity(value, *, create=False):
    return hmac.new(identity_key(create=create), canonical(value).encode(), 'sha256').hexdigest()


def network_config(trust_env):
    # 只散列证书文件，不持久化代理 URL / 代理密码。
    values = {key: os.getenv(key) for key in NETWORK_KEYS
              if trust_env or key.startswith('OPENAI_ORG') or key in {'OPENAI_PROJECT_ID', 'OPENAI_API_VERSION'}}
    for key in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'):
        if values.get(key):
            path = Path(values[key])
            values[key] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
    return values


def codex_login_identity():
    return {'login': 'codex-managed', 'home': os.getenv('CODEX_HOME') or str(Path.home() / '.codex')}


def codex_tool_version(path):
    import re
    try:
        result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeUnavailable('无法确认原 Codex CLI 版本。') from None
    version = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+ /()\-]{0,120}', version):
        raise RuntimeUnavailable('Codex CLI 版本输出不可用。')
    return version


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


class RuntimeSnapshot(FrozenModel):
    protocol: Literal['runtime-snapshot-v1'] = 'runtime-snapshot-v1'
    provider: Literal['openai','codex']
    model: str = Field(min_length=1)
    endpoint: str
    backend: Literal['sdk','requests','codex']
    read_timeout: float = Field(gt=0, allow_inf_nan=False)
    retries: int = Field(ge=0, strict=True)
    trust_env: bool
    max_output_tokens: int | None = Field(default=None,gt=0,strict=True)
    enable_thinking: bool | None = None
    credential_ref: Literal['env:LLM_API_KEY','codex-managed-login']
    credential_identity: str = Field(pattern=r'^[0-9a-f]{64}$')
    network_identity: str = Field(pattern=r'^[0-9a-f]{64}$')
    codex_executable: str | None = None
    codex_executable_sha256: str | None = None
    codex_cli_version: str | None = None
    codex_path_identity: str | None = None
    codex_system_proxy: bool = False

    @model_validator(mode='after')
    def valid(self):
        parsed = urlsplit(self.endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('模型地址不能携带凭据、查询参数或片段。')
        if self.provider=='codex':
            if (self.backend!='codex' or self.endpoint!='codex://chatgpt'
                    or self.credential_ref!='codex-managed-login' or not self.codex_executable
                    or not self.codex_executable_sha256 or not self.codex_cli_version or not self.codex_path_identity):
                raise ValueError('Codex 快照配置不完整。')
        elif self.backend=='codex' or self.credential_ref!='env:LLM_API_KEY' or parsed.scheme not in {'http','https'} or not parsed.hostname:
            raise ValueError('API 快照配置不完整。')
        return self

    @property
    def id(self):
        return 'runtime_'+fingerprint(self.model_dump(mode='json'))


def capture_runtime(*, read_timeout, retries=0, trust_env=True, http_backend=None, max_output_tokens=None):
    from agent.llm_runtime import model_provider, model_http_backend, model_request_options
    provider = model_provider()
    backend = model_http_backend(http_backend)
    options = model_request_options()
    limit = max_output_tokens if max_output_tokens is not None else options.get('max_tokens')
    network = network_config(trust_env)
    fields = dict(provider=provider,backend=backend,read_timeout=read_timeout,retries=retries,trust_env=trust_env,
        max_output_tokens=limit,network_identity=identity(network,create=True))
    if provider=='codex':
        from agent.codex_model import configured_codex_model,codex_executable,use_system_proxy
        model = configured_codex_model()
        if not model:
            raise ValueError('固定运行版本需要显式设置 CODEX_MODEL，不能使用 CLI 隐式默认模型。')
        path = Path(codex_executable()).resolve()
        fields.update(model=model,endpoint='codex://chatgpt',credential_ref='codex-managed-login',
            credential_identity=identity(codex_login_identity(),create=True),
            codex_executable=str(path),codex_executable_sha256=sha256(path.read_bytes()).hexdigest(),
            codex_cli_version=codex_tool_version(path), codex_path_identity=identity(os.getenv('PATH'),create=True),
            codex_system_proxy=use_system_proxy(trust_env), max_output_tokens=None, retries=0)
    else:
        model,key = os.getenv('LLM_MODEL',''),os.getenv('LLM_API_KEY','')
        if not model or not key:
            raise ValueError('请配置 LLM_MODEL 与 LLM_API_KEY。')
        fields.update(model=model,endpoint=os.getenv('LLM_BASE_URL') or 'https://api.openai.com/v1',
            credential_ref='env:LLM_API_KEY',credential_identity=identity(key,create=True),
            enable_thinking=options.get('extra_body',{}).get('enable_thinking'))
    return RuntimeSnapshot(**fields)


def resolve_runtime(snapshot):
    """环境默认值可变化；凭据或网络身份变化时不静默替换。"""
    snapshot = RuntimeSnapshot.model_validate(snapshot)
    network = network_config(snapshot.trust_env)
    if not hmac.compare_digest(snapshot.network_identity,identity(network)):
        raise RuntimeUnavailable('网络或登录目录配置已变化，原运行快照不可恢复。')
    if snapshot.provider=='openai':
        key = os.getenv('LLM_API_KEY','')
        if not key or not hmac.compare_digest(snapshot.credential_identity,identity(key)):
            raise RuntimeUnavailable('原模型凭据不可用或已轮换，请显式建立新任务。')
        return snapshot,key
    path = Path(snapshot.codex_executable)
    if not path.is_file() or sha256(path.read_bytes()).hexdigest()!=snapshot.codex_executable_sha256:
        raise RuntimeUnavailable('原 Codex 执行文件已改变，不能静默恢复。')
    if (codex_tool_version(path) != snapshot.codex_cli_version
            or not hmac.compare_digest(snapshot.codex_path_identity,identity(os.getenv('PATH')))):
        raise RuntimeUnavailable('Codex CLI 版本或启动环境已改变，不能静默恢复。')
    if not hmac.compare_digest(snapshot.credential_identity,identity(codex_login_identity())):
        raise RuntimeUnavailable('原 Codex 登录目录已改变。')
    return snapshot,None


def code_manifest(root=ROOT):
    """只散列 Git 管理的代码/文档和未忽略新增文件，不读取 .env 或运行数据。"""
    def git(*args):
        return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL)
    head = git('rev-parse','HEAD').decode().strip()
    files = git('ls-files','--cached','--others','--exclude-standard','-z').decode().split('\0')
    hashes = {}
    for name in sorted(set(filter(None,files))):
        path = root/name
        if path.suffix.lower() not in {'.py','.ts','.vue','.json','.md','.txt','.toml','.yml','.yaml',
                                       '.css','.html','.js','.mjs','.sh','.sql','.lock','.ini','.cfg'}:
            continue
        if any(part in {'outputs','data','node_modules','.git','.aws','.codex'} for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            raise RuntimeUnavailable('代码清单不能依赖符号链接，请使用可保留的普通 artifact。')
        hashes[name] = sha256(path.read_bytes()).hexdigest() if path.is_file() else 'deleted'
    return {'git_sha':head,'dirty':bool(git('status','--porcelain')), 'files':hashes,
            'content_fingerprint':fingerprint(hashes)}


def assert_code_available(manifest,root=ROOT):
    if code_manifest(root)['content_fingerprint']!=manifest['content_fingerprint']:
        raise RuntimeUnavailable('运行代码已改变，需使用原 artifact 或显式建立新版本。')


def safe_manifest(value):
    """持久化前拒绝已知凭据原值及带认证信息的 URL，错误不回显内容。"""
    encoded = canonical(value)
    for name,secret in os.environ.items():
        if any(token in name.upper() for token in ('API_KEY','PASSWORD','ACCESS_TOKEN','SECRET')) and len(secret)>=8 and secret in encoded:
            raise ValueError('版本清单包含敏感配置，未保存。')
    def visit(node):
        if isinstance(node,dict):
            for key,item in node.items():
                if any(x in key.lower() for x in ('password','api_key','authorization','cookie',
                                                  'access_token','refresh_token','client_secret')):
                    raise ValueError('版本清单包含禁止字段，未保存。')
                visit(item)
        elif isinstance(node,list):
            for item in node:visit(item)
        elif isinstance(node,str) and re_credential_url(node):
            raise ValueError('版本清单包含带凭据地址，未保存。')
    visit(value)
    return value


def re_credential_url(value):
    import re
    return (re.search(r'(?:https?|postgres(?:ql)?(?:\+\w+)?)://[^\s/@]+@',value, re.IGNORECASE)
            or re.search(r'https?://[^\s]+[?&](?:api[_-]?key|access_token|token|password|secret|signature)=',
                         value, re.IGNORECASE))
