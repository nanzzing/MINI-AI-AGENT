# Supabase 계정 및 조사 기록

## 환경 분리

Supabase에서 `mini-ai-dev`, `mini-ai-prod`라는 **서로 다른 프로젝트**를 만든다.
두 프로젝트는 같은 테이블 구조를 쓰지만 사용자와 대화 데이터는 공유하지 않는다.
무료 플랜의 용량/비활성 정책은 Supabase 최신 요금제에서 확인한다.

각 프로젝트의 SQL Editor에서 다음 파일을 **한 번씩** 실행한다.

`supabase/migrations/202611020001_research.sql` 

앞으로 스키마 변경은 새로운 migration 파일로 추가하고 개발에서 확인한 뒤 운영에 적용한다.
기존 migration을 다시 실행하거나 운영 데이터를 개발 DB에 복사하지 않는다.
배포 전에 별도 백업 정책도 정한다. 무료 플랜을 백업 서비스로 취급하지 않는다.

## 로컬 .env

Project Settings의 API 설정에서 Project URL과 **publishable key**를 확인한다.
`sb_publishable_`로 시작하는 새 키를 사용한다. secret/service_role 키는 사용하지 않는다.
  
```dotenv
APP_ENV=development
APP_ORIGIN=http://127.0.0.1:8765
DEV_SUPABASE_URL=https://DEV_PROJECT.supabse.co
DEV_SUPABASE_PUBLISHABLE_KEY=sb_publishable_.a..
```

다른 포트를 사용하면 APP_ORIGIN도 정확히 맞춘다. 생략하면 로컬 127.0.0.1과 실행 포트를 사용한다.
개발 URL/키를 모두 생략하면 기존 로컬 비로그인 임시 모드가 유지되며 기록은 저장되지 않는다.
둘 중 하나만 설정하면 시작에 실패한다. 운영 키를 로컬 .env에 넣을 필요는 없다.

## Render Environment

```dotenv
APP_ENV=production
APP_ORIGIN=https://mini-ai-agent-1.onrender.com
PROD_SUPABASE_URL=https://PROD_PROJECT.supabase.co
PROD_SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
```

APP_ORIGIN은 실제 주소이며 끝에 경로를 붙이지 않는다. 운영은 HTTPS만 허용한다.
Render에서는 개발 모드와 개발 키로의 fallback이 금지된다. 운영 설정 누락 시 서버 시작이 실패한다.
기존 OpenAI/시장 데이터 환경변수는 그대로 유지한다. 일반 계정은 ChatGPT 구독 연결이 아니며
Render의 모델 호출은 여전히 서버 OpenAI API 키를 사용한다.
수동으로 생성한 Render 서비스에는 render.yaml 값이 자동 적용되지 않을 수 있으므로 Environment를 확인한다.

## Auth 설정 (각 프로젝트 별도)

1. Authentication에서 Email provider를 켜고 Confirm email을 활성화한다.
2. URL Configuration의 Site URL을 개발/운영의 APP_ORIGIN과 동일하게 설정한다.
3. Confirm signup 이메일 템플릿을 **링크 대신 코드**로 변경한다: `인증 코드: {{ .Token }}`.
4. Reset password 이메일 템플릿도 `비밀번호 재설정 코드: {{ .Token }}`로 변경한다.
5. 공개 운영 전에 Custom SMTP를 설정한다. 기본 메일 발송기는 테스트용이며 수신자/발송량에 제한이 있다.
6. 가입 후 메일 코드를 사이트의 이메일 인증 화면에 입력하고 로그인한다.

개발/운영에서 계정도 별도로 가입한다. 비밀번호는 Supabase Auth가 관리하며 앱 DB에는 저장하지 않는다.

## 저장 및 복원 범위

- `research_threads`: 사용자 ID, 제목, 갱신 시각, 대화 맥락(최근 4턴), revision.
- `research_turns`: 성공한 질문/답변 한 쌍, 당시 근거 선언과 출처, 작성 시각.
- 전체 가격 이력, 재무 원본, 상세 도구 결과는 매 메시지에 중복 저장하지 않는다.
- 과거 대화는 최신 자료라고 취급하지 않는다. 이어서 질문하면 필요한 자료를 도구로 다시 확인한다.
- 삭제는 확인 후 진행하며 해당 조사의 메시지도 함께 삭제된다.
- 목록/과거 메시지는 30개 단위로 읽는다.
- 두 탭에서 동시에 질문하면 revision이 달라진 두 번째 저장을 거절하고 화면에 저장 실패를 표시한다.
- DB 장애 때 답변은 표시하되 저장 실패 경고를 띄운다. 이 경우 새로고침 전에 답변을 확인해야 한다.
- 실패한 모델 요청과 스트리밍 중 연결이 끊긴 요청은 완료된 대화로 저장하지 않는다.

## 보안과 운영 범위

DB 접근은 publishable key + **로그인한 사용자의 access token**을 사용한다.
두 테이블의 RLS는 `auth.uid() = user_id`를 강제한다. 관리 키로 RLS를 우회하지 않는다.
쿠키에는 임의 세션 ID만 저장하고 HttpOnly/SameSite=Strict/운영 Secure를 적용한다.
토큰은 서버 메모리에만 보관하므로 재배포/재시작 후 다시 로그인해야 한다. DB 기록은 유지된다.
현재 서버는 단일 인스턴스 전제다. 수평 확장 전에는 공유 세션 저장소와 분산 요청 제한을 추가해야 한다.
운영 계정 가입/AI 사용량 제한, 모니터링과 예산 설정도 별도로 관리해야 한다.

## 확인 순서

1. 개발 프로젝트에서 migration 적용 및 Auth/SMTP/이메일 템플릿 설정.
2. .env 설정 후 서버 재시작. 로그인/회원가입/이메일 인증/비밀번호 재설정 확인.
3. 계정 A로 질문 후 새로고침 및 서버 재시작, 재로그인하여 목록 확인.
4. 계정 B에서는 A의 기록이 보이지 않아야 한다. A의 조사 ID로 API 요청해도 404여야 한다.
5. 과거 조사 선택 후 후속 질문, 새로운 조사, 삭제 확인.
6. 운영 프로젝트에도 같은 migration과 Auth 설정을 적용한 다음 Render 환경변수 설정/배포.

로컬 단위 테스트는 외부 인증·메일·DB를 호출하지 않는다. 실제 Supabase 설정과 RLS는 위의 두 계정
시나리오로 추가 검증해야 한다. 키나 프로젝트 없이 실제 연결 성공을 확인할 수는 없다.
