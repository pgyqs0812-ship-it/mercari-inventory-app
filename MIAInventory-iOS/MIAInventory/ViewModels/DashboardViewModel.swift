import Foundation

@MainActor
final class DashboardViewModel: ObservableObject {
    @Published var stats: APIStats?
    @Published var isLoading = false
    @Published var error: String?

    func load() async {
        guard AppSettings.shared.isConfigured else { return }
        isLoading = true
        error = nil
        do {
            stats = try await APIClient.shared.stats()
        } catch {
            self.error = error.localizedDescription
        }
        isLoading = false
    }
}
