import SwiftUI
import SwiftData

struct ProductListView: View {
    @Environment(\.modelContext) private var context
    @Query(sort: \Product.fetchedAt, order: .reverse) private var products: [Product]
    @StateObject private var vm = ProductListViewModel()

    private var displayed: [Product] {
        guard !vm.searchText.isEmpty else { return products }
        return products.filter {
            $0.title.localizedCaseInsensitiveContains(vm.searchText)
        }
    }

    var body: some View {
        NavigationStack {
            List(displayed) { product in
                NavigationLink(destination: ProductDetailView(product: product)) {
                    ProductRow(product: product)
                }
            }
            .searchable(text: $vm.searchText, prompt: "商品名で検索")
            .overlay {
                if products.isEmpty && !vm.isLoading {
                    ContentUnavailableView(
                        "商品がありません",
                        systemImage: "shippingbox",
                        description: Text("↑ 右上の同期ボタンをタップしてデータを取得してください")
                    )
                }
            }
            .navigationTitle("商品一覧 (\(products.count))")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await vm.sync(context: context) }
                    } label: {
                        if vm.isLoading {
                            ProgressView().scaleEffect(0.8)
                        } else {
                            Image(systemName: "arrow.clockwise")
                        }
                    }
                    .disabled(vm.isLoading)
                }
            }
            .alert("同期エラー", isPresented: Binding(
                get: { vm.error != nil },
                set: { if !$0 { vm.error = nil } }
            )) {
                Button("OK") { vm.error = nil }
            } message: {
                Text(vm.error ?? "")
            }
        }
    }
}

private struct ProductRow: View {
    let product: Product

    private var displayStatus: String {
        product.visibility == "stopped" ? "公開停止中" : product.status
    }

    private var statusColor: Color {
        switch product.status {
        case "出品中":
            return product.visibility == "stopped" ? .orange : .green
        case "取引中":
            return .yellow
        case "売却済み", "販売履歴":
            return .blue
        default:
            return .secondary
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(product.title)
                .lineLimit(2)
            HStack {
                Text(product.price)
                    .fontWeight(.semibold)
                    .font(.subheadline)
                Spacer()
                Text(displayStatus)
                    .font(.caption)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(statusColor.opacity(0.12))
                    .foregroundStyle(statusColor)
                    .clipShape(Capsule())
            }
        }
        .padding(.vertical, 2)
    }
}
