using System;
using System.Drawing;
using System.Windows;
using System.Windows.Forms;
using System.Windows.Threading;
using WpfApplication = System.Windows.Application;

namespace Spoiler
{
    /// <summary>
    /// Owns the lifetime of Spoiler's notification-area icon.
    ///
    /// The host deliberately knows nothing about MainWindow.  The two callbacks
    /// keep the tray plumbing usable by the WPF window without creating a
    /// reference cycle, and also make all window work run on the WPF dispatcher.
    /// </summary>
    internal sealed class TrayIconHost : IDisposable
    {
        private readonly Dispatcher _dispatcher;
        private readonly Action _showWindow;
        private readonly Action _exitApplication;
        private readonly WpfApplication _application;
        private readonly NotifyIcon _notifyIcon;
        private readonly ContextMenuStrip _contextMenu;
        private readonly ToolStripMenuItem _openItem;
        private readonly ToolStripMenuItem _exitItem;
        private Icon _icon;
        private bool _disposed;
        private bool _exitRequested;
        private bool _hintShown;

        public TrayIconHost(Action showWindow, Action exitApplication)
        {
            if (showWindow == null)
            {
                throw new ArgumentNullException("showWindow");
            }
            if (exitApplication == null)
            {
                throw new ArgumentNullException("exitApplication");
            }

            _dispatcher = Dispatcher.CurrentDispatcher;
            _showWindow = showWindow;
            _exitApplication = exitApplication;
            _application = WpfApplication.Current;
            if (_application != null)
            {
                _application.SessionEnding += OnSessionEnding;
            }

            _openItem = new ToolStripMenuItem("Spoiler 열기");
            _openItem.Name = "OpenSpoiler";
            _openItem.AccessibleName = "Spoiler 열기";
            _openItem.ShortcutKeyDisplayString = "Enter";
            _openItem.Click += OnOpenClicked;

            _exitItem = new ToolStripMenuItem("완전히 종료");
            _exitItem.Name = "ExitSpoiler";
            _exitItem.AccessibleName = "Spoiler 완전히 종료";
            _exitItem.Click += OnExitClicked;

            _contextMenu = new ContextMenuStrip();
            _contextMenu.Name = "SpoilerTrayMenu";
            _contextMenu.ShowImageMargin = false;
            _contextMenu.Items.Add(_openItem);
            _contextMenu.Items.Add(new ToolStripSeparator());
            _contextMenu.Items.Add(_exitItem);

            _notifyIcon = new NotifyIcon();
            _notifyIcon.Text = "Spoiler";
            _notifyIcon.ContextMenuStrip = _contextMenu;
            _notifyIcon.DoubleClick += OnNotifyIconDoubleClick;
            _notifyIcon.BalloonTipTitle = "Spoiler";
            _notifyIcon.BalloonTipIcon = ToolTipIcon.Info;

            // The .ico is embedded in the single executable.  A system icon is
            // a safe fallback for development/test builds missing the resource.
            _icon = AssetLoader.LoadTrayIcon();
            if (_icon == null)
            {
                _icon = (Icon)SystemIcons.Application.Clone();
            }
            _notifyIcon.Icon = _icon;
            _notifyIcon.Visible = true;
        }

        /// <summary>
        /// True after the user selected the explicit tray exit action.  A window
        /// Closing handler can use this to distinguish "hide to tray" from a
        /// real application exit.
        /// </summary>
        public bool IsExitRequested
        {
            get { return _exitRequested; }
        }

        /// <summary>
        /// Shows a short, non-blocking explanation when the window is hidden.
        /// It is intentionally shown once per host lifetime so repeated closes
        /// do not become notification spam.
        /// </summary>
        public void ShowBackgroundHint()
        {
            RunOnDispatcher(delegate
            {
                if (_disposed || _hintShown)
                {
                    return;
                }

                _hintShown = true;
                try
                {
                    _notifyIcon.ShowBalloonTip(
                        3500,
                        "Spoiler가 백그라운드에서 실행 중이에요",
                        "트레이 아이콘을 더블클릭하면 창을 다시 열 수 있어요.",
                        ToolTipIcon.Info);
                }
                catch (InvalidOperationException)
                {
                    // The shell can reject a balloon while the notification
                    // area is restarting.  The tray icon itself still works.
                }
            });
        }

        /// <summary>
        /// Marks an intentional exit and invokes the application-owned exit
        /// callback.  The callback is responsible for closing MainWindow and
        /// calling Application.Shutdown; this class only owns the tray shell.
        /// </summary>
        public void RequestExit()
        {
            RunOnDispatcher(delegate
            {
                if (_disposed || _exitRequested)
                {
                    return;
                }

                _exitRequested = true;
                _notifyIcon.Visible = false;
                InvokeExitCallbackAndShutdown();
            });
        }

        public void Dispose()
        {
            RunOnDispatcher(DisposeCore);
            GC.SuppressFinalize(this);
        }

        private void OnOpenClicked(object sender, EventArgs args)
        {
            RunOnDispatcher(_showWindow);
        }

        private void OnExitClicked(object sender, EventArgs args)
        {
            RequestExit();
        }

        private void OnSessionEnding(object sender, SessionEndingCancelEventArgs args)
        {
            // Windows is already in the process of signing out/shutting down.
            // Marking this as an explicit close lets MainWindow's hide-to-tray
            // handler allow the normal shutdown path to complete.
            RunOnDispatcher(delegate
            {
                if (_disposed || _exitRequested)
                {
                    return;
                }

                _exitRequested = true;
                _notifyIcon.Visible = false;
                InvokeExitCallbackAndShutdown();
            });
        }

        private void OnNotifyIconDoubleClick(object sender, EventArgs args)
        {
            RunOnDispatcher(_showWindow);
        }

        private void InvokeExitCallbackAndShutdown()
        {
            // MainWindow's callback may enqueue its own Close() operation.  A
            // lower-priority shutdown lets that operation mark the close as
            // intentional before OnExplicitShutdown closes the dispatcher.
            _exitApplication();
            if (_application == null)
            {
                return;
            }

            try
            {
                _dispatcher.BeginInvoke(
                    DispatcherPriority.ApplicationIdle,
                    new Action(delegate
                    {
                        try
                        {
                            _application.Shutdown();
                        }
                        catch (InvalidOperationException)
                        {
                            // The OS or another owner may already be shutting
                            // down the dispatcher.
                        }
                    }));
            }
            catch (InvalidOperationException)
            {
                // Dispatcher is already stopping; the original exit request
                // has still been delivered to the application owner.
            }
        }

        private void RunOnDispatcher(Action action)
        {
            if (action == null || _disposed)
            {
                return;
            }

            if (_dispatcher.CheckAccess())
            {
                action();
                return;
            }

            try
            {
                _dispatcher.BeginInvoke(action);
            }
            catch (InvalidOperationException)
            {
                // Dispatcher is shutting down.  There is no UI left to restore
                // or notify, so the request can safely be ignored.
            }
        }

        private void DisposeCore()
        {
            if (_disposed)
            {
                return;
            }

            _disposed = true;
            try
            {
                _notifyIcon.Visible = false;
            }
            catch (ObjectDisposedException)
            {
            }

            _notifyIcon.DoubleClick -= OnNotifyIconDoubleClick;
            _openItem.Click -= OnOpenClicked;
            _exitItem.Click -= OnExitClicked;
            if (_application != null)
            {
                _application.SessionEnding -= OnSessionEnding;
            }

            _notifyIcon.ContextMenuStrip = null;
            _notifyIcon.Dispose();
            _contextMenu.Dispose();
            if (_icon != null)
            {
                _icon.Dispose();
                _icon = null;
            }
        }
    }
}
