import Foundation

@MainActor
final class SettingsViewModel: ObservableObject {
    @Published var connectionResult: String?
    @Published var isTesting = false

    func testConnection() async {
        guard AppSettings.shared.isConfigured else {
            connectionResult = "⚠ IP アドレスとトークンを入力してください"
            return
        }
        isTesting = true
        connectionResult = nil
        do {
            let ping = try await APIClient.shared.ping()
            connectionResult = "✓ 接続成功 — \(ping.app) \(ping.version)"
        } catch {
            connectionResult = "✗ 接続失敗: \(error.localizedDescription)"
        }
        isTesting = false
    }
}
