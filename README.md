# BlackClip Guardrail

AI와 함께 만든 매매 전략을 실돈 넣기 전에 점검하고, 넣은 뒤에는 백테스트대로 가는지 매일 지켜보는 맥용 로컬 도구예요.

## 무엇을 하나요

- **점검**: 실전에 넣기 전에 재현성 · 미래참조 · 거래비용 · 시도 수 · 국면을 보고, 그 전략에 맞는 가드레일(정상 범위와 경고선)을 만들어요.
- **지켜보기**: 실전에 넣은 뒤에는 조회 전용 키로 체결을 받아 같은 기간 백테스트와 매일 맞춰 보고, 벗어나면 텔레그램으로 알려 줘요.
- **회고**: 이미 봇을 돌리고 있었다면 지난 석 달 체결로 「첫 회고」 한 장부터 보여 줘요.
- **로컬**: 키 · 체결 · 전략 코드는 이 맥에만 있어요. 화면은 이 컴퓨터(127.0.0.1)에서만 열려요.

주문은 내지 않아요. 경고만 하고 봇을 멈추지도 않아요.

[샘플 대시보드 보기](https://guardrail.blackclip.app/demo/): 지어낸 전략 넷으로 그린 읽기 전용 화면이에요. 설치 없이 먼저 볼 수 있어요.

## 필요한 것

- macOS(예약 작업에 launchd를 써요)
- [uv](https://docs.astral.sh/uv/)(파이썬 3.11 이상은 uv가 받아 줘요)
- [Claude Code](https://claude.com/claude-code)(6단계 전략 연결 파일을 만들 때 필요해요. 다른 명령도 대신 돌리고 결과를 설명해 줘요)
- 바이낸스 **조회 전용** API 키(현물과 선물). 주문이나 출금 권한이 켜진 키는 저장하지 않아요.
- 텔레그램 계정(매일 알림을 받으려면)

## 설치

엔진(`blackclip` 명령):

    uv tool install git+https://github.com/Noah-TaeHwan/blackclip-guardrail
    blackclip --version

`blackclip`을 못 찾는다고 나오면 `uv tool update-shell`을 돌리고 새 터미널을 열어요.

Claude Code 플러그인(스킬 하나와 훅 넷). 전략 레포 폴더에서 Claude Code를 열고(Claude Code 2.1.275 이상):

    /plugin install blackclip-guardrail --marketplace Noah-TaeHwan/blackclip-guardrail

터미널에서는 같은 일을 이렇게 해요(Claude Code 2.1.292 이상):

    claude plugin install blackclip-guardrail --marketplace Noah-TaeHwan/blackclip-guardrail --scope project

`--scope project`로 넣으면 그 레포에서만 돌아요. 기본값(user)은 모든 폴더에서 훅이 돌고, 퀀트 레포처럼 보이지 않는 폴더에서는 첫 셋업 안내를 내지 않아요.

## 처음 30분

전략 레포 폴더에서 돌려요. Claude Code에서 「셋업해 줘」라고 해도 돼요. 그때는 `blackclip setup`이 알려 주는 다음 할 일 순서로 가요.

1. 지금 어디까지 왔는지 보기

       blackclip scan .
       blackclip setup

   `setup`이 다음 할 일 하나를 알려 줘요. 폴더를 읽기만 하고 코드는 실행하지 않아요.

2. 조회 키 넣기: 바이낸스 API 관리에서 「조회」만 켠 키를 만든 뒤

       blackclip connect --open

   브라우저에 이 컴퓨터에서만 열리는 화면이 떠요. 키는 맥 키체인에 저장해요.

3. 체결 받기

       blackclip fetch --symbols BTCUSDT --since "$(date -u -v-90d +%Y-%m-%dT00:00:00Z)"

   `--since`는 처음 받을 시작 시각이에요(첫 회고가 지난 석 달이라 90일 전, `date -v-90d`는 맥 명령). 받은 체결은 `.blackclip/fills/`에 쌓여요.

4. 첫 회고

       mkdir -p .blackclip/strategies/<전략>
       blackclip retro --fills .blackclip/fills/binance-main/spot/fills.csv \
         --backtest <백테스트 거래 목록 CSV> --capital <실전 시작 잔고> \
         --live-start <실전 시작 ISO 시각> --days 90 \
         --end "$(date -u +%Y-%m-%dT00:00:00Z)" \
         --out .blackclip/strategies/<전략>/retro.md

   오늘 0시(UTC)까지 90일을 봐요. `--end`를 빼면 마지막 체결 다음 날까지를 봐요. 선물이면 경로의 `spot`을 `futures`로 바꾸고 `--funding .blackclip/fills/binance-main/futures/funding.csv`를 더해요.

   CSV 모양과 예시 결과는 [`examples/retro/`](https://github.com/Noah-TaeHwan/blackclip-guardrail/tree/main/examples/retro)에 있어요. 실행 경고(놓친 주문과 비싸게 된 체결)는 기본으로 꺼져 있어요. 첫 회고를 읽고 나서 켜요:

       blackclip notify --execution-alerts on

5. 텔레그램 잇기: 텔레그램 @BotFather에서 봇을 만들고 토큰을 받은 뒤

       blackclip connect --open --telegram
       blackclip connect --telegram

   두 번째 명령이 띄운 숫자를 봇에게 보내면 이어져요.

6. 전략 연결 파일: Claude Code에게 「이 전략 연결해 줘」라고 하면 `.blackclip/strategies/<전략>/link.py`를 만들어요. 엔진은 따로 띄운 프로세스에서 이 파일을 불러(맨 위 코드도 돌아요) 가격 읽기 `load_prices()`와 신호 함수 `signals()`를 불러요.

7. 점검 · 가드레일 · 실전 시작 · 매일 알림

       blackclip check <전략>
       blackclip save <전략>
       blackclip live <전략>
       blackclip schedule

   `schedule`은 매일 대조(이 맥 시간 기본 09:10, `--at HH:MM`으로 바꿈)와 1시간마다 보는 시계를 맥 예약 작업으로 걸어요. 그 시각에 맥이 잠들어 있으면 깨어날 때 한 번 돌아요. `blackclip schedule --remove`로 지워요.

## 대시보드

    blackclip dashboard

이 컴퓨터 브라우저에서 전략 목록 · 가드레일 · 회고를 보고, 「연결」에서 키와 텔레그램 토큰을 넣어요. 실행할 때마다 새 접속 토큰을 만들고 Ctrl+C로 꺼요. 기본 포트는 8765예요.

## 키와 데이터

- 바이낸스 키와 텔레그램 토큰은 맥 키체인에 저장해요. 맥의 `security` 명령을 찾지 못할 때만 나만 읽을 수 있는 파일(`~/.config/blackclip/secrets.json`)에 둬요.
- 체결 · 회고 · 매일 대조 기록은 레포 안 `.blackclip/`에 있어요. Git에는 전략 기록(점검 · 가드레일 · 기록 · 연결 코드)만 올라가고, 체결 · 연결 상태 · 이력은 `.blackclip/.gitignore`로 빠져요.
- 밖으로 나가는 요청은 바이낸스 API(계좌 조회와 공개 봉), 텔레그램 봇 API, 직접 넣은 헬스체크 주소뿐이에요.

## 투자 고지

- 이 도구는 투자 자문이 아니에요. 점검 결과 · 가드레일 · 경고는 참고용 숫자이고, 수익을 보장하지 않아요.
- 투자 결정과 그 결과(손실 포함)의 책임은 쓰는 사람에게 있어요.
- 경고만 하고 주문을 내거나 봇을 멈추지 않아요. 멈출지는 직접 정해요.
- 조회 전용 키만 받아요. 주문 · 출금 권한이 켜진 키는 저장하지 않아요.
- 백테스트와 실전은 다를 수 있고, 거래소 응답이 늦거나 틀리면 결과도 틀릴 수 있어요.
- BlackClip Guardrail은 바이낸스 · 텔레그램 · Anthropic과 제휴하거나 그 보증을 받은 도구가 아니에요.

## 라이선스

소스 공개(FSL-1.1-ALv2), 각 버전은 나온 지 2년 뒤 Apache-2.0으로 바뀌어요.
