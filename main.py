
import json
import requests

from pathlib import Path
from dotenv import load_dotenv

from tools.web_search import google_search
from tools.web_reader import read_webpage

from dataclasses import asdict
import json

def main():
    # 1. 读取 API 密钥
    load_dotenv()

    # 2. 获取用户的研究主题
    query = input("请输入检索主题（回车使用示例）：").strip()

    query = query or (
        "site:arxiv.org/abs/ "
        "medical image segmentation transformer"
    )

    # 3. 搜索相关网页
    results = google_search(query, limit=5)

    # 4. 创建输出目录
    output = Path("outputs")
    output.mkdir(exist_ok=True)

    # 5. 保存检索结果
    # asdict 把 dataclass 实例对象 递归转换成 Python 字典 `dict`
    data = [asdict(result) for result in results]
    with open(output /"search_results.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


    # 6. 在终端显示结果
    for i, paper in enumerate(data, 1):
        print(f"{i}. {paper['title']}")
        print(f"   {paper['url']}")

    print(f"搜索完成，获得 {len(results)} 条结果")

    # 7. 用 Jina 阅读第一条结果
    if results:
        url = data[0]["url"]
        try:
            content = read_webpage(url)
        except requests.exceptions.RequestException as exc:

            print(f"网页读取失败：{exc}")
        else:
            (output / "first_page.md").write_text(
                content, encoding="utf-8"
            )
            print("已保存网页内容到 outputs/first_page.md")


if __name__ == "__main__":
    main()
