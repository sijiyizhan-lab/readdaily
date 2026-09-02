import Foundation

public enum ReadingWorkspaceMode: Equatable, Sendable {
    case sideBySide
    case stacked
}

public enum ReadingWorkspaceLayout {
    public static let sideBySideBreakpoint = 1_400.0

    public static func mode(for containerWidth: Double) -> ReadingWorkspaceMode {
        containerWidth >= sideBySideBreakpoint ? .sideBySide : .stacked
    }
}

public enum CarouselPlaybackPolicy {
    public static func shouldAutoAdvance(
        reduceMotion: Bool,
        isUserPaused: Bool,
        isHovering: Bool,
        hasKeyboardFocus: Bool
    ) -> Bool {
        !reduceMotion && !isUserPaused && !isHovering && !hasKeyboardFocus
    }
}

public struct SRGBColor: Equatable, Sendable {
    public let red: Double
    public let green: Double
    public let blue: Double

    public init(red: Double, green: Double, blue: Double) {
        self.red = red
        self.green = green
        self.blue = blue
    }

    public func contrastRatio(against other: SRGBColor) -> Double {
        let brighter = max(relativeLuminance, other.relativeLuminance)
        let darker = min(relativeLuminance, other.relativeLuminance)
        return (brighter + 0.05) / (darker + 0.05)
    }

    private var relativeLuminance: Double {
        0.2126 * Self.linearized(red)
            + 0.7152 * Self.linearized(green)
            + 0.0722 * Self.linearized(blue)
    }

    private static func linearized(_ component: Double) -> Double {
        component <= 0.04045
            ? component / 12.92
            : pow((component + 0.055) / 1.055, 2.4)
    }
}

public enum ReadDailyAccessibilityColors {
    public static let cardLight = SRGBColor(red: 0.988, green: 0.992, blue: 0.980)
    public static let cardDark = SRGBColor(red: 0.125, green: 0.157, blue: 0.149)
    public static let accentSoftLight = SRGBColor(red: 0.855, green: 0.932, blue: 0.910)
    public static let accentSoftDark = SRGBColor(red: 0.105, green: 0.243, blue: 0.224)

    public static let accentTextLight = SRGBColor(red: 0.078, green: 0.420, blue: 0.396)
    public static let accentTextDark = SRGBColor(red: 0.396, green: 0.780, blue: 0.725)
    public static let positiveTextLight = SRGBColor(red: 0.090, green: 0.420, blue: 0.227)
    public static let positiveTextDark = SRGBColor(red: 0.388, green: 0.820, blue: 0.541)
    public static let attentionTextLight = SRGBColor(red: 0.549, green: 0.278, blue: 0.000)
    public static let attentionTextDark = SRGBColor(red: 1.000, green: 0.694, blue: 0.361)
    public static let failureTextLight = SRGBColor(red: 0.639, green: 0.149, blue: 0.133)
    public static let failureTextDark = SRGBColor(red: 1.000, green: 0.561, blue: 0.529)
}
