using System;

namespace Spoiler
{
    // Durable per-room catch-up position returned by the account-owned inbox.
    public sealed class MessageCatchUpCursor
    {
        public string ChatId { get; internal set; }
        public string LogId { get; internal set; }
        public DateTime UpdatedAtUtc { get; internal set; }
    }
}
