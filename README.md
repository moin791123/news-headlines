# 한줄뉴스

경제·부동산·주식 위주로 국내외 주요 뉴스를 **제목 한 줄**로 모아 보는 페이지입니다.
제목을 누르면 원래 기사로 이동합니다.

- 분야: 국내 경제, 부동산, 주식·증시, 해외 경제·증시, 해외 주요뉴스, 연예, 사회·이슈, 유럽 축구(EPL·라리가·분데스리가·리그앙)
- 기사 옆 ✕(관심없음)를 누르면 그 분야의 비중과 제목에 들어간 단어의 점수가 낮아져, 비슷한 기사가 점점 덜 보입니다. 기사를 눌러 읽으면 그 분야 비중이 조금 올라갑니다. 학습 내용은 각 기기의 브라우저에 저장되며 `내 취향`에서 확인하거나 초기화할 수 있습니다.
- 뉴스는 구글 뉴스 RSS에서 GitHub Actions가 30분마다 받아와 GitHub Pages에 올립니다. 빨간 점은 여러 언론이 함께 다루는 화제 기사입니다.

## 구성

- `site/index.html` 페이지 (폰·PC 반응형)
- `scripts/fetch_news.py` 뉴스 수집 (파이썬 표준 라이브러리만 사용, 분야와 검색어는 파일 위쪽 `FEEDS`에서 바꿀 수 있습니다)
- `.github/workflows/update.yml` 30분마다 수집 후 배포

## 처음 설정

저장소 Settings → Pages → Build and deployment → Source 를 **GitHub Actions** 로 두고, Actions 탭에서 `뉴스 갱신 및 배포`를 한 번 실행하면 됩니다.

## 내 컴퓨터에서 실행

```bash
python3 scripts/fetch_news.py
python3 -m http.server -d site 8000   # http://localhost:8000
```
