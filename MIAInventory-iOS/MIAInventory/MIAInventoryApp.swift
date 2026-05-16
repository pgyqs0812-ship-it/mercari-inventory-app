import SwiftUI
import SwiftData

@main
struct MIAInventoryApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .modelContainer(for: Product.self)
    }
}
