import Testing
@testable import ConstructionReadingDeskCore

@Suite("Read Daily 八报仪表盘")
struct DailyReadingDashboardTests {
    @Test("固定报纸注册表包含八家报纸且顺序稳定")
    func fixedRegistryContainsEightSources() {
        #expect(NewspaperRegistry.dailySources.map(\.name) == [
            "人民日报", "光明日报", "经济日报", "中国建设报",
            "科技日报", "农民日报", "南方日报", "北京日报",
        ])
        #expect(Set(NewspaperRegistry.dailySources.map(\.id)).count == 8)
        #expect(NewspaperCategory.allCases.map(\.rawValue) == ["中央党报", "部委行业报", "地方党报"])
    }

    @Test("一天始终按固定分类展示八家报纸及可读状态")
    func dashboardFillsMissingSourcesAndMapsStatuses() {
        let issues = [
            IssueSummary(
                sourceID: "zgjsb",
                sourceName: "中国建设报",
                date: "2026-09-03",
                stage: "needs_review",
                readingStatus: "completed",
                pageCount: 8
            ),
            IssueSummary(
                sourceID: "rmrb",
                sourceName: "人民日报",
                date: "2026-09-03",
                stage: "failed"
            ),
        ]

        let day = DailyReadingDashboard.day(date: "2026-09-03", issues: issues)
        let entries = day.sections.flatMap(\.entries)

        #expect(entries.count == 8)
        #expect(entries.first(where: { $0.source.id == "zgjsb" })?.status == .readyForReview)
        #expect(entries.first(where: { $0.source.id == "zgjsb" })?.readingStatus == .completed)
        #expect(entries.first(where: { $0.source.id == "rmrb" })?.status == .failed)
        #expect(entries.first(where: { $0.source.id == "gmrb" })?.status == .notStarted)
        #expect(entries.first(where: { $0.source.id == "zgjsb" })?.status.accessibleLabel == "已读取，待校对")
        #expect(entries.first(where: { $0.source.id == "gmrb" })?.readingStatus.accessibleLabel == "今日未读")
    }

    @Test("仪表盘日期倒序且旧 inbox 结构仍可生成八报日视图")
    func dashboardGroupsLegacyInboxByDateDescending() {
        let issues = [
            IssueSummary(sourceID: "zgjsb", sourceName: "中国建设报", date: "2026-09-02", stage: "published"),
            IssueSummary(sourceID: "rmrb", sourceName: "人民日报", date: "2026-09-03", stage: "ready_to_publish"),
        ]

        let dashboard = DailyReadingDashboard(issues: issues)

        #expect(dashboard.dates == ["2026-09-03", "2026-09-02"])
        #expect(dashboard.day(for: "2026-09-03").sections.flatMap(\.entries).count == 8)
    }

    @Test("非发布报纸的复核完成状态不会伪装成待发布")
    func reviewCompleteIsDistinctFromPublishReady() throws {
        let payload = JSONValue.object([
            "date": .string("2026-09-03"),
            "newspapers": .array([.object([
                "source": .string("rmrb"),
                "available": .bool(true),
                "status": .string("review_complete"),
                "acquisition_status": .string("complete"),
                "review_status": .string("complete"),
                "publish_status": .string("not_supported"),
            ])]),
        ])

        let day = try WorkbenchPayloadMapper().dailyDashboard(from: payload)
        let row = try #require(day.entries.first { $0.source.id == "rmrb" })
        #expect(row.status == .reviewComplete)
        #expect(row.status.accessibleLabel == "校对完成")
    }

    @Test("生命周期状态优先于已完成的抓取状态")
    func lifecycleStatusWinsOverAcquisitionStatus() throws {
        let payload = JSONValue.object([
            "date": .string("2026-09-03"),
            "newspapers": .array([
                .object([
                    "source": .string("zgjsb"),
                    "available": .bool(true),
                    "status": .string("published"),
                    "acquisition_status": .string("complete"),
                ]),
                .object([
                    "source": .string("rmrb"),
                    "available": .bool(true),
                    "status": .string("ready_to_publish"),
                    "acquisition_status": .string("complete"),
                ]),
            ]),
        ])

        let day = try WorkbenchPayloadMapper().dailyDashboard(from: payload)

        #expect(day.entries.first { $0.source.id == "zgjsb" }?.status == .published)
        #expect(day.entries.first { $0.source.id == "rmrb" }?.status == .readyToPublish)
    }

    @Test("日期菜单始终包含今天和当前选择")
    func dateChoicesIncludeTodayAndSelection() {
        #expect(ReadingDatePolicy.menuDates(
            availableDates: ["2026-09-02", "2026-08-31"],
            selectedDate: "2026-09-01",
            today: "2026-09-03"
        ) == ["2026-09-03", "2026-09-02", "2026-09-01", "2026-08-31"])
        #expect(ReadingDatePolicy.initialSelection(
            selectedDate: nil,
            availableDates: ["2026-09-02"],
            today: "2026-09-03"
        ) == "2026-09-03")
    }

    @Test("设置草稿仅在数据路径变化时要求重载工作区")
    func settingsDraftDistinguishesWeatherFromDataContext() {
        let current = ReadDailySettingsValues(
            repositoryPath: "/Applications/Read Daily.app/engine",
            archivePath: "/Data/archive-a",
            vaultPath: "/Data/vault-a",
            weatherText: "晴 · 26℃"
        )
        let weatherOnly = ReadDailySettingsValues(
            repositoryPath: current.repositoryPath,
            archivePath: current.archivePath,
            vaultPath: current.vaultPath,
            weatherText: "多云 · 24℃"
        )
        let movedArchive = ReadDailySettingsValues(
            repositoryPath: current.repositoryPath,
            archivePath: "/Data/archive-b",
            vaultPath: current.vaultPath,
            weatherText: current.weatherText
        )

        #expect(weatherOnly.changesDataContext(comparedTo: current) == false)
        #expect(movedArchive.changesDataContext(comparedTo: current) == true)
    }

    @Test("未配置天气时明确显示占位且不伪造数据")
    func weatherRequiresExplicitLocalConfiguration() {
        #expect(LocalWeatherSummary(configuredText: "").displayText == "天气未配置")
        #expect(LocalWeatherSummary(configuredText: "  ").isConfigured == false)
        #expect(LocalWeatherSummary(configuredText: "晴 · 26℃").displayText == "晴 · 26℃")
    }
}
