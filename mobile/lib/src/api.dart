import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import 'i18n.dart';
import 'settings.dart';

class ApiError implements Exception {
  ApiError(this.status, this.message, {this.code = ''});
  final int status;
  final String message;
  final String code;
  @override
  String toString() => message;
}

class Download {
  Download(this.name, this.bytes);
  final String name;
  final List<int> bytes;
}

/// The server's routes, with this phone's key. Nothing here is cached: the PC is the one place the conversations live.
class Api {
  Api(this.settings);
  final AppSettings settings;
  static const _timeout = Duration(seconds: 25);

  Uri uri(String path, [Map<String, String>? query]) {
    final base = settings.base;
    if (base == null) throw ApiError(0, Texts.t('err.network'));
    return base.replace(path: path.startsWith('/') ? path : '/$path', queryParameters: query);
  }

  Map<String, String> get headers => {'Authorization': 'Bearer ${settings.key}'};

  Never _fail(http.Response r) {
    String message = Texts.t('err.http', [r.statusCode]);
    String code = '';
    try {
      final j = jsonDecode(utf8.decode(r.bodyBytes));
      final e = j is Map ? j['error'] : null;
      if (e is Map) {
        message = '${e['message'] ?? message}';
        code = '${e['code'] ?? ''}';
      }
    } catch (_) {}
    if (r.statusCode == 401) message = Texts.t('err.unauthorized');
    throw ApiError(r.statusCode, message, code: code);
  }

  Future<http.Response> _send(Future<http.Response> Function() call) async {
    try {
      return await call().timeout(_timeout);
    } on TimeoutException {
      throw ApiError(0, Texts.t('err.network'));
    } on SocketException {
      throw ApiError(0, Texts.t('err.network'));
    } on http.ClientException {
      throw ApiError(0, Texts.t('err.network'));
    }
  }

  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query}) async {
    final r = await _send(() => http.get(uri(path, query), headers: headers));
    if (r.statusCode >= 400) _fail(r);
    final j = jsonDecode(utf8.decode(r.bodyBytes));
    return j is Map<String, dynamic> ? j : <String, dynamic>{'value': j};
  }

  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body]) async {
    final r = await _send(() => http.post(uri(path), headers: {...headers, 'Content-Type': 'application/json'}, body: jsonEncode(body ?? {})));
    if (r.statusCode >= 400) _fail(r);
    if (r.bodyBytes.isEmpty) return {};
    final j = jsonDecode(utf8.decode(r.bodyBytes));
    return j is Map<String, dynamic> ? j : <String, dynamic>{'value': j};
  }

  /// Tells the app that its address and key are good, and the name of the key.
  Future<Map<String, dynamic>> hello() => get('/api/remote/hello');

  Future<Download> download(String path, [Map<String, String>? query]) async {
    final r = await _send(() => http.get(uri(path, query), headers: headers));
    if (r.statusCode >= 400) _fail(r);
    var name = 'file';
    final disposition = r.headers['content-disposition'] ?? '';
    final star = RegExp(r"filename\*=UTF-8''([^;]+)").firstMatch(disposition);
    final plain = RegExp(r'filename="?([^";]+)"?').firstMatch(disposition);
    if (star != null) {
      name = Uri.decodeComponent(star.group(1)!);
    } else if (plain != null) {
      name = plain.group(1)!;
    }
    return Download(name, r.bodyBytes);
  }

  /// A file attached to a conversation: the bytes are the body of the request, the name is in the address.
  Future<Map<String, dynamic>> upload(String sid, String name, List<int> bytes) async {
    final target = uri('/api/attachments', {'session': sid, 'name': name});
    final r = await _send(() => http.post(target, headers: {...headers, 'Content-Type': 'application/octet-stream'}, body: bytes));
    if (r.statusCode >= 400) _fail(r);
    final j = jsonDecode(utf8.decode(r.bodyBytes));
    return (j is Map && j['attachment'] is Map) ? Map<String, dynamic>.from(j['attachment'] as Map) : <String, dynamic>{};
  }
}
