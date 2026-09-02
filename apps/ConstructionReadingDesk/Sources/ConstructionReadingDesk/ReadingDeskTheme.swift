import AppKit
import SwiftUI

enum ReadingDeskTheme {
    static let canvas = adaptive(
        "reading-desk-canvas",
        light: NSColor(calibratedRed: 0.929, green: 0.957, blue: 0.945, alpha: 1),
        dark: NSColor(calibratedRed: 0.082, green: 0.110, blue: 0.106, alpha: 1)
    )
    static let panel = adaptive(
        "reading-desk-panel",
        light: NSColor(calibratedRed: 0.902, green: 0.941, blue: 0.925, alpha: 1),
        dark: NSColor(calibratedRed: 0.094, green: 0.125, blue: 0.120, alpha: 1)
    )
    static let card = adaptive(
        "reading-desk-card",
        light: NSColor(calibratedRed: 0.988, green: 0.992, blue: 0.980, alpha: 1),
        dark: NSColor(calibratedRed: 0.125, green: 0.157, blue: 0.149, alpha: 1)
    )
    static let cardCool = adaptive(
        "reading-desk-card-cool",
        light: NSColor(calibratedRed: 0.942, green: 0.961, blue: 0.973, alpha: 1),
        dark: NSColor(calibratedRed: 0.111, green: 0.145, blue: 0.157, alpha: 1)
    )
    static let field = adaptive(
        "reading-desk-field",
        light: NSColor(calibratedRed: 0.969, green: 0.979, blue: 0.972, alpha: 1),
        dark: NSColor(calibratedRed: 0.098, green: 0.125, blue: 0.120, alpha: 1)
    )
    static let accent = adaptive(
        "reading-desk-accent",
        light: NSColor(calibratedRed: 0.160, green: 0.514, blue: 0.490, alpha: 1),
        dark: NSColor(calibratedRed: 0.380, green: 0.745, blue: 0.690, alpha: 1)
    )
    static let accentSoft = adaptive(
        "reading-desk-accent-soft",
        light: NSColor(calibratedRed: 0.855, green: 0.932, blue: 0.910, alpha: 1),
        dark: NSColor(calibratedRed: 0.105, green: 0.243, blue: 0.224, alpha: 1)
    )
    static let border = adaptive(
        "reading-desk-border",
        light: NSColor(calibratedRed: 0.805, green: 0.850, blue: 0.833, alpha: 1),
        dark: NSColor(calibratedRed: 0.255, green: 0.314, blue: 0.298, alpha: 1)
    )
    static let strongBorder = adaptive(
        "reading-desk-strong-border",
        light: NSColor(calibratedRed: 0.590, green: 0.675, blue: 0.645, alpha: 1),
        dark: NSColor(calibratedRed: 0.440, green: 0.530, blue: 0.505, alpha: 1)
    )
    static let bannerStart = adaptive(
        "reading-desk-banner-start",
        light: NSColor(calibratedRed: 0.830, green: 0.929, blue: 0.889, alpha: 1),
        dark: NSColor(calibratedRed: 0.090, green: 0.255, blue: 0.219, alpha: 1)
    )
    static let bannerEnd = adaptive(
        "reading-desk-banner-end",
        light: NSColor(calibratedRed: 0.888, green: 0.927, blue: 0.965, alpha: 1),
        dark: NSColor(calibratedRed: 0.105, green: 0.174, blue: 0.224, alpha: 1)
    )

    private static func adaptive(_ name: String, light: NSColor, dark: NSColor) -> Color {
        Color(nsColor: NSColor(name: NSColor.Name(name)) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
        })
    }
}

struct ReadingDeskBackground: View {
    var body: some View {
        LinearGradient(
            colors: [ReadingDeskTheme.canvas, ReadingDeskTheme.panel.opacity(0.72)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
        .ignoresSafeArea()
        .accessibilityHidden(true)
    }
}

private struct ReadingDeskCardModifier: ViewModifier {
    let padding: CGFloat
    let cool: Bool
    @Environment(\.colorSchemeContrast) private var contrast
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func body(content: Content) -> some View {
        content
            .padding(padding)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(cool ? ReadingDeskTheme.cardCool : ReadingDeskTheme.card)
            )
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(contrast == .increased ? ReadingDeskTheme.strongBorder : ReadingDeskTheme.border, lineWidth: 1)
            }
            .shadow(
                color: reduceTransparency ? .clear : Color.black.opacity(0.055),
                radius: 6,
                x: 0,
                y: 2
            )
    }
}

extension View {
    func readingDeskCard(padding: CGFloat = 16, cool: Bool = false) -> some View {
        modifier(ReadingDeskCardModifier(padding: padding, cool: cool))
    }
}

struct ReadingDeskGroupBoxStyle: GroupBoxStyle {
    @Environment(\.colorSchemeContrast) private var contrast
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            configuration.label
                .font(.headline)
                .foregroundStyle(.primary)
            Rectangle()
                .fill(contrast == .increased ? ReadingDeskTheme.strongBorder : ReadingDeskTheme.border)
                .frame(height: 1)
                .accessibilityHidden(true)
            configuration.content
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(ReadingDeskTheme.card)
        )
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(contrast == .increased ? ReadingDeskTheme.strongBorder : ReadingDeskTheme.border, lineWidth: 1)
        }
        .shadow(
            color: reduceTransparency ? .clear : Color.black.opacity(0.05),
            radius: 6,
            x: 0,
            y: 2
        )
    }
}

struct ReadingDeskSectionTitle: View {
    let title: String
    let systemImage: String
    var count: Int?

    var body: some View {
        HStack(spacing: 7) {
            Image(systemName: systemImage)
                .foregroundStyle(ReadingDeskTheme.accent)
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
            Spacer(minLength: 8)
            if let count {
                Text("\(count)")
                    .font(.caption2.monospacedDigit().weight(.semibold))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(ReadingDeskTheme.cardCool, in: Capsule())
            }
        }
        .accessibilityElement(children: .combine)
    }
}
