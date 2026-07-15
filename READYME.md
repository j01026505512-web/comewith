# Binance Smart Trader

## 1. 프로젝트 개요
SMC(Smart Money Concepts) 로직을 기반으로 바이낸스 선물 거래를 자동화하는 파이썬 시스템입니다. AWS 환경에서 24시간 무중단 운영을 목표로 설계되었습니다.

## 2. 주요 기능
- **SMC 매매 전략:** 거래량 분석을 통한 디멘드/서플라이 존 탐색.
- **리스크 관리:** 격리 마진(Isolated) 강제 및 레버리지 자동 조절.
- **안정성:** 예외 처리(try-except) 및 실시간 텔레그램 모니터링.

## 3. 설치 및 설정
1. 파이썬 가상환경 생성 및 라이브러리 설치:
   `pip install -r requirements.txt`
2. `.env` 파일을 복사하여 `.env.template`를 만들고 키를 입력하세요.
3. 시스템 테스트 모드 실행:
   `python bin_aws.py`

## 4. 폴더 구조
- `bin_aws.py`: 핵심 백엔드 매매 로직
- `bin_gui.py`: Tkinter 기반 모니터링 GUI
- `trade_history.json`: 매매 기록 저장소

## 5. 면책 조항
본 코드는 연구용입니다. 실거래 시 발생하는 모든 자산 손실에 대한 책임은 사용자 본인에게 있습니다.