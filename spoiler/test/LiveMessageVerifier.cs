using System;
using System.Linq;

namespace Spoiler.Tests
{
    /// <summary>
    /// QA-only verifier that never prints stored sender names or message text.
    /// It reports only whether an exact caller-supplied test marker exists.
    /// </summary>
    internal static class LiveMessageVerifier
    {
        public static int Main(string[] args)
        {
            string marker = args != null && args.Length > 0 ? args[0] : string.Empty;
            if (string.IsNullOrEmpty(marker))
            {
                Console.WriteLine("INVALID_MARKER");
                return 2;
            }

            var items = new NotificationStore().Load();
            bool matched = items.Any(item => item != null
                && string.Equals(item.Message, marker, StringComparison.Ordinal));
            Console.WriteLine(matched ? "MATCH" : "NO_MATCH");
            Console.WriteLine("ITEM_COUNT=" + items.Count);
            return matched ? 0 : 1;
        }
    }
}
