"""用 .env 配置测试真实会议检索，不调用 LLM、不改文献库。"""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path

from dotenv import load_dotenv
import requests

from tools.open_access_pdf import FulltextError
from tools.venue_search.cvpr import search_cvpr_page, search_venue_page
from tools.venue_search.catalog import VENUES

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', default='monocular depth')
    parser.add_argument('--start-year', type=int, default=date.today().year-2)
    parser.add_argument('--end-year', type=int, default=date.today().year-1)
    parser.add_argument('--limit', type=int, default=1, choices=range(1, 21))
    parser.add_argument('--main-only', action='store_true')
    parser.add_argument('--venues', nargs='+', choices=list(VENUES), default=['cvpr'])
    args = parser.parse_args(argv)
    load_dotenv(ROOT / '.env')
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'query': args.query,
              'start_year': args.start_year, 'end_year': args.end_year, 'main_only': args.main_only, 'venues':args.venues}
    try:
        fetch = search_cvpr_page if args.venues==['cvpr'] else lambda query,**kwargs:search_venue_page(query,venue_slugs=args.venues,**kwargs)
        result = fetch(args.query, page_size=args.limit, start_year=args.start_year,
                                 end_year=args.end_year, verified_main_only=args.main_only)
        report.update(ok=True, total=result['total'], publication_counts=result['publication_counts'],
                      sources=result['source_ids'], notice=result['notice'],
                      papers=[{key: p.get(key) for key in ('title', 'year', 'venue_slug', 'openalex_id', 'publication')} for p in result['items']])
    except requests.RequestException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        report.update(ok=False, code='VENUE_HTTP_ERROR' if status else 'VENUE_CONNECTION_ERROR',
                      http_status=status, error_type=type(exc).__name__,
                      message='会议来源请求失败，请检查本机网络、OpenAlex Key/额度或稍后重试。')
    except FulltextError as exc:
        report.update(ok=False, code=exc.code, message=str(exc))
    except (ValueError, OSError) as exc:
        report.update(ok=False, code='VENUE_CONFIGURATION_OR_STORAGE_ERROR', error_type=type(exc).__name__,
                      message='请检查年份、检索条件、环境配置和缓存目录。')
    destination = ROOT/'outputs'/('cvpr_search_diagnostic.json' if args.venues==['cvpr'] else 'venue_search_diagnostic.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('诊断已保存：outputs/'+destination.name)
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
