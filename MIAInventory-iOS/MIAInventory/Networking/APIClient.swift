import Foundation

// MARK: - Response models

struct APIPing: Decodable {
    let ok: Bool
    let version: String
    let app: String
}

struct APIStats: Decodable {
    let total: Int
    let active: Int
    let stopped: Int
    let trading: Int
    let sold: Int
    let lastSync: String

    enum CodingKeys: String, CodingKey {
        case total, active, stopped, trading, sold
        case lastSync = "last_sync"
    }
}

struct APIProduct: Decodable {
    let id: String
    let title: String
    let price: String
    let priceInt: Int
    let status: String
    let visibility: String
    let itemURL: String
    let createdAt: String
    let syncedAt: String

    enum CodingKeys: String, CodingKey {
        case id, title, price, status, visibility
        case priceInt  = "price_int"
        case itemURL   = "item_url"
        case createdAt = "created_at"
        case syncedAt  = "synced_at"
    }
}

struct APIProductsResponse: Decodable {
    let count: Int
    let items: [APIProduct]
}

struct APISyncStatus: Decodable {
    let running: Bool
    let done: Bool
    let step: String
    let fetched: Int
    let error: String
}

// MARK: - Error

enum APIError: LocalizedError {
    case invalidURL
    case unauthorized
    case serverError(Int)
    case decodingError(Error)
    case networkError(Error)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "無効な URL です。設定の IP アドレスを確認してください。"
        case .unauthorized:
            return "認証エラー: API トークンを確認してください。"
        case .serverError(let code):
            return "サーバーエラー: HTTP \(code)"
        case .decodingError(let err):
            return "データ解析エラー: \(err.localizedDescription)"
        case .networkError(let err):
            return err.localizedDescription
        }
    }
}

// MARK: - Client

final class APIClient {
    static let shared = APIClient()
    private init() {}

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest  = 10
        cfg.timeoutIntervalForResource = 30
        return URLSession(configuration: cfg)
    }()

    private func request<T: Decodable>(
        _ path: String,
        requiresAuth: Bool = true
    ) async throws -> T {
        let settings = AppSettings.shared
        guard let url = URL(string: settings.baseURL + path) else {
            throw APIError.invalidURL
        }
        var req = URLRequest(url: url)
        if requiresAuth {
            req.setValue("Bearer \(settings.token)", forHTTPHeaderField: "Authorization")
        }
        do {
            let (data, response) = try await session.data(for: req)
            if let http = response as? HTTPURLResponse {
                switch http.statusCode {
                case 401:   throw APIError.unauthorized
                case 400...: throw APIError.serverError(http.statusCode)
                default:    break
                }
            }
            do {
                return try JSONDecoder().decode(T.self, from: data)
            } catch {
                throw APIError.decodingError(error)
            }
        } catch let err as APIError {
            throw err
        } catch {
            throw APIError.networkError(error)
        }
    }

    func ping() async throws -> APIPing {
        try await request("/api/ping", requiresAuth: false)
    }

    func stats() async throws -> APIStats {
        try await request("/api/stats")
    }

    func products(q: String = "", status: String = "") async throws -> APIProductsResponse {
        var path = "/api/products"
        var params: [String] = []
        if !q.isEmpty,
           let enc = q.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
            params.append("q=\(enc)")
        }
        if !status.isEmpty { params.append("status=\(status)") }
        if !params.isEmpty { path += "?" + params.joined(separator: "&") }
        return try await request(path)
    }

    func product(id: String) async throws -> APIProduct {
        try await request("/api/products/\(id)")
    }

    func syncStatus() async throws -> APISyncStatus {
        try await request("/api/sync/status")
    }
}
