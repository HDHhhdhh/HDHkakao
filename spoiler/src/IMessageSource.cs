using System;

namespace Spoiler
{
    /// <summary>
    /// A read-only source of KakaoTalk message previews.
    ///
    /// Start, Stop, and preview delivery must not open chat rooms, send a
    /// message, or advance a read watermark. An explicit user-triggered reply
    /// capability is exposed separately through IReplyMessageSource. This interface uses
    /// the same events as the original notification collector so the UI can
    /// switch sources without changing its rendering code.
    /// </summary>
    public interface IMessageSource : IDisposable
    {
        event EventHandler<NotificationPreview> PreviewCaptured;
        event EventHandler<CaptureStateChangedEventArgs> StateChanged;

        bool IsRunning { get; }

        void Start();

        void Stop();
    }
}
