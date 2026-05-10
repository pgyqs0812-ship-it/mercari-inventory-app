import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            DashboardView()
                .tabItem { Label("ダッシュボード", systemImage: "square.grid.2x2") }
            ProductListView()
                .tabItem { Label("商品", systemImage: "shippingbox") }
            SettingsView()
                .tabItem { Label("設定", systemImage: "gearshape") }
        }
    }
}
