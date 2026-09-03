import Testing
@testable import ConstructionReadingDeskCore

@Suite("Read Daily 可访问性策略")
struct AccessibilityPolicyTests {
    @Test("1400 点以下切换为纵向工作区以避免横向溢出")
    func responsiveWorkspaceBreakpoint() {
        #expect(ReadingWorkspaceLayout.mode(for: 1_399) == .stacked)
        #expect(ReadingWorkspaceLayout.mode(for: 1_400) == .sideBySide)
        #expect(ReadingWorkspaceLayout.mode(for: 1_920) == .sideBySide)
    }

    @Test("轮播在用户暂停、悬停、键盘聚焦或减少动态效果时停止")
    func carouselPausePolicy() {
        #expect(CarouselPlaybackPolicy.shouldAutoAdvance(
            reduceMotion: false,
            isUserPaused: false,
            isHovering: false,
            hasKeyboardFocus: false
        ))
        #expect(!CarouselPlaybackPolicy.shouldAutoAdvance(
            reduceMotion: false,
            isUserPaused: true,
            isHovering: false,
            hasKeyboardFocus: false
        ))
        #expect(!CarouselPlaybackPolicy.shouldAutoAdvance(
            reduceMotion: false,
            isUserPaused: false,
            isHovering: true,
            hasKeyboardFocus: false
        ))
        #expect(!CarouselPlaybackPolicy.shouldAutoAdvance(
            reduceMotion: false,
            isUserPaused: false,
            isHovering: false,
            hasKeyboardFocus: true
        ))
        #expect(!CarouselPlaybackPolicy.shouldAutoAdvance(
            reduceMotion: true,
            isUserPaused: false,
            isHovering: false,
            hasKeyboardFocus: false
        ))
    }

    @Test("状态和强调文字在浅色与深色表面均达到 4.5 比 1")
    func readableTextPaletteMeetsWCAGAA() {
        let pairs = [
            (ReadDailyAccessibilityColors.accentTextLight, ReadDailyAccessibilityColors.cardLight),
            (ReadDailyAccessibilityColors.accentTextLight, ReadDailyAccessibilityColors.accentSoftLight),
            (ReadDailyAccessibilityColors.positiveTextLight, ReadDailyAccessibilityColors.cardLight),
            (ReadDailyAccessibilityColors.positiveTextLight, ReadDailyAccessibilityColors.accentSoftLight),
            (ReadDailyAccessibilityColors.attentionTextLight, ReadDailyAccessibilityColors.cardLight),
            (ReadDailyAccessibilityColors.attentionTextLight, ReadDailyAccessibilityColors.accentSoftLight),
            (ReadDailyAccessibilityColors.failureTextLight, ReadDailyAccessibilityColors.cardLight),
            (ReadDailyAccessibilityColors.failureTextLight, ReadDailyAccessibilityColors.accentSoftLight),
            (ReadDailyAccessibilityColors.accentTextDark, ReadDailyAccessibilityColors.cardDark),
            (ReadDailyAccessibilityColors.accentTextDark, ReadDailyAccessibilityColors.accentSoftDark),
            (ReadDailyAccessibilityColors.positiveTextDark, ReadDailyAccessibilityColors.cardDark),
            (ReadDailyAccessibilityColors.positiveTextDark, ReadDailyAccessibilityColors.accentSoftDark),
            (ReadDailyAccessibilityColors.attentionTextDark, ReadDailyAccessibilityColors.cardDark),
            (ReadDailyAccessibilityColors.attentionTextDark, ReadDailyAccessibilityColors.accentSoftDark),
            (ReadDailyAccessibilityColors.failureTextDark, ReadDailyAccessibilityColors.cardDark),
            (ReadDailyAccessibilityColors.failureTextDark, ReadDailyAccessibilityColors.accentSoftDark),
        ]

        for (foreground, background) in pairs {
            #expect(foreground.contrastRatio(against: background) >= 4.5)
        }
    }
}
