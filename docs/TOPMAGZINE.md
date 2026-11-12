# ResearchPilot：顶刊顶会检索模块接入方案

既然 ResearchPilot 主要面向 人工智能、机器学习、计算机视觉、自然语言处理，我建议把顶刊顶会检索做成一个独立的研究能力模块，与现有 OpenAlex、PostgreSQL、LangGraph 和 Vue + FastAPI 系统集成。

推荐采用：OpenAlex + DBLP + Semantic Scholar 负责论文发现，CCF 目录负责会议期刊分级，官方论文集负责最终发表状态核验。

不要让 LLM 自己判断“这是不是顶会论文”。它应该调用检索工具，由程序根据明确的数据来源给出判断。

## 一、先明确各个平台的职责

| 数据源              | 核心职责                 | 接入优先级     |
| ---------------- | -------------------- | --------- |
| OpenAlex         | 按关键词、年份及发表来源检索       | P0，已有基础   |
| DBLP             | 计算机领域会议、期刊及论文身份补充    | P0        |
| Semantic Scholar | 补充相关论文、引用量、语义检索      | P1        |
| 中国计算机学会（CCF）     | A/B/C 分级参考           | P0，本地维护目录 |
| 官方会议论文集          | 核查主会、Workshop、正式录用状态 | P0，分会议实现  |
| OpenReview       | ICLR 等会议的投稿与录用记录     | P1        |

这一分工的依据是：OpenAlex 支持按发表来源的 ID 筛选论文；DBLP 提供论文、作者、发表场所的搜索 API；Semantic Scholar 支持年份和 `venue` 参数检索。

[image](https://www.google.com/s2/favicons?domain=https://help.openalex.org\&sz=32)

OpenAlex Help Center+2



### 建议第一批支持的发表场所

| 方向     | 会议                | 期刊                             |
| ------ | ----------------- | ------------------------------ |
| 机器学习   | NeurIPS、ICML、ICLR | JMLR、TMLR                      |
| 计算机视觉  | CVPR、ICCV、ECCV    | TPAMI、IJCV                     |
| 自然语言处理 | ACL、EMNLP、NAACL   | TACL、Computational Linguistics |
| AI 综合  | AAAI、IJCAI        | Artificial Intelligence、JAIR   |

这些是首批重点维护的候选名单，不意味着所有项目都需要标记为 CCF A。实际 CCF 等级应当从官方目录逐项导入，不能直接根据这张名单推断。

截至 2026 年 11 月，CCF 第七版目录已于 2026 年 3 月 31 日发布，并在 4 月 9 日更新勘误。ResearchPilot 应记录目录版本，而不是把等级当作永远不变的属性。

[image](https://www.google.com/s2/favicons?domain=https://www.ccf.org.cn\&sz=32)

ccf.org.cn



## 二、推荐的系统架构

Vue：顶刊顶会检索页面

关键词 · 领域 · 会议/期刊 · CCF 等级 · 年份

FastAPI：Venue Search API

参数校验 · 并发检索 · 分页 · 结果聚合

OpenAlex

论文发现

DBLP

元数据核对

Semantic Scholar

补充搜索

Venue Resolver + Publication Verifier

归一化会议名称 · 分级匹配 · 主会核验 · 去重

现有 PostgreSQL + ResearchPilot Agent

保存论文 · 全文解析 · AI 分析 · 生成综述

我们的实施顺序是：

1. 新增 `Venue` 数据库模型，解决会议/期刊的标准化身份问题。
2. 建立 CCF 2026 目录和外部数据库 ID 的映射。
3. 实现按 Venue 精准查询的 OpenAlex 适配器。
4. 接入 DBLP 和 Semantic Scholar，合并候选论文。
5. 实现正式发表状态验证及去重。
6. 开放 FastAPI 接口，并接入 Vue 检索页面。
7. 将新检索工具注册到第五课的 LangGraph Agent。

下面开始具体设计。

## 三、先改数据库：建立 Venue 管理体系

这一部分比调用搜索 API 更重要。

因为同一个会议在不同平台可能使用不同名称：

| 标准名称    | 其他常见写法                                                          |
| ------- | --------------------------------------------------------------- |
| NeurIPS | NIPS、Neural Information Processing Systems                      |
| CVPR    | IEEE/CVF Conference on Computer Vision and Pattern Recognition  |
| ACL     | Annual Meeting of the Association for Computational Linguistics |
| ICML    | International Conference on Machine Learning                    |

如果直接依赖字符串比较，就可能出现漏检、误判或重复记录。

### 3.1 新增四张表

建议在原有 PostgreSQL 数据库中增加：

| 表                    | 作用                          |
| -------------------- | --------------------------- |
| `venues`             | 保存会议、期刊的标准身份                |
| `venue_identifiers`  | 保存 OpenAlex、DBLP 等平台的 ID 映射 |
| `venue_rankings`     | 保存 CCF 分级及版本                |
| `paper_publications` | 保存一篇论文与其发表场所之间的关系及核验状态      |

数据关系如下：

venues

CVPR、ICML、ACL、TPAMI …

venue_identifiers

DBLP / OpenAlex ID

venue_rankings

CCF · 2026 · A/B/C

paper_publications

论文 ID · Venue ID · 年份 · 发表类别 · 验证状态

已有 papers 表

PaperRecord · DOI · OpenAlex ID

### 3.2 编写 SQLAlchemy 模型

创建 `storage/venue_models.py`：

```
        String(30), nullable=False    )    edition_year: Mapped[int] = mapped_column(        nullable=False    )    tier: Mapped[str] = mapped_column(        String(10), nullable=False    )    source_url: Mapped[str] = mapped_column(        Text, nullable=False    )class PaperPublicationRecord(Base):    __tablename__ = "paper_publications"    __table_args__ = (        UniqueConstraint(            "paper_id",            "venue_id",            "publication_year",            "track",        ),    )    id: Mapped[int] = mapped_column(        primary_key=True    )    paper_id: Mapped[int] = mapped_column(        ForeignKey("papers.id"),        nullable=False,    )    venue_id: Mapped[int] = mapped_column(        ForeignKey("venues.id"),        nullable=False,    )    publication_year: Mapped[int] = mapped_column(        nullable=False    )    track: Mapped[str] = mapped_column(        String(30),        default="unknown",        nullable=False,    )    verification_status: Mapped[str] = mapped_column(        String(30),        default="unverified",        nullable=False,    )    verification_url: Mapped[str | None] = (        mapped_column(Text)    )    verified_at: Mapped[datetime | None] = (        mapped_column(DateTime(timezone=True))    )
```

这里有三个关键设计。

`slug` 是我们系统内部的标准 ID。 例如 `cvpr`、`neurips`、`acl`。不要用可能重复的缩写直接充当数据库主键。

一个 Venue 可以映射多个外部 ID。 因为 OpenAlex 可能存在不同来源记录，DBLP 又有自己的会议流标识。

论文发表信息单独存储。 一篇论文可能先是 arXiv 预印本，再正式发表于某个会议。它的正式发表关系不应该被论文原来的 `url` 或 OpenAlex 来源字段覆盖。

### 3.3 创建新表

修改已有 `storage/init_db.py`，增加：

```
import storage.venue_models
```

保留原来的：

```
Base.metadata.create_all(bind=engine)
```

然后执行：

```
python -m storage.init_db
```

这次只新增表，不需要删除原来的任何文献数据。

由于目前新增的是独立表，`create_all()` 可以完成初始化。以后修改现有表的结构时，应使用 Alembic 数据库迁移。

## 四、建立标准 Venue 目录

我建议先不要一次导入所有 CCF 会议和期刊，而是先建立 10–20 个最常用 Venue，把端到端检索跑通。

例如：

```
INITIAL_VENUES = [    {        "slug": "cvpr",        "name": (            "IEEE/CVF Conference on "            "Computer Vision and Pattern Recognition"        ),        "acronym": "CVPR",        "venue_type": "conference",        "field": "computer_vision",        "official_url": "https://cvpr.thecvf.com/",    },    {        "slug": "icml",        "name": (            "International Conference "            "on Machine Learning"        ),        "acronym": "ICML",        "venue_type": "conference",        "field": "machine_learning",        "official_url": "https://icml.cc/",    },    {        "slug": "acl",        "name": (            "Annual Meeting of the Association "            "for Computational Linguistics"        ),        "acronym": "ACL",        "venue_type": "conference",        "field": "nlp",        "official_url": "https://www.aclweb.org/",    },]
```

然后从官方 CCF 2026 目录中，逐个填写等级到 `venue_rankings`。

对于 `venue_identifiers`，则逐个维护对应平台的准确 ID。

例如：

```
venue: CVPR

DBLP stream:
conf/cvpr

OpenAlex source:
通过 /sources 检索后人工确认的 S... ID
```

这里不能随便生成一个 `S...`，因为那是 OpenAlex 真实的实体 ID。

建议在这个阶段就区分 Venue 身份、CCF 等级和论文正式发表状态。 三者不能互相替代。

## 五、Step 2：接入 OpenAlex 的 Venue 定向检索

这是最适合先实现的功能，因为你第二课已经使用过 OpenAlex API。

### 5.1 根据 Venue 查找 OpenAlex Source ID

可以查询：

```
GET https://api.openalex.org/sources?search=CVPR
```

OpenAlex 的 `/sources` 接口可以检索期刊、会议及其他出版来源。

不过这里存在一个重要限制：OpenAlex 的 Source 是出版来源或会议系列，并非每个具体会议年份。部分会议论文可能被归入通用论文集系列，因此仅靠 Source ID 过滤可能漏掉论文。

[image](https://www.google.com/s2/favicons?domain=https://help.openalex.org\&sz=32)

OpenAlex Help Center



所以这一步要做的是候选检索，而不是最终发表核验。

### 5.2 实现新的检索适配器

创建 `tools/venue_search/openalex_adapter.py`：

```
import osimport reimport requestsdef search_openalex_venue(    query: str,    source_id: str,    start_year: int,    end_year: int,    limit: int = 20,) -> list[dict]:    """根据 OpenAlex Source ID 检索候选论文。"""    if not re.fullmatch(r"S\d+", source_id):        raise ValueError("无效的 OpenAlex Source ID")    if start_year > end_year:        raise ValueError("年份范围无效")    if not query.strip():        raise ValueError("检索关键词不能为空")    limit = max(1, min(limit, 100))    params = {        "search": query,        "filter": (            f"primary_location.source.id:{source_id},"            f"from_publication_date:{start_year}-01-01,"            f"to_publication_date:{end_year}-12-31"        ),        "per_page": limit,    }    api_key = os.getenv("OPENALEX_API_KEY")    if api_key:        params["api_key"] = api_key    response = requests.get(        "https://api.openalex.org/works",        params=params,        timeout=(5, 30),    )    response.raise_for_status()    data = response.json()    papers = []    for work in data.get("results", []):        primary = work.get(            "primary_location"        ) or {}        source = primary.get("source") or {}        papers.append({
```

### 5.3 运行测试

先从 OpenAlex `/sources` 查询到正确的会议或期刊 ID，然后执行：

```
papers = search_openalex_venue(    query="vision transformer",    source_id="S123456789",    start_year=2023,    end_year=2026,    limit=20,)for paper in papers:    print(        paper["title"],        paper["year"],        paper["source_name"],    )
```

`S123456789` 是占位示例，必须替换成已经核实的 ID。

如果成功，我们就获得了第一批按发表场所过滤的论文候选。

### 这里一定要注意

OpenAlex 的 `primary_location.source.id` 是正式支持的过滤字段，但它只描述主要发表位置。还可以检索 `locations.source.id` 等其他位置；后者可能包含预印本仓库，不能直接当作顶会录用依据。

[image](https://www.google.com/s2/favicons?domain=https://help.openalex.org\&sz=32)

OpenAlex Help Center



因此，此阶段返回的：

```
"verification_status": "indexed_unverified"
```

是有意设计的。

检索结果命中了某个 Venue，并不表示我们已经核实论文属于该会议主会论文集。

## 六、Step 3：接入 DBLP

DBLP 是计算机领域非常重要的补充数据源。

它尤其适合帮助我们处理会议缩写、论文记录、出版年份和发表场所之间的关联。

官方提供：

```
https://dblp.org/search/publ/api
https://dblp.org/search/venue/api
```

并支持 `q`、`h`、`f`、`format=json` 等参数。

[image](https://www.google.com/s2/favicons?domain=https://dblp.org\&sz=32)

dblp.org



创建：

`tools/venue_search/dblp_adapter.py`

```
import requestsdef search_dblp(    query: str,    limit: int = 50,) -> list[dict]:    """搜索 DBLP 中的计算机领域论文。"""    response = requests.get(        "https://dblp.org/search/publ/api",        params={            "q": query,            "h": min(max(limit, 1), 100),            "format": "json",        },        timeout=(5, 30),    )    response.raise_for_status()    data = response.json()    hits = (        data.get("result", {})        .get("hits", {})        .get("hit", [])    )    if isinstance(hits, dict):        hits = [hits]    results = []    for hit in hits:        info = hit.get("info", {})        results.append({            "title": info.get("title", ""),            "year": info.get("year"),            "venue_name": info.get("venue"),            "doi": info.get("doi"),            "dblp_url": info.get("url"),            "dblp_key": info.get("key"),            "provider": "dblp",            "verification_status":                "indexed_unverified",        })    return results
```

测试：

```
papers = search_dblp(    "vision transformer",    limit=30,)for paper in papers:    print(        paper["title"],        paper["venue_name"],        paper["year"],    )
```

随后使用本地 `Venue` 映射过滤候选结果。

例如，已确认 CVPR 对应 DBLP 会议流 `conf/cvpr`。不过 DBLP 官方说明：部分卫星 Workshop 也可能关联主会的会议流，因此仅匹配到 DBLP 会议流不能证明是主会论文。

[image](https://www.google.com/s2/favicons?domain=https://dblp.uni-trier.de\&sz=32)

dblp.uni-trier.de



还有一个局限：DBLP 的论文搜索主要基于检索词。如果只取前 30 或 50 条，再按 CVPR 过滤，可能遗漏很多符合条件的论文。

因此，第一版用它补充元数据；需要完整检索某年所有主会论文时，应优先读取对应年度的正式论文集或会议目录。

DBLP 对自动请求有限流措施，遇到 HTTP 429 时应遵守 `Retry-After`，并添加缓存、退避重试；官方建议避免高频请求。

[image](https://www.google.com/s2/favicons?domain=https://dblp.org\&sz=32)

dblp.org



## 七、Step 4：接入 Semantic Scholar

Semantic Scholar 比较适合提供补充搜索、引用量和关联论文信息。

它的 Paper Search API 支持按照 `venue`、`year` 筛选结果。

[image](https://www.google.com/s2/favicons?domain=https://api.semanticscholar.org\&sz=32)

Academic Graph API



创建 `tools/venue_search/semantic_scholar_adapter.py`：

```
import osimport requestsdef search_semantic_scholar(    query: str,    venue: str,    start_year: int,    end_year: int,    limit: int = 20,) -> list[dict]:    headers = {}    api_key = os.getenv(        "SEMANTIC_SCHOLAR_API_KEY"    )    if api_key:        headers["x-api-key"] = api_key    response = requests.get(        (            "https://api.semanticscholar.org"            "/graph/v1/paper/search"        ),        params={            "query": query,            "venue": venue,            "year": f"{start_year}-{end_year}",            "limit": min(max(limit, 1), 100),            "fields": (                "title,year,venue,externalIds,"                "citationCount,url"
```

在 `.env` 中可选添加：

```
SEMANTIC_SCHOLAR_API_KEY=你的密钥
```

官方说明部分接口允许无密钥访问，但会限流；如果要稳定集成到服务，建议申请 API Key 并处理 429。

[image](https://www.google.com/s2/favicons?domain=https://webflow.semanticscholar.org\&sz=32)

Semantic Scholar



### 三个搜索平台怎么配合？

我建议第一版采用以下策略：

| 场景             | 首选平台             | 辅助平台             |
| -------------- | ---------------- | ---------------- |
| 按研究方向找论文       | OpenAlex         | Semantic Scholar |
| 按会议、年份找论文      | OpenAlex + 官方论文集 | DBLP             |
| 补充 DOI、年份、发表场所 | DBLP             | OpenAlex         |
| 查看引用量和关联论文     | Semantic Scholar | OpenAlex         |
| 确认正式主会发表       | 官方论文集            | 其他索引作为辅助         |

不要把三个 API 的原始数据直接拼成前端结果。应先交给一个统一的 `VenueSearchService` 做清洗、关联和去重。

## 八、Step 5：实现正式发表验证

这是整个顶刊顶会检索模块中，最值得你投入精力的功能。

研究人员搜索“CVPR 2025 主会论文”，系统必须尽可能排除：

- 仅提交到 CVPR、尚未录用的论文。
- CVPR Workshop 论文。
- 只有 arXiv 预印本、没有主会发表记录的论文。
- 其他会议论文或同名论文。

### 8.1 为不同会议选择权威核验来源

| 会议                  | 主要核验来源                                                                        |
| ------------------- | ----------------------------------------------------------------------------- |
| NeurIPS             | [NeurIPS Proceedings](https://proceedings.neurips.cc/?utm_source=chatgpt.com) |
| ICML                | [PMLR](https://proceedings.mlr.press/?utm_source=chatgpt.com)                 |
| ICLR                | [OpenReview](https://openreview.net/?utm_source=chatgpt.com)                  |
| CVPR / ICCV         | [CVF Open Access](https://openaccess.thecvf.com/?utm_source=chatgpt.com)      |
| ACL / EMNLP / NAACL | [ACL Anthology](https://aclanthology.org/?utm_source=chatgpt.com)             |
| ECCV                | 对应年份的官方会议论文集                                                                  |
| AAAI / IJCAI        | 对应年份的官方 Proceedings                                                           |

其中 ACL Anthology 已提供正式的 Python 数据访问模块 `acl-anthology`，可以获取论文、卷和会议元数据，非常适合 NLP 方向的正式论文核验。

[image](https://www.google.com/s2/favicons?domain=https://aclanthology.org\&sz=32)

ACL Anthology



OpenReview 的 `venueid` 等字段可以帮助区分不同投稿状态，但需要根据具体会议年份检查配置；不能简单地认为“OpenReview 搜得到就是录用了”。

[image](https://www.google.com/s2/favicons?domain=https://docs.openreview.net\&sz=32)

OpenReview



### 8.2 给论文设置明确的验证状态

建议区分下面几种状态：

| 状态                   | 解释                        |
| -------------------- | ------------------------- |
| `unverified`         | 仅有搜索结果，尚未交叉核实             |
| `indexed_unverified` | DBLP/OpenAlex 等索引显示对应发表信息 |
| `official_verified`  | 在官方论文集或官方录用记录中完成身份核验      |

同时使用独立字段 `track`：

```
main
workshop
findings
demo
unknown
```

因此，即使一篇 Workshop 论文在官方目录中被找到，也可能是：

```
{
  "verification_status": "official_verified",
  "track": "workshop"
}
```

它不应该通过“只看顶会主会论文”的筛选。

### 8.3 核验方法

例如针对 CVPR，可按以下顺序处理：

1. 通过 OpenAlex、DBLP 等发现候选论文。
2. 获取 DOI、原始标题、作者及发表年份。
3. 查询该年度 CVF 官方论文目录。
4. 根据官方论文 ID、DOI，或标题加作者等信息匹配。
5. 核实论文所属主会或 Workshop 目录。
6. 保存 `verification_status`、`track` 和 `verification_url`。

匹配时尽量优先使用官方论文 ID、DOI。标题相似度只能辅助判断；若匹配存在歧义，应保持未核验状态。

## 九、Step 6：设计统一的检索服务

完成三个适配器以后，建议新增：

`services/venue_search_service.py`

它不应该重新发明学术检索功能，而是负责协调已有适配器。

VenueSearchService.search()

检索候选

OpenAlex / DBLP / S2

Venue 映射

缩写、外部 ID、CCF 等级

论文去重

DOI、OpenAlex ID 等

发表核验

官方论文集、Track

统一 VenuePaperResult

### 统一结果的数据格式

建议每个候选论文在 FastAPI 中返回：

```

{
  "title": "Example Research Paper",
  "year": 2025,
  "doi": "10.1234/example",
  "openalex_id": "https://openalex.org/W123456",
  "citation_count": 42,
  "venue": {
    "id": 1,
    "slug": "cvpr",
    "name": "CVPR",
    "type": "conference",
    "ranking": {
      "system": "CCF",
      "edition_year": 2026,
      "tier": "A"
    }
  },
  "publication": {
    "track": "unknown",
    "verification_status": "indexed_unverified",
    "verification_url": null
  },
  "providers": [
    "openalex",
    "dblp"
  ]
}

```

此 JSON 是接口结构示例，论文、DOI、排名字段和引用次数均为占位数据，并非已核实的记录。

这样 Vue 不需要知道论文来自 OpenAlex、DBLP 还是 Semantic Scholar，只需要统一地展示结果。

### 论文去重顺序

建议按照以下顺序执行：

1. DOI 归一化后精确匹配。
2. OpenAlex ID 精确匹配。
3. DBLP 记录 ID 或 Semantic Scholar ID 精确匹配。
4. 若以上标识缺失，使用标题、年份、作者进行辅助匹配。
5. 对存在歧义的候选保留独立记录，等待后续核实。

不要仅凭标题相似就合并论文。

特别是论文的预印本与正式出版版本，可能存在标题、作者或实验内容的差异。

你第三课现有的 `PaperRepository` 已经处理 DOI 和 OpenAlex ID 去重，所以我们可以继续复用它。对于缺少 DOI 和 OpenAlex ID 的 DBLP 结果，先进行元数据补全，再考虑存入正式文献库。

## 十、Step 7：通过 FastAPI 开放接口

我建议第一阶段新增三个接口。

| 接口                                    | 作用             |
| ------------------------------------- | -------------- |
| `GET /api/v1/venues`                  | 获取可以筛选的顶刊顶会目录  |
| `GET /api/v1/venues/{slug}/papers`    | 搜索指定会议或期刊的论文   |
| `GET /api/v1/papers/{id}/publication` | 查看论文的发表场所和核验状态 |

之后再增加批量官方核验和排名管理接口。

### FastAPI 接入示例

创建 `backend/app/api/venue.py`，根据你实际的后端包路径调整导入：

```
import osfrom fastapi import APIRouter, HTTPException, Queryfrom sqlalchemy import selectfrom storage.database import make_dbfrom storage.venue_models import (    VenueRecord,    VenueIdentifierRecord,)from tools.venue_search.openalex_adapter import (    search_openalex_venue,)router = APIRouter(    prefix="/api/v1/venues",    tags=["Venue Search"],)@router.get("/{slug}/papers")def search_venue_papers(    slug: str,    query: str = Query(min_length=2),    start_year: int = Query(2023, ge=1900),    end_year: int = Query(2026, ge=1900),    limit: int = Query(20, ge=1, le=100),):    if start_year > end_year:        raise HTTPException(            422, "年份范围无效"        )
```

注意：这只是第一阶段的 OpenAlex 单来源接入示例。它返回候选结果，还没有运行多来源聚合和官方验证；正式应用应增加上游 API 异常处理及统一错误响应。

另外，为了让示例容易理解，这里复用了 `make_db()` 创建连接。正式 FastAPI 项目建议在应用生命周期中初始化 SQLAlchemy Engine，并通过依赖注入获取 Session，避免每个 HTTP 请求都创建和释放连接池。

### 最终接口效果

前端可以调用：

```
GET /api/v1/venues/cvpr/papers?query=vision%20transformer&start_year=2024&end_year=2026
```

FastAPI 将执行：

`查询 Venue → 获取 OpenAlex Source ID → 搜索论文 → 返回标准化候选结果`

之后再把 DBLP、Semantic Scholar 和官方核验逻辑加入服务层，接口路径无需改变。

## 十一、Step 8：Vue 前端应该增加什么？

你已经决定使用 Vue 3 + TypeScript + FastAPI，我建议不要新增一个完全独立的检索系统，而是在现有“学术文献检索”页面中增加 普通检索 / 顶刊顶会检索 两个模式。

## ResearchPilot · 学术检索

综合检索

顶刊顶会检索

输入研究主题，例如 Vision Transformer

研究方向

计算机视觉

会议 / 期刊

CVPR

分级

CCF A · 2026

年份

2024–2026

检索结果

支持按发表状态筛选

Example Computer Vision Paper

CVPR · 2025 · DOI · 42 citations

CCF A（目录级别）

发表状态：索引记录，待核验

查看论文详情与原始来源

仅为 UI 示意，数据不代表真实检索结果。

### 页面中必须能区分两个概念

Venue 等级：例如 CCF 2026 A 类。

论文发表状态：例如“索引记录，待核验”“官方确认主会”“官方确认 Workshop”。

前端不要把这两个信息合并成一个“顶会认证”标签。

### 建议增加的前端文件

```
frontend/src/
├── api/
│   └── venue.ts
├── types/
│   └── venue.ts
├── components/
│   ├── VenueSelector.vue
│   ├── VenueRankFilter.vue
│   ├── PublicationStatus.vue
│   └── VenuePaperCard.vue
└── views/
    └── SearchView.vue
```

其中 `VenueSelector.vue` 支持按 AI、ML、CV、NLP 领域过滤会议和期刊，`PublicationStatus.vue` 专门显示论文的核验状态和官方链接。

## 十二、Step 9：让 LangGraph Agent 也支持顶刊顶会检索

这是 ResearchPilot 与普通学术搜索网站最大的区别之一。

用户不需要手动选择每个会议，也可以直接向 Agent 提问：

> 帮我查找 2024–2026 年 CVPR 和 ICCV 上与 Vision Transformer 有关的正式主会论文。

Agent 应将请求转换成结构化检索条件，调用新的 `search_top_venue_papers` 工具。

例如：

```
@tooldef search_top_venue_papers(    query: str,    venues: list[str],    start_year: int,    end_year: int,    verified_main_only: bool = True,) -> str:    """检索指定顶刊顶会中的相关论文。"""    ...
```

这个工具应当复用 FastAPI 背后的 `VenueSearchService`，而不是让 Agent 通过 HTTP 请求你自己的 FastAPI 接口。

当 `verified_main_only=True` 时，程序只能返回已经完成官方核验并且 `track="main"` 的结果；如果没有核验结果，就明确返回“暂未找到符合核验条件的论文”。

不要为了满足用户的“顶会”要求而把待核验候选自动升级成正式录用论文。

## 十三、建议的开发顺序与验收标准

不要同时开发全部平台。按下面四个阶段推进更容易排查问题。

01

Venue 数据库与 CCF 目录

建立四张新表，录入首批 10–20 个会议和期刊，并核实 CCF 2026 等级。

验收：能够查询某个 Venue 的标准名称、类别、CCF 等级及来源。

02

OpenAlex + DBLP + Semantic Scholar

实现三个适配器、统一返回结构、去重和异常处理。

验收：同一论文从多个平台返回时能够正确关联；缺失标识时不误合并。

03

正式发表与主会核验

优先实现 CVF、PMLR 和 ACL Anthology 的核验适配器，再扩展 NeurIPS 与 OpenReview。

验收：测试样例中的主会论文、Workshop 论文和仅投稿论文能够正确区分。

04

FastAPI + Vue + LangGraph 集成

提供筛选接口、结果列表、状态展示和 Agent 工具。

验收：能够从 Vue 检索指定顶会论文、核实发表状态、保存到 PostgreSQL，并继续做全文分析。

## 我建议你现在先做哪一步？

先完成第一阶段：Venue 数据库和会议映射。

这一步决定了后续检索的准确性，尤其是 OpenAlex Source ID、DBLP Stream、CCF 等级和官方论文集之间的对应关系。

第一阶段的具体完成标准是：

- PostgreSQL 存在 `venues`、`venue_identifiers`、`venue_rankings`、`paper_publications` 四张表。
- 至少录入 CVPR、ICCV、ECCV、NeurIPS、ICML、ICLR、ACL、EMNLP、AAAI、IJCAI 这十个会议。
- 逐项核实 CCF 2026 等级，不把未核实的等级直接写入。
- 建立并验证第一批 OpenAlex Source ID 与 DBLP Stream 映射。
- 通过测试确保主会与 Workshop 不会因为 Venue 名称相同而被错误归类。

完成后，我们就可以在现有 ResearchPilot 项目上实现一个真正可用的 `search_top_venue_papers()` 顶刊顶会定向检索工具，并让 Vue 前端和 LangGraph Agent 共用这一套能力。

这会比简单增加几个学术网站搜索接口更有价值：ResearchPilot 不仅能够找到相关论文，还能说明论文发表在哪里、该 Venue 属于哪个目录等级，以及发表状态是否经过核实。