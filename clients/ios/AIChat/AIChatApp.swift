import SwiftUI

@main
struct AIChatApp: App {
    var body: some Scene {
        WindowGroup {
            BrowserContainer()
        }
    }
}

private struct BrowserContainer: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> BrowserViewController {
        BrowserViewController()
    }

    func updateUIViewController(_ uiViewController: BrowserViewController, context: Context) {}
}
