using System.Windows.Media;
using System.Windows.Media.Effects;
using System;
using System.Collections.Generic;

namespace Spoiler
{
    internal static class DesignTokens
    {
        private static readonly object BrushGate = new object();
        private static readonly Dictionary<string, SolidColorBrush> BrushesByColor = new Dictionary<string, SolidColorBrush>(StringComparer.OrdinalIgnoreCase);

        internal static class Palette
        {
            public const string Canvas = "#FAFAFA";
            public const string Surface = "#FFFFFF";
            public const string SurfaceSubtle = "#F7F7F8";
            public const string SurfaceMuted = "#F0F1F3";
            public const string SurfaceControl = "#F3F4F5";
            public const string SurfaceHover = "#F5F6F7";
            public const string SurfaceControlHover = "#E8EAED";
            public const string SurfacePressed = "#DDE1E5";
            public const string SurfaceDisabled = "#F0F1F3";
            public const string SurfacePending = "#FFF2AD";
            public const string SurfaceNewest = "#FFFCF0";
            public const string SurfaceNewestHover = "#FFF7D8";
            public const string SurfaceNotice = "#F5F4EE";
            public const string SurfaceExpanded = "#FAFAF7";
            public const string Divider = "#E5E7EB";
            public const string DividerStrong = "#D4D7DC";
            public const string InkStrong = "#18181B";
            public const string Ink = "#27272A";
            public const string InkBody = "#44444B";
            public const string InkSecondary = "#60646C";
            public const string InkMuted = "#60646C";
            public const string InkDisabled = "#777C85";
            public const string InkQuiet = "#8A8A84";
            public const string Icon = "#3C3C39";
            public const string IconMuted = "#5C5C57";
            public const string StatusIdle = "#B9B9B4";
            public const string StatusWatching = "#68C882";
            public const string StatusError = "#E88181";
            public const string Accent = "#FEE500";
            public const string AccentHover = "#F2D900";
            public const string AccentPressed = "#E4CC00";
            public const string AccentInk = "#1D1B00";
            public const string DangerSurface = "#FDE7E8";
            public const string DangerInk = "#B4232C";
            public const string Focus = "#2563EB";
            public const string ScrollThumb = "#90949B";
            public const string ScrollThumbHover = "#747982";

            public static readonly string[] AvatarFallbacks =
            {
                "#E9D7FF", "#D9EEFF", "#FFE4D5", "#DDF4E4", "#F3E6C8", "#E3E1FF"
            };
        }

        internal static class Room
        {
            public const string Canvas = "#F3F4F4";
            public const string Selected = "#FFF9D9";
            public const string SelectedBorder = "#E8D989";
            public const string Outgoing = Palette.Accent;
            public const double Sidebar = 276;
            public const double SidebarMin = 252;
            public const double SidebarMax = 320;
            public const double Inset = Space.Xl;
            public const double Gap = Space.Sm;
            public const double Avatar = 40;
            public const double RowHeight = 76;
            public const double TitleAction = 34;
            public const double Action = 36;
            public const double SearchHeight = 40;
            public const double ComposerHeight = 72;
            public const double BubbleRadius = 16;
            public const double BubbleInset = 12;
            public const double Body = TypeScale.Body;
            public const double Metadata = TypeScale.GroupMetadata;
            public const double Heading = 18;
            public const double BubbleMax = 560;
            public const double BubbleReserve = 124;
        }

        internal static class TypeScale
        {
            public const double WindowTitle = 17;
            public const double Hero = 39;
            public const double HeroLine = 44;
            public const double Section = 22;
            public const double Sender = 17;
            public const double Body = 14.5;
            public const double BodyLine = 21;
            public const double Metadata = 14;
            public const double GroupMetadata = 12.5;
            public const double Small = 13;
            public const double Status = 15;
            public const double PrivacyTitle = 15;
            public const double PrivacyBody = 12.5;
            public const double EmptyTitle = 19;
            public const double EmptyBody = 13.5;
            public const double RegistrationTitle = 27;
            public const double RegistrationIntro = 14;
            public const double FieldLabel = 13;
            public const double FieldText = 15;
            public const double Passcode = 25;
            public const double RegistrationStatus = 13.5;
        }

        internal static class Size
        {
            public const double WindowWidth = 960;
            public const double WindowHeight = 800;
            public const double WindowMinWidth = 760;
            public const double WindowMinHeight = 560;
            public const double TitleBarHeight = 36;
            public const double RailWidth = 860;
            public const double MinimumHit = 44;
            public const double Action = 48;
            public const double PrimaryActionWidth = 142;
            public const double TitleIcon = 34;
            public const double HeroIcon = 82;
            public const double Avatar = 68;
            public const double GroupAvatar = 62;
            public const double Unread = 30;
            public const double PrivacyIcon = 50;
            public const double RowMinHeight = 96;
            public const double GroupRowMinHeight = 104;
            public const double AvatarColumn = 96;
            public const double TimeColumn = 84;
            public const double UnreadColumn = 42;
            public const double EmptyMinHeight = 290;
            public const double RegistrationWidth = 500;
            public const double RegistrationHeight = 570;
            public const double RegistrationMinWidth = 460;
            public const double RegistrationMinHeight = 540;
            public const double FieldHeight = 44;
            public const double RegistrationPrimaryButtonMinWidth = 108;
        }

        internal static class Space
        {
            public const double Xs = 3;
            public const double Sm = 8;
            public const double Md = 10;
            public const double Lg = 14;
            public const double Xl = 16;
            public const double TitleBarLeft = 18;
            public const double TitleBarRight = 8;
            public const double ContentHorizontal = 36;
            public const double ContentTop = 26;
            public const double ContentBottom = 30;
            public const double HeaderBottom = 30;
            public const double FooterHorizontal = 30;
            public const double FooterVertical = 40;
            public const double RowLeft = 10;
            public const double RowVertical = 8;
            public const double RowRight = 14;
            public const double NoticeVertical = 15;
            public const double NoticeHorizontal = 18;
            public const double GroupInset = 16;
            public const double MessageInset = 14;
            public const double RegistrationInset = 28;
            public const double RegistrationFieldGap = 12;
            public const double RegistrationLabelGap = 6;
            public const double RegistrationFieldPaddingHorizontal = 14;
            public const double RegistrationButtonsTop = 12;
            public const double RegistrationButtonGap = 8;
            public const double RegistrationButtonPaddingHorizontal = 18;
            public const double RegistrationSecondaryButtonPaddingHorizontal = 15;
        }

        internal static class Radius
        {
            public const double Window = 22;
            public const double LatestRow = 10;
            public const double Panel = 18;
            public const double Notice = 14;
            public const double Message = 11;
            public const double Action = 15;
            public const double TitleAction = 12;
            public const double Field = 10;
        }

        internal static SolidColorBrush Brush(string hex)
        {
            lock (BrushGate)
            {
                SolidColorBrush brush;
                if (BrushesByColor.TryGetValue(hex, out brush)) return brush;
                brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(hex));
                brush.Freeze();
                BrushesByColor.Add(hex, brush);
                return brush;
            }
        }

        internal static DropShadowEffect LatestRowElevation()
        {
            return new DropShadowEffect
            {
                Color = Color.FromRgb(140, 118, 42),
                BlurRadius = 10,
                ShadowDepth = 1,
                Direction = 270,
                Opacity = 0.045
            };
        }

        internal static DropShadowEffect ActionElevation()
        {
            return new DropShadowEffect
            {
                Color = Colors.Black,
                BlurRadius = 12,
                ShadowDepth = 2,
                Direction = 270,
                Opacity = 0.12
            };
        }
    }
}
