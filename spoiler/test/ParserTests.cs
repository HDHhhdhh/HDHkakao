using System;
using System.Collections.Generic;

namespace Spoiler.Tests
{
    internal static class ParserTests
    {
        public static int Main()
        {
            int failures = 0;
            failures += AssertParsed(
                new[] { "카카오톡", "민수", "오늘 회의 자료 보냈어요!", "10:58", "닫기" },
                "민수",
                "오늘 회의 자료 보냈어요!");
            failures += AssertParsed(
                new[] { "지원: 점심 메뉴 뭐 먹을까?" },
                "지원",
                "점심 메뉴 뭐 먹을까?");
            failures += AssertParsed(
                new[] { "카카오톡", "동아리방", "수진", "사진 보냈어!", "시스템 메뉴 모음", "닫기" },
                "수진",
                "사진 보냈어!");
            failures += AssertParsed(
                new[] { "닫기", "카카오톡", "지원: 점심 메뉴 뭐 먹을까?", "읽음으로 표시" },
                "지원",
                "점심 메뉴 뭐 먹을까?");
            failures += AssertParsed(
                new[] { "카카오톡", "가족방", "엄마", "사진 3장을 보냈습니다.", "10:59", "읽음으로 표시", "링크 열기" },
                "엄마",
                "사진 3장을 보냈습니다.");
            failures += AssertParsed(
                new[] { "카카오톡", "민수", "민수", "동영상을 보냈습니다.", "mark as read", "open message" },
                "민수",
                "동영상을 보냈습니다.");
            failures += AssertParsed(
                new[] { "카카오톡", "민수", "회의는 10:30에 시작해" },
                "민수",
                "회의는 10:30에 시작해");
            failures += AssertParsed(
                new[] { "카카오톡", "민수", "자료: https://example.com/a:b" },
                "민수",
                "자료: https://example.com/a:b");
            failures += AssertParsed(
                new[] { "오늘 회의 자료 보냈어요!", "민수", "카카오톡" },
                "민수",
                "오늘 회의 자료 보냈어요!");
            failures += AssertRejected(new[] { "카카오톡", "닫기", "10:58" });
            failures += AssertRejected(new[] { "카카오톡", "알림 설정", "읽음으로 표시", "열기" });
            failures += AssertRejected(new[] { "편집", "메시지 입력" });
            failures += AssertRejected(new[] { "MOMENT 광고", "광고 링크" });
            failures += AssertTrustedPath();
            failures += AssertInteractiveRoles();
            failures += AssertNotificationSettings();

            Console.WriteLine(failures == 0 ? "PASS: parser tests" : "FAIL: " + failures + " parser tests");
            return failures == 0 ? 0 : 1;
        }

        private static int AssertParsed(IEnumerable<string> parts, string expectedSender, string expectedMessage)
        {
            string sender;
            string message;
            bool result = NotificationCaptureService.TryParseParts(parts, out sender, out message);
            if (!result || sender != expectedSender || message != expectedMessage)
            {
                Console.WriteLine("Expected {0} / {1}; got {2} / {3}", expectedSender, expectedMessage, sender, message);
                return 1;
            }
            return 0;
        }

        private static int AssertRejected(IEnumerable<string> parts)
        {
            string sender;
            string message;
            if (NotificationCaptureService.TryParseParts(parts, out sender, out message))
            {
                Console.WriteLine("Expected rejection; got {0} / {1}", sender, message);
                return 1;
            }
            return 0;
        }

        private static int AssertTrustedPath()
        {
            string programFilesX86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
            string trusted = System.IO.Path.Combine(programFilesX86, "Kakao", "KakaoTalk", "KakaoTalk.exe");
            string lookalike = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                "Kakao",
                "KakaoTalk",
                "KakaoTalk.exe");

            if (!NotificationCaptureService.IsTrustedKakaoExecutablePath(trusted)
                || NotificationCaptureService.IsTrustedKakaoExecutablePath(lookalike))
            {
                Console.WriteLine("Trusted KakaoTalk path allowlist failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertInteractiveRoles()
        {
            int[] interactive = { 0x02, 0x0B, 0x0C, 0x1E, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x34 };
            foreach (int role in interactive)
            {
                if (!NotificationCaptureService.IsInteractiveAccessibleRole(role))
                {
                    Console.WriteLine("Interactive MSAA role was not rejected: 0x{0:X}", role);
                    return 1;
                }
            }

            if (NotificationCaptureService.IsInteractiveAccessibleRole(0x2A))
            {
                Console.WriteLine("Non-interactive MSAA role was rejected.");
                return 1;
            }

            return 0;
        }

        private static int AssertNotificationSettings()
        {
            if (NotificationCaptureService.ParseKakaoNotificationSetting(new[] { "use_msg_recv_alarm = yes" }) != KakaoNotificationSettingState.Enabled
                || NotificationCaptureService.ParseKakaoNotificationSetting(new[] { "use_msg_recv_alarm = no" }) != KakaoNotificationSettingState.Disabled
                || NotificationCaptureService.ParseKakaoNotificationSetting(new[] { "unrelated = yes" }) != KakaoNotificationSettingState.Unknown)
            {
                Console.WriteLine("KakaoTalk notification setting parser failed.");
                return 1;
            }

            return 0;
        }
    }
}
