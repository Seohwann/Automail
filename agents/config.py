"""공통 설정: 환경변수 로드 + LLM 팩토리.

환경변수(.env):
- GOOGLE_API_KEY : Gemini(AI Studio) 키. ChatGoogleGenerativeAI 가 자동으로 읽는다.
- GEMINI_MODEL   : 사용할 모델명 (기본 gemini-3.1-flash-lite, 무료 티어).

주의: 여기서 쓰는 GOOGLE_API_KEY(=Gemini)와
google_clients.py 의 OAuth(credentials.json/token.json, Sheets·Gmail)는 서로 다른 인증이다.
"""
import os

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


def get_llm(temperature: float = 0.3) -> ChatGoogleGenerativeAI:
    """에이전트들이 공유하는 LLM 인스턴스를 만든다."""
    if not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError("GOOGLE_API_KEY 가 설정되지 않았습니다 (.env 를 확인하세요).")
    return ChatGoogleGenerativeAI(model=GEMINI_MODEL, temperature=temperature)
