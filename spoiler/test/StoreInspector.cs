using System;
using System.Linq;

namespace Spoiler.Tests
{
    internal static class StoreInspector
    {
        public static int Main()
        {
            NotificationPreview preview = new NotificationStore().Load().FirstOrDefault();
            if (preview == null)
            {
                Console.Error.WriteLine("No captured preview was stored.");
                return 1;
            }

            Console.WriteLine(preview.Sender + "|" + preview.Message);
            return string.Equals(preview.Sender, "민수", StringComparison.Ordinal)
                && string.Equals(preview.Message, "오늘 회의 자료 보냈어요!", StringComparison.Ordinal)
                ? 0
                : 2;
        }
    }
}
