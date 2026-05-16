import SwiftUI

struct ProductDetailView: View {
    let product: Product

    private var displayStatus: String {
        product.visibility == "stopped" ? "公開停止中" : product.status
    }

    var body: some View {
        List {
            Section("商品情報") {
                InfoRow(label: "タイトル",   value: product.title)
                InfoRow(label: "価格",       value: product.price)
                InfoRow(label: "ステータス", value: displayStatus)
            }
            Section("日時") {
                InfoRow(label: "出品日",   value: product.createdAt)
                InfoRow(label: "同期日時", value: product.syncedAt)
            }
            if !product.itemURL.isEmpty, let url = URL(string: product.itemURL) {
                Section("リンク") {
                    Link(destination: url) {
                        HStack {
                            Text("Mercari で開く")
                            Spacer()
                            Image(systemName: "safari")
                                .foregroundStyle(.blue)
                        }
                    }
                }
            }
        }
        .navigationTitle("商品詳細")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct InfoRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Text(label)
                .foregroundStyle(.secondary)
                .frame(width: 80, alignment: .leading)
            Text(value.isEmpty ? "—" : value)
                .multilineTextAlignment(.leading)
        }
    }
}
