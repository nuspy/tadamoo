/// Addresses for a person who is on another device (docs/remote-devices.md). The app is always away from the machine the server runs on, so a link in an answer that names that machine
/// (localhost:PORT, 127.0.0.1, 0.0.0.0) would open the phone's own address. These rules turn such text into what works from here, the same rules as the page (serve/web/netaddr.js):
///  - an address of the server's machine becomes the host the app reaches the server at (the port and the rest as written);
///  - a file route of the server written as an address becomes the reference the app resolves with its own key (tadamoo-file:, tadamoo-path:), so a picture loads from the network address;
///  - an app pointed at a loopback address (an emulator, a tunnel on the phone) is at the machine itself: nothing is changed.
library;

const _loop = r'(?:localhost|[A-Za-z0-9-]+\.localhost|127(?:\.\d{1,3}){3}|\[::1\]|0\.0\.0\.0|\[::\])';
final _url = RegExp(r'\b(https?|wss?)://(' + _loop + r')(:\d{1,5})?((?:[/?#][^\s<>()\[\]\x22\x27\x60]*)?)', caseSensitive: false);
final _outputRoute = RegExp(r'(!?\[[^\]\n]*\]\()https?://' + _loop + r'(?::\d{1,5})?/api/outputs/[A-Za-z0-9_-]+/([a-f0-9]{12})/file[^)\s]*\)', caseSensitive: false);
final _sessionFile = RegExp(r'(!?\[[^\]\n]*\]\()https?://' + _loop + r'(?::\d{1,5})?/api/agent/sessions/[A-Za-z0-9_-]+/file\?([^)\s]*)\)', caseSensitive: false);
final _loopName = RegExp(r'^(localhost|[^.]+\.localhost)$', caseSensitive: false);

bool isLoopbackHost(String host) {
  final h = host.toLowerCase().replaceAll(RegExp(r'^\[|\]$'), '');
  if (h.isEmpty) return false;
  if (_loopName.hasMatch(h)) return true;
  return RegExp(r'^127(\.\d{1,3}){3}$').hasMatch(h) || h == '::1' || h == '0.0.0.0' || h == '::';
}

/// `text` with the addresses of the server's machine made reachable from where the app is, given the address the app reaches the server at (null: none configured).
String fixAddresses(String text, Uri? base) {
  if (base == null || base.host.isEmpty || isLoopbackHost(base.host) || text.isEmpty) return text;
  var out = text.replaceAllMapped(_outputRoute, (m) => '${m.group(1)}tadamoo-file:${m.group(2)})');
  out = out.replaceAllMapped(_sessionFile, (m) {
    final path = Uri.splitQueryString(m.group(2) ?? '')['path'];
    return path == null || path.isEmpty ? m.group(0)! : '${m.group(1)}tadamoo-path:${Uri.encodeFull(path).replaceAll(')', '%29')})';
  });
  final host = base.host.contains(':') ? '[${base.host}]' : base.host;
  return out.replaceAllMapped(_url, (m) => '${m.group(1)}://$host${m.group(3) ?? ''}${m.group(4) ?? ''}');
}
