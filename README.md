# Case Finder

Slack 개인 대화에서 질문을 받고, **질문자 본인의 권한으로** Slack·Jira·Gmail 업무 기록을 읽어 근거와 함께 답하는 읽기 전용 앱이다.

## 지금 구현된 범위

- Slack 이벤트 서명 확인 및 중복 이벤트 방지
- Slack 개인 대화만 처리 (채널 멘션은 처리하지 않음)
- Slack·Jira·Gmail의 사용자별 OAuth 연결 흐름
- OAuth 토큰 Fernet 암호화 저장, 질문·원문 본문 미저장
- Slack 공개 채널은 허용, 비공개 채널·DM·그룹 DM은 질문자가 현재 구성원일 때만 사용하도록 차단
- Jira 이슈·댓글·상태 이력 검색용 연결부
- Gmail 읽기 전용 검색·스레드 읽기 연결부
- 근거 없는 AI 답변을 막는 기본 답변 규칙. LLM이 연결되지 않으면 결론을 꾸며내지 않고 근거 후보만 보임

## 의도적으로 제외한 것

- 이슈 수정, 상태 변경, 메일 발송·삭제
- 첨부파일, 이미지, OCR, 외부 문서 자동 열람
- 채널 멘션 질의응답
- 질문·앱 답변을 공통 업무 근거로 재수집하는 동작

## 로컬 실행

Python 3.11 이상을 권장한다. 현재 개발 컴퓨터는 Python 3.9이므로, 배포 컨테이너는 Python 3.12를 사용한다.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app_from_env --factory --host 127.0.0.1 --port 8000
```

`.env`에는 실제 비밀값을 넣지만 채팅, 저장소, 로그에는 넣지 않는다. 실행 전 `APP_BASE_URL`은 공개 HTTPS 주소로 바꿔야 한다. 로컬만 테스트할 때는 `http://127.0.0.1:8000`을 쓸 수 있다.

## 외부 서비스 설정 (배포 직전에 한 번만)

1. Slack 앱의 Event Subscriptions Request URL을 `https://배포주소/slack/events`로 설정하고, DM 메시지 이벤트를 구독한다. 기존 사용자 토큰 권한은 유지하되, 채널 멘션은 첫 버전에서 구독하지 않는다.
2. Slack, Google, Atlassian OAuth 앱에 각 콜백 주소를 등록한다.
   - `/oauth/slack/callback`
   - `/oauth/gmail/callback`
   - `/oauth/jira/callback`
3. Gmail은 테스트에 쓴 데스크톱 OAuth 클라이언트와 별도로 **웹 애플리케이션** OAuth 클라이언트를 만든다.
4. Jira는 사용자별 OAuth(3LO)로 연결한다. 서비스 계정이나 관리자 권한으로 모든 이슈를 읽는 구조로 만들지 않는다.
5. Google Workspace 관리자에게 공용 메일 주소가 전달/그룹/사서함 위임 중 어느 방식인지 확인한다. 확인된 범위 밖 메일은 검색 대상에 넣지 않는다.

## 테스트

```sh
.venv/bin/python -m pytest -q
```

테스트는 Slack 서명 검증, OAuth state 재사용 방지, 암호화 저장, 비공개 대화 차단 규칙을 확인한다. 실제 업무 내용은 테스트에 사용하지 않는다.

## 배포 전 확인할 제한

- Slack 미참여 비공개 채널 접근 차단은 실제 다른 사용자와 최종 검증이 필요하다.
- Jira 권한은 연결한 사용자 계정 기준으로 API가 허용한 자료만 사용한다.
- `gmail.readonly`는 Google의 제한적 범위다. 외부 사용자에게 배포하거나 서버에 관련 데이터를 보관하는 방식은 Google Workspace 정책과 OAuth 검증 요건을 확인해야 한다.
- Gmail REST API가 안정적인 원문 웹 링크를 보장하지 않는 경우, 앱은 링크를 지어내지 않고 가장 가까운 메일 식별 정보만 표시해야 한다.

## Render deployment note

For Render, use `STORAGE_BACKEND=firestore` and set `GOOGLE_SERVICE_ACCOUNT_JSON_B64`
as a **secret environment variable**. Its value is a base64-encoded JSON key for a
dedicated service account that has only `roles/datastore.user` and
`roles/secretmanager.secretAccessor` in `case-finder-test`. Do not commit the key,
put it in `.env`, or paste it into Slack.
