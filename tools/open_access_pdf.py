"""按论文标识查找 OpenAlex 开放 PDF，限制下载大小并检查重定向目标。"""
import ipaddress
import math
import os
import socket
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests

from storage.repository import normalize_doi, normalize_openalex_id
from tools.http_diagnostics import request_diagnostic

MAX_BYTES = 20 * 1024 * 1024


class FulltextError(ValueError):
    def __init__(self, code, message, diagnostic=None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic


def pdf_download_settings():
    from tools.execution_config import CURRENT
    fixed = CURRENT.get()
    if fixed:
        return dict(fixed['pdf_download'])
    result = {}
    for name, key, default, maximum in (
        ('PDF_DOWNLOAD_TIMEOUT', 'total_timeout', 600, 3600),
        ('PDF_READ_TIMEOUT', 'read_timeout', 60, 600),
        ('PDF_CONNECT_TIMEOUT', 'connect_timeout', 10, 120),
    ):
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError:
            raise ValueError(f'{name} 必须为大于0且不超过{maximum}的秒数。') from None
        if not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError(f'{name} 必须为大于0且不超过{maximum}的秒数。')
        result[key] = value
    return result


def find_open_pdf_urls(doi=None, openalex_id=None):
    expected_doi = normalize_doi(doi)
    expected_id = normalize_openalex_id(openalex_id)
    if not expected_doi and not expected_id:
        raise FulltextError('PAPER_ID_REQUIRED', '该论文缺少 DOI 或 OpenAlex ID，暂时无法自动确认全文来源，请手动上传。')
    identifier = expected_id.rsplit('/', 1)[-1] if expected_id else 'https://doi.org/' + expected_doi
    params = {'select': 'id,doi,best_oa_location,locations,open_access'}
    from tools.execution_config import openalex_key
    if key := openalex_key():
        params['api_key'] = key
    with requests.get('https://api.openalex.org/works/' + quote(identifier, safe=':/'),
                      params=params, timeout=(10, 30)) as response:
        if response.status_code == 404:
            raise FulltextError('NO_OPEN_PDF', 'OpenAlex 未找到这篇论文的开放全文，请手动上传 PDF。')
        response.raise_for_status()
        work = response.json()
    if (expected_id and normalize_openalex_id(work.get('id')) != expected_id) or (
        expected_doi and normalize_doi(work.get('doi')) != expected_doi
    ):
        raise FulltextError('PAPER_ID_MISMATCH', '来源记录与当前论文标识不一致，已停止自动下载，请手动核对。')
    candidates = []
    locations = []
    if work.get('best_oa_location'):
        locations.append((work['best_oa_location'], True))
    locations.extend((location, False) for location in work.get('locations') or [])
    for location, best in locations:
        if not (location.get('is_oa') is True or (best and location.get('is_oa') is not False)):
            continue
        url = location.get('pdf_url')
        if not url:
            landing = urlsplit(location.get('landing_page_url') or '')
            if landing.hostname in {'arxiv.org', 'export.arxiv.org'} and landing.path.startswith('/abs/'):
                url = 'https://arxiv.org/pdf/' + landing.path.removeprefix('/abs/')
        if isinstance(url, str) and url.startswith('https://') and url not in candidates:
            candidates.append(url)
    if not candidates:
        raise FulltextError('NO_OPEN_PDF', '未找到可直接下载的开放获取 PDF。论文可能只有摘要、网页全文或需要订阅，请手动上传。')
    return candidates[:5]


def validate_pdf_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise FulltextError('UNSAFE_PDF_URL', '全文地址不是可用的公开 HTTPS 地址。')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise FulltextError('PDF_HOST_UNREACHABLE', '无法解析全文下载站点，请检查网络或稍后重试。') from None
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise FulltextError('UNSAFE_PDF_URL', '全文下载地址指向非公开网络，已停止下载。')


def download_open_pdf(url: str, destination: Path, *, progress=None, settings=None, visited_urls=None):
    settings = settings or pdf_download_settings()
    current, started = url, time.monotonic()
    total, expected, phase = 0, None, 'resolve'
    response_received, http_status, content_type, pdf_header_verified = False, None, None, False
    last_report, last_phase = started, None

    def details(reason=None):
        elapsed = max(0, time.monotonic()-started)
        return {'host': urlsplit(current).hostname, 'phase': phase,
                'candidate_found': True, 'response_received': response_received, 'http_status': http_status,
                'content_type': content_type, 'pdf_header_verified': pdf_header_verified,
                'elapsed_seconds': round(elapsed, 2), 'received_bytes': total, 'total_bytes': expected,
                'average_bytes_per_second': round(total/elapsed, 1) if elapsed else 0,
                **settings, **({'reason': reason} if reason else {})}

    def report(force=False):
        nonlocal last_report, last_phase
        instant = time.monotonic()
        if progress and (force or phase != last_phase or instant-last_report >= 2):
            progress(details())
            last_report, last_phase = instant, phase

    def content_status():
        if pdf_header_verified:
            return '已确认 PDF 文件头，但文件尚未完整下载。'
        if response_received:
            return '已收到 HTTP 响应，但尚未确认返回内容是 PDF。'
        return '已取得候选地址，但尚未收到 HTTP 响应。'

    def remaining():
        value = settings['total_timeout']-(time.monotonic()-started)
        if value <= 0:
            diagnostic = details('total_timeout')
            message = (f"{diagnostic['host']}：PDF 下载达到总时限 {settings['total_timeout']:g} 秒，"
                       f"已接收 {total} 字节（{total/1024/1024:.2f} MB）。{content_status()}")
            raise FulltextError('PDF_DOWNLOAD_TIMEOUT', message, dict(diagnostic, message=message))
        return value

    try:
        for _ in range(6):
            response_received, http_status, content_type, pdf_header_verified = False, None, None, False
            phase = 'resolve';report()
            remaining();validate_pdf_url(current)
            if visited_urls is not None:
                visited_urls.add(current)
            phase = 'response_headers';report()
            left = remaining()
            with requests.get(current, stream=True,
                              timeout=(min(settings['connect_timeout'], left), min(settings['read_timeout'], left)),
                              allow_redirects=False,
                              headers={'User-Agent': 'ResearchPilot/0.1 (open-access PDF reader)', 'Accept': 'application/pdf'}) as response:
                response_received = True
                http_status = response.status_code
                media = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                content_type = media if media in {'application/pdf','application/octet-stream','text/html','text/plain','application/json'} else 'other' if media else None
                remaining()
                if response.is_redirect:
                    location = response.headers.get('Location')
                    if not location:
                        raise FulltextError('PDF_DOWNLOAD_FAILED', '全文站点未提供有效的跳转地址。')
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                length = response.headers.get('Content-Length', '')
                expected = int(length) if length.isdigit() else None
                if expected is not None and expected > MAX_BYTES:
                    raise FulltextError('PDF_TOO_LARGE', '开放 PDF 超过 20 MB，需自行下载后处理。')
                destination.parent.mkdir(parents=True, exist_ok=True)
                header = bytearray()
                phase = 'download';report(force=True)
                with destination.open('wb') as output:
                    for block in response.iter_content(chunk_size=16384):
                        if not block:
                            remaining()
                            continue
                        total += len(block)
                        if total > MAX_BYTES:
                            raise FulltextError('PDF_TOO_LARGE', '开放 PDF 超过 20 MB，需自行下载后处理。')
                        if len(header) < 5:
                            header.extend(block[:5-len(header)])
                        if len(header) == 5 and bytes(header) != b'%PDF-':
                            raise FulltextError('NOT_A_PDF', '全文站点返回的不是 PDF，可能需要登录或验证码，请手动下载。')
                        first_pdf_bytes = len(header) == 5 and not pdf_header_verified
                        pdf_header_verified = bytes(header) == b'%PDF-'
                        remaining()
                        output.write(block)
                        report(force=first_pdf_bytes)
                remaining()
                if bytes(header) != b'%PDF-':
                    raise FulltextError('NOT_A_PDF', '全文站点返回的不是 PDF，可能需要登录或验证码，请手动下载。')
                phase = 'completed';report(force=True)
                return current
        raise FulltextError('PDF_REDIRECT_LIMIT', '全文站点跳转次数过多，请手动下载 PDF。')
    except requests.RequestException as exc:
        destination.unlink(missing_ok=True)
        network = request_diagnostic(exc)
        causes = network.get('causes', [])
        is_timeout = isinstance(exc, requests.Timeout) or 'ReadTimeoutError' in causes
        if is_timeout:
            connecting = isinstance(exc, requests.ConnectTimeout)
            reason = 'connect_timeout' if connecting else 'read_timeout'
            diagnostic = dict(network, **details(reason))
            if connecting:
                diagnostic['phase'] = 'connect'
                message = f"{diagnostic['host']}：建立 PDF 连接超时，尚未完成连接。"
            else:
                message = (f"{diagnostic['host']}：等待 PDF 响应数据超时，已接收 {total} 字节（{total/1024/1024:.2f} MB）。"
                           +content_status())
            diagnostic['message'] = message
            raise FulltextError('PDF_CONNECT_TIMEOUT' if connecting else 'PDF_READ_TIMEOUT', message, diagnostic) from None
        exc.download_diagnostic = dict(network, **details('request_failed'))
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, FulltextError) and exc.diagnostic is None:
            exc.diagnostic = dict(details(), error_type=type(exc).__name__, message=str(exc))
        raise
