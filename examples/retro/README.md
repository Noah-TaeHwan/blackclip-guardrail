# 회고 예시 (지어낸 값)

실전 시작 9/23, 10/13 매도가 실제 체결에 없고(놓친 주문으로 추정), 10/08 매수는 두 번에 나눠 조금 비싸게 체결됐다. 자본 37000 USDT는 놓친 매도 0.12 BTC가 자본의 약 20%가 되게 잡았다. 시각은 UTC라 한국 시각 10/13 15:58이 06:58로 찍힌다. `--end`는 기간 끝만 정하고, `--fills-until`은 체결 파일에 그 시각 전의 체결이 모두 담겼다는 고객의 말이다. `--fills-until`을 주면 그 시각 전의 짝 없는 백테스트 거래를 놓친 주문(추정)으로 쓰고, 안 주면 마지막 체결 날 뒤의 짝 없는 백테스트 거래는 봇이 멈췄는지 체결 파일이 끝났는지 구분할 수 없어 「마지막 체결 뒤(구분 불가)」로 쓴다.

    uv run blackclip retro --fills examples/retro/fills.csv --backtest examples/retro/backtest.csv --funding examples/retro/funding.csv --capital 37000 --live-start 2026-09-23T00:00:00+00:00 --end 2026-10-15T00:00:00+00:00 --fills-until 2026-10-15T00:00:00+00:00

고객 백테스트가 거래 시각을 봉 시작(일봉이면 0시)으로 적으면 `--window 1d`를 붙인다.
