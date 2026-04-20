# 📖 보험 분쟁 사례 분석 자동화 프로젝트 (LLM-based)

본 프로젝트는 금융분쟁조정위원회의 보험 분쟁 사례 PDF를 기반으로, LLM을 활용하여 비정형 데이터를 구조화하고 인사이트를 도출하는 데이터 파이프라인입니다.

---

## 📁 프로젝트 구조

```text
project/
├── data/
│   ├── interim/             # 중간 처리 데이터 (JSON)
│   │   ├── structured_cases.json
│   │   └── llm_case_summaries.json
│   └── output/              # 최종 분석 결과 (Excel/JSON)
│       ├── case_summary_table.xlsx
│       ├── final_insights.json
│       └── overall_insights_table.xlsx
├── pdf/                     # 원본 PDF 파일 폴더
├── main.py                  # 메인 실행 스크립트
├── dispute case list.xlsx   # 분석 대상 리스트
├── .env                     # API Key 설정 파일
└── README.md
```

## 📁 주요 파일 상세
### 1. Data (Interim & Output)
* **`structured_cases.json`**: PDF에서 텍스트를 추출하고 구조화 데이터 (민원, 쟁점, 결과 등)
* **`llm_case_summaries.json`**: LLM이 분석한 핵심 논리, 약관 근거, 분쟁 태그 및 사례별 분석 결과
* **`final_insights.json`**: 전체 사례를 종합 분석하여 도출한 반복 패턴 및 정책적 시사점

### 2. 핵심 실행 파일
* **`main.py`**: [PDF 매칭 → 텍스트 추출 → 구조화 → LLM 요약 → 인사이트 도출] 전 과정 제어
* **`.env`**: `OPENAI_API_KEY` 등 환경 변수 관리 파일

## 🔄 파이프라인 흐름
### Step 1: PDF → 구조화 데이터 생성
```
PDF → 텍스트 추출 → 섹션 파싱 → structured_cases.json
```
* pypdf 기반 텍스트 추출
* 정규표현식으로 민원/쟁점/결과 분리


### Step 2: 사례별 LLM 요약 생성
```
structured_cases.json → LLM 분석 → llm_case_summaries.json
```
* 사례별 핵심 쟁점 및 판단 논리 도출
* 약관 기반 판단 근거 추출
* 분쟁 태그 및 키워드 생성

### Step 3: 전체 인사이트 도출
```
llm_case_summaries.json → 메타 분석 → final_insights.json
```
* 반복 분쟁 패턴 분석
* 소비자 오해 및 보험사 판단 기준 정리
* 정책 및 실무적 시사점 도출


## 🔍 주요 기능
### 1. 지능현 pdf 매칭
* 파일명 유사도 기반 매칭 (difflib)
* 유니코드/특수문자 정규화 처리

### 2. 정밀 텍스트 구조화
* 정규표현식을 활용한 섹션 단위 분리
* 비정형 PDF → 구조화 JSON 변환

### 3. LLM 기반 심층 분석
* 단순 요약이 아닌 판단 논리, 약관 해석, 분쟁 태그까지 추출

### 4. 메타 인사이트 도출
* 반복 분쟁 유형 분석
* 소비자 행동 패턴 도출
* 정책/상품 설계 시사점 제공

## ⚙️ 실행 방법
### 1. 환경 구성 및 패키지 설치
``` text
python -m venv venv
# Windows
venv\Scripts\activate
# 패키지 설치
pip install pandas openpyxl pypdf python-dotenv openai
```

### 2. API Key 설정
루트 디렉토리에 .env 파일을 생성하고 키를 입력합니다.
``` text
OPENAI_API_KEY=your_api_key_here
```

### 3. 실행
``` text
python main.py
```

## ⚠️ 주의사항
* 매칭 실패: 파일명이 크게 다를 경우 unmatched_pdf_cases.xlsx와 로그를 확인하십시오.
* PDF 상태: 스캔본(이미지 형식) PDF는 텍스트 추출이 원활하지 않을 수 있습니다.
* 비용 관리: OpenAI API 사용량에 따른 비용이 발생할 수 있습니다.