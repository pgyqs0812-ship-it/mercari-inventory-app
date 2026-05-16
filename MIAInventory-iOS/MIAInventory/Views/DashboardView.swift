import SwiftUI

struct DashboardView: View {
    @StateObject private var vm = DashboardViewModel()

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading {
                    ProgressView("読み込み中…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let stats = vm.stats {
                    statsContent(stats)
                } else if let err = vm.error {
                    ContentUnavailableView(
                        "接続エラー",
                        systemImage: "wifi.slash",
                        description: Text(err)
                    )
                } else {
                    ContentUnavailableView(
                        "設定が必要です",
                        systemImage: "gearshape",
                        description: Text("設定タブで Mac の IP アドレスと API トークンを入力してください")
                    )
                }
            }
            .navigationTitle("ダッシュボード")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await vm.load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(vm.isLoading)
                }
            }
        }
        .task { await vm.load() }
    }

    @ViewBuilder
    private func statsContent(_ stats: APIStats) -> some View {
        List {
            Section("在庫サマリー") {
                KPIRow(label: "合計",       value: stats.total,   color: .primary)
                KPIRow(label: "出品中",     value: stats.active,  color: .green)
                KPIRow(label: "公開停止中", value: stats.stopped, color: .orange)
                KPIRow(label: "取引中",     value: stats.trading, color: .yellow)
                KPIRow(label: "売却済み",   value: stats.sold,    color: .blue)
            }
            Section("最終同期") {
                Text(stats.lastSync.isEmpty ? "未同期" : stats.lastSync)
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
            }
        }
    }
}

private struct KPIRow: View {
    let label: String
    let value: Int
    let color: Color

    var body: some View {
        HStack {
            Text(label)
            Spacer()
            Text("\(value)")
                .fontWeight(.semibold)
                .foregroundStyle(color)
        }
    }
}
