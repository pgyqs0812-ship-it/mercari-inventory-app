import Foundation
import SwiftData

@MainActor
final class ProductListViewModel: ObservableObject {
    @Published var isLoading = false
    @Published var error: String?
    @Published var searchText = ""

    func sync(context: ModelContext) async {
        isLoading = true
        error = nil
        do {
            let response = try await APIClient.shared.products()
            for apiProduct in response.items {
                let targetId = apiProduct.id
                let descriptor = FetchDescriptor<Product>(
                    predicate: #Predicate { $0.itemId == targetId }
                )
                if let existing = try? context.fetch(descriptor).first {
                    context.delete(existing)
                }
                context.insert(Product(
                    itemId:     apiProduct.id,
                    title:      apiProduct.title,
                    price:      apiProduct.price,
                    priceInt:   apiProduct.priceInt,
                    status:     apiProduct.status,
                    visibility: apiProduct.visibility,
                    itemURL:    apiProduct.itemURL,
                    createdAt:  apiProduct.createdAt,
                    syncedAt:   apiProduct.syncedAt
                ))
            }
            try? context.save()
        } catch {
            self.error = error.localizedDescription
        }
        isLoading = false
    }
}
