from pathlib import Path
import os
import re
import json
import logging
import argparse
import pandas as pd
from pypdf import PdfReader
from dotenv import load_dotenv
import sys
import unicodedata
from difflib import get_close_matches
from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8")

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
    encoding="utf-8",
    format="%(asctime)s | %(levelname)s | %(message)s",
)

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

STRUCTURED_CASES_PATH = INTERIM_DIR / "structured_cases.json"
LLM_CASE_SUMMARIES_PATH = INTERIM_DIR / "llm_case_summaries.json"
FINAL_INSIGHTS_PATH = OUTPUT_DIR / "final_insights.json"

# =========================
# 2. PDF 섹션 패턴
# =========================
SECTION_PATTERNS = {
    "complaint_details": r"▣\s*민원내용\s*(.*?)(?=▣\s*쟁점|$)",
    "issue": r"▣\s*쟁점\s*(.*?)(?=▣\s*처리결과|$)",
    "decision": r"▣\s*처리결과\s*(.*?)(?=▣\s*소비자\s*유의사항|$)",
    "consumer_note": r"▣\s*소비자\s*유의사항\s*(.*?)(?=▣\s*참고자료|$)",
    "reference": r"▣\s*참고자료\s*(.*?)(?=$)",
}

# =========================
# 3. 유틸 함수
# =========================
def clean_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def save_json(data, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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

    if text.lower().endswith(".pdf"):
        text = text[:-4]

    replacements = {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "｢": "",
        "｣": "",
        "「": "",
        "」": "",
        "·": "",
        "･": "",
        "（": "(",
        "）": ")",
        "–": "-",
        "—": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\s+", " ", text).strip()
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

    if target in pdf_index:
        return pdf_index[target]

    for key, path in pdf_index.items():
        if target in key or key in target:
            return path

    candidates = get_close_matches(target, pdf_index.keys(), n=1, cutoff=0.85)
    if candidates:
        return pdf_index[candidates[0]]

    return None


def ensure_openai_client():
    if client is None:
        raise ValueError("OPENAI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")


# =========================
# 4. LLM 프롬프트
# =========================
CASE_SYSTEM_PROMPT = """
너는 보험 분쟁조정사례 분석 보조연구원이다.
입력된 사례 원문을 바탕으로 사실관계, 쟁점, 판단논리, 소비자 유의사항을 정확하게 정리해야 한다.
추측하지 말고, 제공된 정보에 근거하여 한국어로만 작성하라.
반드시 JSON 객체만 출력하라.
"""

INSIGHT_SYSTEM_PROMPT = """
너는 보험 분쟁조정사례를 메타 분석하는 연구보조원이다.
여러 사례 요약을 종합하여 반복 패턴, 소비자 오해, 판단 기준, 제도적 시사점을 한국어로 정리해야 한다.
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
    ensure_openai_client()
    prompt = build_case_prompt(case)

    response = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": CASE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )

    return json.loads(response.choices[0].message.content)


def generate_overall_insights(case_summaries: list[dict]) -> dict:
    ensure_openai_client()

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
        response_format={"type": "json_object"},
    )

    return json.loads(response.choices[0].message.content)


# =========================
# 5. Step 1
# PDF -> structured_cases.json
# =========================
def run_step1_extract_structured_cases() -> list[dict]:
    print("\n[STEP 1] PDF 원문 추출 및 구조화 시작")
    logging.info("[STEP 1] 시작")

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
                **sections,
            }

            structured_cases.append(case_dict)
            print(f"[완료] 구조화 성공: {row['case_no']} - {row['title']}")

        except Exception as e:
            logging.exception(f"구조화 실패 - case_no={row.get('case_no')}, error={e}")
            print(f"[오류] 구조화 실패: {row.get('case_no')} / {e}")

    save_json(structured_cases, STRUCTURED_CASES_PATH)

    print(f"[저장 완료] {STRUCTURED_CASES_PATH}")
    logging.info(f"[STEP 1] 완료 - {len(structured_cases)}건 저장")

    return structured_cases


# =========================
# 6. Step 2
# structured_cases.json -> llm_case_summaries.json
# =========================
def run_step2_generate_case_summaries() -> list[dict]:
    print("\n[STEP 2] 사례별 LLM summary 생성 시작")
    logging.info("[STEP 2] 시작")

    ensure_openai_client()

    if not STRUCTURED_CASES_PATH.exists():
        raise FileNotFoundError(
            f"{STRUCTURED_CASES_PATH} 파일이 없습니다. 먼저 Step 1을 실행하세요."
        )

    structured_cases = load_json(STRUCTURED_CASES_PATH)
    llm_case_summaries = []

    for idx, case in enumerate(structured_cases, start=1):
        try:
            summary = summarize_case(case)

            result = {
                "case_no": case.get("case_no"),
                "category_main": case.get("category_main"),
                "category_sub": case.get("category_sub"),
                "title": case.get("title"),
                "register_date": case.get("register_date"),
                "matched_pdf": case.get("matched_pdf"),
                **summary,
            }

            llm_case_summaries.append(result)
            print(f"[완료] summary 생성: {idx}/{len(structured_cases)} - {case.get('title')}")

        except Exception as e:
            logging.exception(f"summary 생성 실패 - case_no={case.get('case_no')}, error={e}")
            print(f"[오류] summary 생성 실패: {case.get('case_no')} / {e}")

    save_json(llm_case_summaries, LLM_CASE_SUMMARIES_PATH)

    print(f"[저장 완료] {LLM_CASE_SUMMARIES_PATH}")
    logging.info(f"[STEP 2] 완료 - {len(llm_case_summaries)}건 저장")

    return llm_case_summaries


# =========================
# 7. Step 3
# llm_case_summaries.json -> final_insights.json
# =========================
def run_step3_generate_final_insights() -> dict:
    print("\n[STEP 3] 전체 summary 기반 insight 생성 시작")
    logging.info("[STEP 3] 시작")

    ensure_openai_client()

    if not LLM_CASE_SUMMARIES_PATH.exists():
        raise FileNotFoundError(
            f"{LLM_CASE_SUMMARIES_PATH} 파일이 없습니다. 먼저 Step 2를 실행하세요."
        )

    llm_case_summaries = load_json(LLM_CASE_SUMMARIES_PATH)
    final_insights = generate_overall_insights(llm_case_summaries)

    save_json(final_insights, FINAL_INSIGHTS_PATH)

    print(f"[저장 완료] {FINAL_INSIGHTS_PATH}")
    logging.info("[STEP 3] 완료 - final_insights 저장")

    return final_insights


# =========================
# 8. 실행 함수
# =========================
def run_all():
    run_step1_extract_structured_cases()
    run_step2_generate_case_summaries()
    run_step3_generate_final_insights()
    print("\n[전체 완료] 1~3단계 파이프라인 실행 완료")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--step",
        type=int,
        choices=[1, 2, 3],
        help="특정 단계만 실행할 때 사용: 1, 2, 3",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        if args.step == 1:
            run_step1_extract_structured_cases()
        elif args.step == 2:
            run_step2_generate_case_summaries()
        elif args.step == 3:
            run_step3_generate_final_insights()
        else:
            run_all()

    except Exception as e:
        logging.exception(f"파이프라인 실행 실패: {e}")
        print(f"\n[치명적 오류] {e}")


if __name__ == "__main__":
    main()