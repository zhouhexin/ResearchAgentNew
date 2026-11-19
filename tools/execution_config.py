"""单次执行的工具配置；ContextVar 隔离并发，不修改环境变量。"""
from contextlib import contextmanager
from contextvars import ContextVar
import os

from agent.runtime_snapshot import RuntimeUnavailable, identity, network_config

CURRENT = ContextVar('research_tool_config', default=None)


def capture():
    from tools.venue_search.catalog import VENUES
    from tools.open_access_pdf import pdf_download_settings
    return {'source_ids': {f'OPENALEX_{venue["acronym"].upper()}_SOURCE_IDS':
                           os.getenv(f'OPENALEX_{venue["acronym"].upper()}_SOURCE_IDS', '')
                           for venue in VENUES.values()},
            'openalex_identity': identity(os.getenv('OPENALEX_API_KEY'), create=True),
            'network_identity': identity(network_config(True), create=True),
            'database_identity': identity(os.getenv('DATABASE_URL'), create=True),
            'pdf_download': pdf_download_settings(), 'mode': 'live-with-checkpoints',
            'http_timeout': [10, 30], 'verification_seconds': 90, 'screen_batch_size': 5}


def check(config):
    if (identity(os.getenv('OPENALEX_API_KEY')) != config['openalex_identity']
            or identity(network_config(True)) != config['network_identity']
            or identity(os.getenv('DATABASE_URL')) != config['database_identity']):
        raise RuntimeUnavailable('原检索凭据或网络配置不可用，不能替换后继续原任务。')


@contextmanager
def scope(config):
    if config:
        check(config)
    token = CURRENT.set(config)
    try:
        yield
    finally:
        CURRENT.reset(token)


def openalex_key():
    config = CURRENT.get()
    if config:
        check(config)
    return os.getenv('OPENALEX_API_KEY')


def source_ids(name):
    config = CURRENT.get()
    return config['source_ids'].get(name, '') if config else os.getenv(name, '')
