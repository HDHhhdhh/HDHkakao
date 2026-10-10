using System.Threading.Tasks;

namespace Spoiler
{
    /// <summary>Optional explicit user action, separate from the read-only listener contract.</summary>
    public interface IReplyMessageSource
    {
        bool CanReply { get; }
        Task<ReplySendResult> SendReplyAsync(string chatId, string text);
    }

    public enum ReplySendStatus { Sent, Failed, Unknown }

    public sealed class ReplySendResult
    {
        public ReplySendStatus Status { get; private set; }
        public string Code { get; private set; }
        public string Message { get; private set; }
        public string RequestId { get; private set; }
        public string ChatId { get; private set; }
        public string LogId { get; private set; }

        public ReplySendResult(ReplySendStatus status, string code, string message,
            string requestId, string chatId, string logId)
        {
            Status = status; Code = code; Message = message;
            RequestId = requestId; ChatId = chatId; LogId = logId;
        }
    }
}
