
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from agent.citation_guard import citation_report
from agent.research_tools import make_research_tools
from agent.workflow import build_research_agent


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")

    database_url = os.environ["DATABASE_URL"]

    # 1. 初始化大语言模型
    llm = ChatOpenAI(
        model=os.environ["LLM_MODEL"],
        api_key=os.environ["LLM_API_KEY"],
        base_url=(
            os.getenv("LLM_BASE_URL") or None
        ),
        use_responses_api=False,
        timeout=60,
        max_retries=2,
    )

    # 2. 加载已有科研工具
    tools, engine = make_research_tools(
        database_url
    )

    # 3. 构建 Agent
    graph = build_research_agent(
        llm,
        tools,
    )

    try:
        question = input(
            "请输入科研问题："
        ).strip()

        if not question:
            print("科研问题不能为空")
            return

        # 4. 执行 Agent
        result = graph.invoke(
            {
                "messages": [
                    HumanMessage(content=question)
                ]
            },
            config={
                "recursion_limit": 14
            },
        )

        # 5. 获取模型最终回答
        answer = str(
            result["messages"][-1].content
        )

        # 6. 校验引用编号
        report = citation_report(
            answer,
            result["messages"],
        )

        if report["invalid"]:
            print(
                "引用校验失败，暂不展示回答：",
                report["invalid"],
            )
            return

        print("\nResearchPilot 回答：")
        print(answer)

        if report["has_evidence_without_citation"]:
            print(
                "\n警告：检索到了原文，"
                "但回答未附证据编号。"
            )

        elif not report["allowed"]:
            print(
                "\n提示：本轮没有获得"
                "可引用的本地原文证据。"
            )

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
