v1.0

# 1. regulation_parser.py 실행

- (결과1) 우리대학 규정을 게시한 공개 URL로 부터 모든 규정파일을 pdf_cache 폴더에 캐싱
- (결과2) 모든 규정을 파싱하고 regulations.db, regulations_delete.db에 저장 
a. schema는 verify_db.py 참조
b. 재검색시 최종게정일이 변경된 경우에만 db 업데이트
c. 신규 규정의 경우 db insert
d. 삭제 db의 경우 rebulations.db에서 delete하고 regulations_delete.db에 추가
- (결과3) 주제별 클러스터파일(주제별 클러스트.txt)을 참조하여 regulationsByCluster 폴더 내부에 클러스터별로 규정을 통합하여 저장 (이때 파일용량이 일정이상의 경우 파일을 분리하여 작성 (numbering)) - 향후 이 파일이 Agent로 순차적으로 전송되는 데이터임

** 1달에 1번 규정이 업데이트 되는 경우 regulation_parser.py를 실행하여 regulationsByCluster 폴더를 재생성하고 변경된 규정의 카테고리에 대해서 재분석 필요

# 2. streamlit_dashboard

- (동작1) 분석대상이 기준이 되는 규정(regulation_parser.py 또는 제개정규정)을 순차적으로 Agent에 전송하고 json형태로 리턴받아 화면에 맵핑함
** json을 파싱하는 과정에서 LLM이 부가 설명문구등을 삽입하여 json형식이 깨지는 경우 있어서 이에 대해 시스템 프롬프트를 매우 엄격하게 적용함

- (동작2) 현행규정의 경우 분할된 클러스터단위의 규정을 API로 전달하며, 에이전트는 전달된 데이터를 라인바이라인으로 읽어와 대학규정/상위규정과 비교하여 정합성을 분석함 (법제처 API가 적절히 동작하고 있지 않아 RAG로 구성함)


