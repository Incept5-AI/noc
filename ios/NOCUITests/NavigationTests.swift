import XCTest

/// Taps through the Models tab and checks the detail screen that opens is the machine tapped —
/// and that it is still there after a few 2.5 s refreshes (the regression: rows keyed on port
/// collided across hosts, and the push popped back to the list).
@MainActor
final class NavigationTests: XCTestCase {
    private func launch() throws -> XCUIApplication {
        let env = ProcessInfo.processInfo.environment
        guard let tok = env["NOC_TOKEN"] else { throw XCTSkip("set TEST_RUNNER_NOC_TOKEN") }
        let app = XCUIApplication()
        app.launchEnvironment["NOC_DEBUG_TOKEN"] = tok
        if let b = env["NOC_BASE"] { app.launchEnvironment["NOC_DEBUG_BASE"] = b }
        app.launch()
        // get there the way a person does: start on Fleet, open a machine, come back, switch tab
        // TEST_RUNNER_NOC_HOST picks the card to open (default matches demo/demo.py's fleet)
        let pick = env["NOC_HOST"] ?? "gpu-node"
        let first = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", pick)).firstMatch
        if first.waitForExistence(timeout: 15) {
            first.tap(); sleep(1)
            app.navigationBars.buttons.element(boundBy: 0).tap()
        }
        app.tabBars.buttons["Models"].tap()
        return app
    }

    private func check(_ app: XCUIApplication, row: XCUIElement) {
        let host = String(row.label.split(separator: ",")[0]).trimmingCharacters(in: .whitespaces)
        row.tap()
        let bar = app.navigationBars[host]
        XCTAssertTrue(bar.waitForExistence(timeout: 5), "detail for \(host) did not open (label: \(row.label))")
        sleep(8)
        XCTAssertTrue(bar.exists, "detail for \(host) popped after refreshes")
        bar.buttons.element(boundBy: 0).tap()
    }

    func testServingRowOpensItsMachine() throws {
        let app = try launch()
        let serving = app.buttons.matching(NSPredicate(format: "label CONTAINS ' req' OR label CONTAINS ' run'"))
        guard serving.firstMatch.waitForExistence(timeout: 15) else { throw XCTSkip("nothing serving with stats") }
        check(app, row: serving.firstMatch)
    }
}
