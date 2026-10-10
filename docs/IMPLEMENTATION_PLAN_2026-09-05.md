# KakaoTalk Hub 2.4 implementation

Approved scope: implement the product improvements proposed in the 2026-09-05 review, in order. No live messages are needed for validation.

1. Protect document edits/imports, distinguish errors after dispatch, connect screen state and cancellation to every PC workflow, serialize PC operations across processes, revalidate target window identity.
2. Show each row's delivery route and live waiting/running/success/uncertain/retryable counts.
3. Explain direct authentication recovery and provide read-only diagnostics; persist recoverable run results with collision-free logs; pin runtime dependencies and create local Git history.
4. Separate document editing, shared mapping policy and send services from Tk rendering while preserving tested public behavior.

Validation: offline regression tests for the review failures, process serialization/cancel/crash tests, journal recovery tests, existing Python/Node suite, and an isolated GUI smoke with fake services. Never read live credentials or interact with KakaoTalk during tests. Native direct reservation protocol research remains a separate experimental project; the approved product improvements retain existing native screen reservation.

Ownership while implementing:
- Root: GUI integration, document module, routing/progress, diagnostics UI, journal integration, runtime/Git/docs, final review.
- Sender worker: kakao_sender.py, shared PC lease module, sender/lease tests.
- Direct worker: kakao_direct.py, shared mapping policy module, refresh_student_chat_mappings.py, direct/mapping tests. GUI extraction only by explicit root coordination.
- Journal worker: durable run journal module and tests; root integrates it into the GUI.

No remote publish/push, live send, or user credential replacement is part of this implementation.

Completed: all five ordered implementation stages are included in version 2.4.0. See [release and validation report](RELEASE_2.4.md). Final clean-environment suite: 422 passed, 1 POSIX-only skip, 47 subtests passed; Node: 31 passed. Isolated own-window GUI smoke passed at 1100×760 and 900×560. Real account login and KakaoTalk message delivery were not exercised.
