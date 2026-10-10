using System;
using System.Collections.Generic;
using System.Text;
using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;

namespace Spoiler
{
    // WPF permits a break between Korean syllables even with WrapWithOverflow.
    // These joins belong to the display only. The source and clipboard are raw.
    internal sealed class KoreanTextView
    {
        public readonly string Original;
        public readonly string Display;
        private readonly int[] _sourceOffsets;

        public KoreanTextView(string text)
        {
            Original = text ?? string.Empty;
            int firstJoin = 1;
            while (firstJoin < Original.Length && !ShouldJoin(Original[firstJoin - 1], Original[firstJoin])) firstJoin++;
            if (firstJoin >= Original.Length) { Display = Original; return; }
            StringBuilder display = new StringBuilder(Original.Length * 2);
            List<int> offsets = new List<int>(Original.Length * 2 + 1) { 0 };
            for (int i = 0; i < Original.Length; i++)
            {
                if (i > 0 && ShouldJoin(Original[i - 1], Original[i]))
                {
                    display.Append('\u2060');
                    offsets.Add(i);
                }
                display.Append(Original[i]);
                offsets.Add(i + 1);
            }
            Display = display.ToString();
            _sourceOffsets = offsets.ToArray();
        }

        private static bool ShouldJoin(char left, char right)
        {
            bool leftKorean = left >= '\uAC00' && left <= '\uD7A3';
            bool rightKorean = right >= '\uAC00' && right <= '\uD7A3';
            return (leftKorean || rightKorean) && char.IsLetterOrDigit(left) && char.IsLetterOrDigit(right);
        }

        public string OriginalRange(int start, int length)
        {
            int from = Math.Max(0, Math.Min(start, Display.Length));
            int to = (int)Math.Min(Display.Length, (long)from + Math.Max(0, length));
            if (_sourceOffsets != null) { from = _sourceOffsets[from]; to = _sourceOffsets[to]; }
            return Original.Substring(from, to - from);
        }
    }

    internal sealed class KoreanMessageTextBox : TextBox
    {
        private readonly KoreanTextView _view;
        public string OriginalText { get { return _view == null ? string.Empty : _view.Original; } }

        public KoreanMessageTextBox(string original)
        {
            _view = new KoreanTextView(original);
            IsReadOnly = true;
            TextWrapping = TextWrapping.Wrap; // Emergency break for oversized words.
            Text = _view.Display;
            DataObject.AddCopyingHandler(this, OnCopying);
        }

        public string OriginalSelectionText()
        {
            return _view.OriginalRange(SelectionStart, SelectionLength);
        }

        private void OnCopying(object sender, DataObjectCopyingEventArgs args)
        {
            string raw = OriginalSelectionText();
            args.DataObject.SetData(DataFormats.UnicodeText, raw);
            args.DataObject.SetData(DataFormats.Text, raw);
            args.DataObject.SetData(DataFormats.StringFormat, raw);
        }

        protected override AutomationPeer OnCreateAutomationPeer()
        {
            return new RawValuePeer(this);
        }

        private sealed class RawValuePeer : TextBoxAutomationPeer, IValueProvider
        {
            public RawValuePeer(KoreanMessageTextBox owner) : base(owner) { }
            public override object GetPattern(PatternInterface pattern)
            {
                return pattern == PatternInterface.Value ? this : base.GetPattern(pattern);
            }
            bool IValueProvider.IsReadOnly { get { return true; } }
            string IValueProvider.Value { get { return ((KoreanMessageTextBox)Owner).OriginalText; } }
            void IValueProvider.SetValue(string value) { throw new InvalidOperationException("Read-only message."); }
        }
    }

    internal sealed class KoreanGuideTextBlock : TextBlock
    {
        private string _original = string.Empty;
        private bool _updating;
        public string OriginalText { get { return _original; } }

        static KoreanGuideTextBlock()
        {
            TextProperty.OverrideMetadata(typeof(KoreanGuideTextBlock), new FrameworkPropertyMetadata(string.Empty, OnGuidePropertyChanged));
            TextWrappingProperty.OverrideMetadata(typeof(KoreanGuideTextBlock), new FrameworkPropertyMetadata(TextWrapping.NoWrap, OnGuidePropertyChanged));
        }

        private static void OnGuidePropertyChanged(DependencyObject sender, DependencyPropertyChangedEventArgs args)
        {
            ((KoreanGuideTextBlock)sender).UpdateDisplay(args);
        }

        private void UpdateDisplay(DependencyPropertyChangedEventArgs args)
        {
            if (_updating) return;
            if (args.Property == TextProperty) _original = (string)args.NewValue ?? string.Empty;
            if (args.Property != TextProperty && args.Property != TextWrappingProperty) return;
            string display = TextWrapping == TextWrapping.NoWrap ? _original : new KoreanTextView(_original).Display;
            if (Text == display) return;
            _updating = true;
            try { SetCurrentValue(TextProperty, display); }
            finally { _updating = false; }
        }
    }
}
