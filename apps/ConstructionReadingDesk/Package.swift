// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ConstructionReadingDesk",
    defaultLocalization: "zh-Hans",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "ConstructionReadingDeskCore", targets: ["ConstructionReadingDeskCore"]),
        .executable(name: "ConstructionReadingDesk", targets: ["ConstructionReadingDesk"]),
    ],
    targets: [
        .target(name: "ConstructionReadingDeskCore"),
        .executableTarget(
            name: "ConstructionReadingDesk",
            dependencies: ["ConstructionReadingDeskCore"],
            exclude: ["Resources"]
        ),
        .testTarget(
            name: "ConstructionReadingDeskCoreTests",
            dependencies: ["ConstructionReadingDeskCore", "ConstructionReadingDesk"],
            swiftSettings: [
                // Some standalone Command Line Tools installations ship Swift
                // Testing outside the compiler's default framework search path.
                .unsafeFlags([
                    "-F", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                ], .when(platforms: [.macOS])),
            ],
            linkerSettings: [
                .unsafeFlags([
                    "-F", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                    "-framework", "Testing",
                    "-Xlinker", "-rpath",
                    "-Xlinker", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                    "-Xlinker", "-rpath",
                    "-Xlinker", "/Library/Developer/CommandLineTools/Library/Developer/usr/lib",
                ], .when(platforms: [.macOS])),
            ]
        ),
    ]
)
