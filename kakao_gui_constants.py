"""Shared immutable Hub presentation and screen guard constants."""
from typing import Final

APP_NAME = "카카오톡 Hub"

APP_VERSION = "2.5.0"

MIN_WINDOW_SIZE = (1024, 720)

UNBOUNDED_WINDOW_SIZE = (10000, 10000)

FAILED_RETRY_INSTRUCTION: Final[str] = (
    "상단 [직접 실패 2차 전송] 버튼으로 직접 전송에 명시적으로 실패한 행만 "
    "PC 화면 방식으로 다시 보낼 수 있습니다."
)

ATTACHMENT_EMPTY_LABEL: Final[str] = "첨부 파일 없음"

SEARCH_EMPTY_STATUS: Final[str] = "검색어를 입력하세요"

SCHEDULE_PICKER_DAYS: Final[int] = 366

SCHEDULE_PICKER_MINUTES: Final[tuple[int, ...]] = tuple(range(0, 60, 5))

KOREAN_WEEKDAYS: Final[str] = "월화수목금토일"

SUCCESS_RESULT_STATUSES: Final[frozenset[str]] = frozenset({"sent", "pasted", "scheduled", "schedule_acknowledged", "direct_sent"})

CLOSE_WAIT_TIMEOUT_SECONDS: Final[float] = 5.0

SCREEN_USE_ACK_TIMEOUT_SECONDS: Final[float] = 5.0

SCREEN_USE_POLL_MS: Final[int] = 50

VK_SHIFT: Final[int] = 0x10

VK_ESCAPE: Final[int] = 0x1B

GENERIC_KAKAO_WINDOW_TITLES: Final[frozenset[str]] = frozenset(
    {"", "카카오톡", "예약 메시지", "브리핑 보드"}
)

PC_CATALOG_REFRESH_REASONS: Final[frozenset[str]] = frozenset(
    {"manual_picker", "manual_validate", "manual_retry"}
)
