import Foundation
import SwiftData

@Model
final class Product {
    @Attribute(.unique) var itemId: String
    var title: String
    var price: String
    var priceInt: Int
    var status: String
    var visibility: String
    var itemURL: String
    var createdAt: String
    var syncedAt: String
    var fetchedAt: Date

    init(
        itemId: String,
        title: String,
        price: String,
        priceInt: Int,
        status: String,
        visibility: String,
        itemURL: String,
        createdAt: String,
        syncedAt: String
    ) {
        self.itemId     = itemId
        self.title      = title
        self.price      = price
        self.priceInt   = priceInt
        self.status     = status
        self.visibility = visibility
        self.itemURL    = itemURL
        self.createdAt  = createdAt
        self.syncedAt   = syncedAt
        self.fetchedAt  = Date()
    }
}
