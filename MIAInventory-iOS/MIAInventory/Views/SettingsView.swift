import SwiftUI

struct SettingsView: View {
    @ObservedObject private var settings = AppSettings.shared
    @StateObject private var vm = SettingsViewModel()

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack {
                        Text("IP アドレス")
                        Spacer()
                        TextField("例: 192.168.1.10", text: $settings.macIP)
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.numbersAndPunctuation)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                    }
                    HStack {
                        Text("ポート番号")
                        Spacer()
                        TextField("5050", text: $settings.port)
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.numberPad)
                    }
                    HStack {
                        Text("API トークン")
                        Spacer()
                        SecureField("トークンを入力", text: $settings.token)
                            .multilineTextAlignment(.trailing)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                    }
                } header: {
                    Text("Mac 接続設定")
                } footer: {
                    Text("Mac の MIA Inventory → 設定 → iPhone 連携 で IP アドレスとトークンを確認してください。")
                }

                Section("接続テスト") {
                    Button {
                        Task { await vm.testConnection() }
                    } label: {
                        HStack {
                            if vm.isTesting {
                                ProgressView()
                                    .scaleEffect(0.8)
                                    .padding(.trailing, 4)
                            }
                            Text(vm.isTesting ? "テスト中…" : "接続テスト")
                        }
                    }
                    .disabled(vm.isTesting)

                    if let result = vm.connectionResult {
                        Text(result)
                            .font(.footnote)
                            .foregroundStyle(result.hasPrefix("✓") ? Color.green : Color.red)
                    }
                }

                Section {
                    LabeledContent("ベース URL") {
                        Text(settings.baseURL)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("接続情報")
                }

                Section("アプリ情報") {
                    LabeledContent("バージョン", value: "ios-v0.1.0")
                    LabeledContent("対応 Mac API", value: "v1.7.0+")
                }
            }
            .navigationTitle("設定")
        }
    }
}
