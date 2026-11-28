import AppKit
import Darwin
import OSLog

@MainActor
final class SceneTurnApp: NSObject, NSApplicationDelegate {
    private let logger = Logger(subsystem: "online.ethan-g.sceneturn.codex-bridge", category: "launcher")
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let status = NSMenuItem(title: "等待 SceneTurn 网页唤起", action: nil, keyEquivalent: "")
    private var bridge: Process?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem.button?.title = "SceneTurn"
        statusItem.button?.toolTip = "SceneTurn Codex 连接程序"

        let menu = NSMenu()
        menu.addItem(status)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出连接程序", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quit.target = NSApplication.shared
        menu.addItem(quit)
        statusItem.menu = menu
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard urls.contains(where: { $0.absoluteString == "sceneturn-codex://open" }) else { return }
        guard bridge?.isRunning != true else { return }
        logger.notice("Received SceneTurn launch request")
        startBridge()
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard let bridge, bridge.isRunning else { return }
        let pid = bridge.processIdentifier
        if getpgid(pid) == pid {
            kill(-pid, SIGTERM)
        } else {
            bridge.terminate()
        }
    }

    private func startBridge() {
        guard let productPath = Bundle.main.object(forInfoDictionaryKey: "SceneTurnProductRoot") as? String,
              let npmPath = Bundle.main.object(forInfoDictionaryKey: "SceneTurnNpmExecutable") as? String,
              let nodeDirectory = Bundle.main.object(forInfoDictionaryKey: "SceneTurnNodeDirectory") as? String,
              productPath.hasPrefix("/"), npmPath.hasPrefix("/"), nodeDirectory.hasPrefix("/") else {
            status.title = "请重新安装 SceneTurn 连接程序"
            logger.error("Missing installation configuration")
            return
        }

        let product = URL(fileURLWithPath: productPath, isDirectory: true)
        let package = product.appendingPathComponent("scripts/codex-bridge", isDirectory: true)
        let temporary = product.appendingPathComponent(".intermediate/codex-macos-tmp", isDirectory: true)
        let npm = URL(fileURLWithPath: npmPath)
        let files = FileManager.default
        guard files.isExecutableFile(atPath: npm.path),
              files.fileExists(atPath: package.appendingPathComponent("package.json").path),
              files.fileExists(atPath: temporary.path) else {
            status.title = "本机连接文件缺失，请重新安装"
            logger.error("Missing local bridge runtime")
            return
        }

        let process = Process()
        process.executableURL = npm
        process.arguments = ["--prefix", package.path, "run", "start", "--", "serve"]
        process.currentDirectoryURL = product
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "\(nodeDirectory):\(npm.deletingLastPathComponent().path):/usr/bin:/bin:/usr/sbin:/sbin"
        environment["TMPDIR"] = temporary.path
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] completed in
            Task { @MainActor in
                guard let self, self.bridge === completed else { return }
                self.bridge = nil
                let code = completed.terminationStatus
                self.status.title = code == 0 ? "等待 SceneTurn 网页唤起" : "本机程序已停止，可从网页重试"
                self.logger.notice("Local bridge exited with status \(code, privacy: .public)")
            }
        }

        do {
            try process.run()
            bridge = process
            status.title = "本机程序运行中，连接状态见网页"
            logger.notice("Started local bridge process")
        } catch {
            status.title = "无法启动本机程序，请重新安装"
            logger.error("Could not start local bridge process, system code \((error as NSError).code, privacy: .public)")
        }
    }
}

@main
struct SceneTurnMain {
    @MainActor
    static func main() {
        let application = NSApplication.shared
        let sceneTurn = SceneTurnApp()
        application.delegate = sceneTurn
        withExtendedLifetime(sceneTurn) { application.run() }
    }
}
