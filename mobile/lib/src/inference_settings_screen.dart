import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

import 'i18n.dart';
import 'settings.dart';

/// The server's page, for the conversation list and any conversation's own settings: the key goes once as ?key= (app.js reads it, the
/// same way the desktop app's WebView2 hands it over), the fragment opens straight on the chat view.
String inferenceSettingsUrl(Uri base, String key) => '${base.replace(queryParameters: {...base.queryParameters, 'key': key})}#chat';

/// The same inference settings as the web and desktop app (presets, effort, system prompt, compaction, browsing, permission
/// level, checkpoints, structured output, sampling...): rather than a second, smaller copy of that whole panel in Dart, this
/// opens the real one, the page the PC already serves, inside the app. The key goes once as ?key=, read by app.js; the page
/// opens on the conversation list so any conversation's own gear icon reaches its settings, the same as on a PC.
class InferenceSettingsScreen extends StatefulWidget {
  const InferenceSettingsScreen({super.key, required this.settings});
  final AppSettings settings;

  @override
  State<InferenceSettingsScreen> createState() => _InferenceSettingsScreenState();
}

class _InferenceSettingsScreenState extends State<InferenceSettingsScreen> {
  late final WebViewController _controller;
  bool _loading = true;
  String? _problem;

  @override
  void initState() {
    super.initState();
    final base = widget.settings.base;
    if (base == null) {
      _problem = Texts.t('err.network');
      _loading = false;
      _controller = WebViewController();
      return;
    }
    final url = inferenceSettingsUrl(base, widget.settings.key);
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setNavigationDelegate(NavigationDelegate(
        onPageFinished: (_) => setState(() => _loading = false),
        onWebResourceError: (e) => setState(() {
          _loading = false;
          _problem = e.description;
        }),
      ));
    _open(base, url);
  }

  /// A web view cannot send the key in a header with the page and the files it is made of, and the server asks for the key on every request:
  /// the key goes in a cookie first, which the server reads for the page and its files and for nothing else (the page's own calls carry it as ever).
  Future<void> _open(Uri base, String url) async {
    try {
      await WebViewCookieManager().setCookie(WebViewCookie(name: 'tadamoo_key', value: widget.settings.key, domain: base.host, path: '/'));
    } catch (_) {
      // without the cookie the server refuses the page, and the error shows
    }
    await _controller.loadRequest(Uri.parse(url));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(Texts.t('infset.title'))),
      body: Stack(children: [
        if (widget.settings.base != null) WebViewWidget(controller: _controller),
        if (_loading) const Center(child: CircularProgressIndicator()),
        if (_problem != null)
          Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Text(_problem!, textAlign: TextAlign.center),
            ),
          ),
      ]),
    );
  }
}
