using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;

namespace Spoiler
{
    internal sealed class TactileButton : Button
    {
        public static readonly DependencyProperty IsPendingProperty = DependencyProperty.Register(
            "IsPending", typeof(bool), typeof(TactileButton), new PropertyMetadata(false));

        public bool IsPending
        {
            get { return (bool)GetValue(IsPendingProperty); }
            set { SetValue(IsPendingProperty, value); }
        }

        // Kept for existing callers. Button feedback never changes its size.
        public double PressedScale { get; set; }

        public TactileButton()
        {
            Cursor = Cursors.Hand;
            MinHeight = DesignTokens.Size.MinimumHit;
            FocusVisualStyle = null;
            HorizontalContentAlignment = HorizontalAlignment.Center;
            VerticalContentAlignment = VerticalAlignment.Center;
            PressedScale = 1.0;
        }

        public static ControlTemplate CreateRoundedTemplate(double radius, Brush hoverBrush)
        {
            SolidColorBrush hover = hoverBrush as SolidColorBrush;
            bool accent = hover != null && hover.Color == DesignTokens.Brush(DesignTokens.Palette.AccentHover).Color;
            return CreateRoundedTemplate(radius, hoverBrush,
                DesignTokens.Brush(accent ? DesignTokens.Palette.AccentPressed : DesignTokens.Palette.SurfacePressed));
        }

        public static ControlTemplate CreateRoundedTemplate(double radius, Brush hoverBrush, Brush pressedBrush)
        {
            ControlTemplate template = new ControlTemplate(typeof(Button));
            FrameworkElementFactory root = new FrameworkElementFactory(typeof(Grid));
            root.SetValue(UIElement.SnapsToDevicePixelsProperty, true);
            FrameworkElementFactory border = new FrameworkElementFactory(typeof(Border));
            border.Name = "Chrome";
            border.SetValue(Border.CornerRadiusProperty, new CornerRadius(radius));
            border.SetBinding(Border.BackgroundProperty, ParentBinding("Background"));
            border.SetBinding(Border.BorderBrushProperty, ParentBinding("BorderBrush"));
            border.SetBinding(Border.BorderThicknessProperty, ParentBinding("BorderThickness"));

            FrameworkElementFactory content = new FrameworkElementFactory(typeof(ContentPresenter));
            content.Name = "ButtonContent";
            content.SetValue(ContentPresenter.RecognizesAccessKeyProperty, true);
            content.SetBinding(ContentPresenter.HorizontalAlignmentProperty, ParentBinding("HorizontalContentAlignment"));
            content.SetBinding(ContentPresenter.VerticalAlignmentProperty, ParentBinding("VerticalContentAlignment"));
            content.SetBinding(ContentPresenter.MarginProperty, ParentBinding("Padding"));
            border.AppendChild(content);
            root.AppendChild(border);

            FrameworkElementFactory focusRing = new FrameworkElementFactory(typeof(Border));
            focusRing.Name = "FocusRing";
            focusRing.SetValue(Border.CornerRadiusProperty, new CornerRadius(radius));
            focusRing.SetValue(Border.BorderBrushProperty, DesignTokens.Brush(DesignTokens.Palette.Focus));
            focusRing.SetValue(Border.BorderThicknessProperty, new Thickness(2));
            focusRing.SetValue(UIElement.IsHitTestVisibleProperty, false);
            focusRing.SetValue(UIElement.VisibilityProperty, Visibility.Collapsed);
            root.AppendChild(focusRing);
            template.VisualTree = root;

            Trigger hover = new Trigger { Property = Button.IsMouseOverProperty, Value = true };
            hover.Setters.Add(new Setter(Border.BackgroundProperty, hoverBrush, "Chrome"));
            template.Triggers.Add(hover);

            Trigger pressed = new Trigger { Property = Button.IsPressedProperty, Value = true };
            pressed.Setters.Add(new Setter(Border.BackgroundProperty, pressedBrush, "Chrome"));
            template.Triggers.Add(pressed);

            Trigger disabled = new Trigger { Property = Button.IsEnabledProperty, Value = false };
            disabled.Setters.Add(new Setter(Border.BackgroundProperty, DesignTokens.Brush(DesignTokens.Palette.SurfaceDisabled), "Chrome"));
            disabled.Setters.Add(new Setter(Border.BorderBrushProperty, DesignTokens.Brush(DesignTokens.Palette.Divider), "Chrome"));
            disabled.Setters.Add(new Setter(TextElement.ForegroundProperty, DesignTokens.Brush(DesignTokens.Palette.InkDisabled), "ButtonContent"));
            disabled.Setters.Add(new Setter(UIElement.OpacityProperty, .65, "ButtonContent"));
            template.Triggers.Add(disabled);

            Trigger pending = new Trigger { Property = IsPendingProperty, Value = true };
            pending.Setters.Add(new Setter(Border.BackgroundProperty, DesignTokens.Brush(DesignTokens.Palette.SurfacePending), "Chrome"));
            pending.Setters.Add(new Setter(Border.BorderBrushProperty, DesignTokens.Brush(DesignTokens.Room.SelectedBorder), "Chrome"));
            pending.Setters.Add(new Setter(TextElement.ForegroundProperty, DesignTokens.Brush(DesignTokens.Palette.AccentInk), "ButtonContent"));
            pending.Setters.Add(new Setter(UIElement.OpacityProperty, 1.0, "ButtonContent"));
            template.Triggers.Add(pending);

            Trigger focus = new Trigger { Property = Button.IsKeyboardFocusedProperty, Value = true };
            focus.Setters.Add(new Setter(UIElement.VisibilityProperty, Visibility.Visible, "FocusRing"));
            template.Triggers.Add(focus);
            template.Seal();
            return template;
        }

        private static Binding ParentBinding(string property)
        {
            return new Binding(property) { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) };
        }
    }
}
