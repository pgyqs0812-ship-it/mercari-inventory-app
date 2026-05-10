import Foundation
import Combine

final class AppSettings: ObservableObject {
    static let shared = AppSettings()
    private init() {}

    @Published var macIP: String = UserDefaults.standard.string(forKey: "mia.macIP") ?? "" {
        didSet { UserDefaults.standard.set(macIP, forKey: "mia.macIP") }
    }
    @Published var port: String = UserDefaults.standard.string(forKey: "mia.port") ?? "5050" {
        didSet { UserDefaults.standard.set(port, forKey: "mia.port") }
    }
    @Published var token: String = UserDefaults.standard.string(forKey: "mia.token") ?? "" {
        didSet { UserDefaults.standard.set(token, forKey: "mia.token") }
    }

    var baseURL: String { "http://\(macIP):\(port)" }
    var isConfigured: Bool { !macIP.isEmpty && !token.isEmpty }
}
