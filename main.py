from pathlib import Path
import os
import re
import json
import logging
import pandas as pd
from pypdf import PdfReader
from openai import OpenAI
from dotenv import load_dotenv
import sys
sys.stdout.reconfigure(encoding='utf-8')
import unicodedata
from difflib import get_close_matches

# =========================
# 1. 기본 설정
# =========================
BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR
PDF_DIR = BASE_DIR / "pdf"
INTERIM_DIR = BASE_DIR / "data" / "interim"
OUTPUT_DIR = BASE_DIR / "data" / "output"

INTERIM_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=OUTPUT_DIR / "pipeline.log",
    level=logging.INFO,
    encoding="utf-8"
)

load_dotenv()  # .env 파일 로드

from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# =========================
# 2. PDF 섹션 패턴
# =========================
SECTION_PATTERNS = {
    "complaint_details": r"▣\s*민원내용\s*(.*?)(?=▣\s*쟁점|$)",
    "issue": r"▣\s*쟁점\s*(.*?)(?=▣\s*처리결과|$)",
    "decision": r"▣\s*처리결과\s*(.*?)(?=▣\s*소비자\s*유의사항|$)",
    "consumer_note": r"▣\s*소비자\s*유의사항\s*(.*?)(?=▣\s*참고자료|$)",
    "reference": r"▣\s*참고자료\s*(.*?)(?=$)"
}

# =========================
# 3. 유틸 함수
# =========================
def clean_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def load_metadata(xlsx_path: str) -> pd.DataFrame:
    df = pd.read_excel(xlsx_path)
    df.columns = [c.strip() for c in df.columns]

    required_cols = [
        "case_no",
        "category_main",
        "category_sub",
        "title",
        "register_date",
        "download_link",
        "view_count",
    ]

    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise ValueError(f"필수 컬럼 누락: {missing_cols}")

    return df

def extract_text_from_pdf(pdf_path: str) -> str:
    reader = PdfReader(pdf_path)
    texts = []
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            texts.append(page_text)
    return "\n".join(texts).strip()

def parse_sections(raw_text: str) -> dict:
    result = {}
    for key, pattern in SECTION_PATTERNS.items():
        match = re.search(pattern, raw_text, flags=re.DOTALL)
        result[key] = clean_text(match.group(1)) if match else ""
    return result

def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.strip()

    # 확장자 제거
    if text.lower().endswith(".pdf"):
        text = text[:-4]

    # 자주 문제되는 따옴표/괄호/점류 통일
    replacements = {
        "“": '"', "”": '"', "‘": "'", "’": "'",
        "｢": "", "｣": "",
        "「": "", "」": "",
        "·": "", "･": "",
        "（": "(", "）": ")",
        "–": "-", "—": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # 공백 정리
    text = re.sub(r"\s+", " ", text).strip()

    # 파일명 비교용: 특수문자 많이 제거
    text = re.sub(r"[\"'[\]{}()<>]", "", text)
    text = re.sub(r"[,:;/\\|?*]", "", text)

    return text.lower()

def build_pdf_index(pdf_dir: Path) -> dict:
    pdf_index = {}
    for pdf_path in pdf_dir.glob("*.pdf"):
        original_stem = pdf_path.stem
        normalized_stem = normalize_text(original_stem)
        pdf_index[normalized_stem] = pdf_path
    return pdf_index

def match_pdf_path(download_link: str, pdf_index: dict) -> Path | None:
    target = normalize_text(download_link)

    # 1) 완전 일치
    if target in pdf_index:
        return pdf_index[target]

    # 2) 포함 관계 매칭
    for key, path in pdf_index.items():
        if target in key or key in target:
            return path

    # 3) 유사도 매칭
    candidates = get_close_matches(target, pdf_index.keys(), n=1, cutoff=0.85)
    if candidates:
        return pdf_index[candidates[0]]

    return None

# =========================
# 4. LLM 프롬프트
# =========================
CASE_SYSTEM_PROMPT = """
너는 보험 분쟁조정사례 분석 보조연구원이다.
입력된 사례 원문을 바탕으로 사실관계, 쟁점, 판단논리, 소비자 유의사항을 정확하게 정리해야 한다.
추측하지 말고, 제공된 정보에 근거하여 한국어로만 작성하라.
반드시 JSON 객체만 출력하라.
"""

def build_case_prompt(case: dict) -> str:
    return f"""
다음 보험 분쟁조정사례를 분석하라.

[메타데이터]
번호: {case.get("case_no")}
대분류: {case.get("category_main")}
소분류: {case.get("category_sub")}
제목: {case.get("title")}
등록일: {case.get("register_date")}
조회수: {case.get("view_count")}

[본문]
민원내용: {case.get("complaint_details")}
쟁점: {case.get("issue")}
처리결과: {case.get("decision")}
소비자 유의사항: {case.get("consumer_note")}
참고자료: {case.get("reference")}

아래 JSON 형식으로만 한국어로 답하라.
{{
  "summary_short": "3문장 이내 요약",
  "fact_pattern": "사실관계 요약",
  "core_issue": "핵심 쟁점",
  "decision_summary": "처리결과 요약",
  "decision_reasoning": "판단 논리",
  "legal_basis": "약관/참고자료 기반 판단 근거",
  "consumer_caution": "소비자 유의사항 요약",
  "insurance_keywords": ["키워드1", "키워드2"],
  "dispute_tags": ["분쟁태그1", "분쟁태그2"],
  "insight_point": "이 사례가 보여주는 핵심 인사이트"
}}
"""

def summarize_case(case: dict) -> dict:
    prompt = build_case_prompt(case)

    response = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": CASE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"}
    )

    result = json.loads(response.choices[0].message.content)
    return result

# =========================
# 5. 전체 인사이트 생성
# =========================
INSIGHT_SYSTEM_PROMPT = """
너는 보험 분쟁조정사례를 메타 분석하는 연구보조원이다.
여러 사례 요약을 종합하여 반복 패턴, 소비자 오해, 판단 기준, 제도적 시사점을 한국어로 정리해야 한다.
반드시 JSON 객체만 출력하라.
"""

def generate_overall_insights(case_summaries: list[dict]) -> dict:
    prompt = f"""
다음은 보험 분쟁조정사례별 요약 결과이다.
전체 사례를 종합해 반복 패턴과 시사점을 도출하라.

[사례요약목록]
{json.dumps(case_summaries, ensure_ascii=False, indent=2)}

반드시 아래 JSON 형식으로만 한국어로 답하라.
{{
  "top_dispute_types": ["가장 빈번한 분쟁 유형들"],
  "repeated_consumer_misunderstandings": ["소비자 오해 반복 포인트"],
  "repeated_decision_logic": ["반복되는 판단 논리"],
  "important_terms": ["자주 등장하는 핵심 용어"],
  "policy_implications": ["제도/약관/상품설계 시사점"],
  "practical_implications_for_insurers": ["보험사 실무 시사점"],
  "practical_implications_for_consumers": ["소비자 안내 시사점"],
  "research_insights": ["학술/정책 연구 관점 인사이트"]
}}
"""

    response = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"}
    )

    return json.loads(response.choices[0].message.content)

# =========================
# 6. 메인 파이프라인
# =========================
def main():
    xlsx_path = RAW_DIR / "dispute case list.xlsx"
    df = load_metadata(str(xlsx_path))

    pdf_index = build_pdf_index(PDF_DIR)

    structured_cases = []

    for _, row in df.iterrows():
        try:
            pdf_path = match_pdf_path(row["download_link"], pdf_index)

            if pdf_path is None:
                logging.warning(f"PDF not found: {row['download_link']}")
                print(f"[경고] PDF 파일을 찾을 수 없음: {row['download_link']}")
                continue

            raw_text = extract_text_from_pdf(str(pdf_path))
            sections = parse_sections(raw_text)

            case_dict = {
                "case_no": row["case_no"],
                "category_main": row["category_main"],
                "category_sub": row["category_sub"],
                "title": row["title"],
                "register_date": str(row["register_date"]),
                "download_link": row["download_link"],
                "matched_pdf": pdf_path.name,
                "view_count": row["view_count"],
                **sections
            }

            structured_cases.append(case_dict)
            print(f"[완료] 본문 구조화 성공: {row['case_no']} - {row['title']}")

        except Exception as e:
            logging.exception(f"구조화 실패 - case_no={row.get('case_no')}, error={e}")
            print(f"[오류] 구조화 실패: {row.get('case_no')} / {e}")

if __name__ == "__main__":
    main()