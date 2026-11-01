import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// What the app knows about the server and about the person's preferences. Kept on the phone only.
class AppSettings extends ChangeNotifier {
  AppSettings._(this._prefs);
  final SharedPreferences _prefs;

  static Future<AppSettings> load() async => AppSettings._(await SharedPreferences.getInstance());

  /// "100.64.1.2:7232" or a full address; the key made for this phone on the PC (Settings > Phones).
  String get address => _prefs.getString('address') ?? '';
  String get key => _prefs.getString('key') ?? '';
  String get deviceName => _prefs.getString('name') ?? '';
  bool get autoSpeak => _prefs.getBool('autoSpeak') ?? false;
  bool get sendPlace => _prefs.getBool('sendPlace') ?? false;
  String get speechLocale => _prefs.getString('speechLocale') ?? '';
  String get language => _prefs.getString('language') ?? '';
  String get mode => _prefs.getString('mode') ?? 'default';

  /// The role the person last chose for conversations in a folder ('' until they choose one).
  String get agentRole => _prefs.getString('agentRole') ?? '';

  bool get configured => address.trim().isNotEmpty && key.trim().isNotEmpty;

  /// The server's base address with a scheme; null when none was written.
  Uri? get base {
    var text = address.trim();
    if (text.isEmpty) return null;
    if (!text.contains('://')) text = 'http://$text';
    final uri = Uri.tryParse(text);
    if (uri == null || uri.host.isEmpty) return null;
    return Uri(scheme: uri.scheme, host: uri.host, port: uri.hasPort ? uri.port : null);
  }

  Future<void> setServer({required String address, required String key, String name = ''}) async {
    await _prefs.setString('address', address.trim());
    await _prefs.setString('key', key.trim());
    if (name.trim().isNotEmpty) await _prefs.setString('name', name.trim());
    notifyListeners();
  }

  Future<void> setAutoSpeak(bool value) async {
    await _prefs.setBool('autoSpeak', value);
    notifyListeners();
  }

  Future<void> setSendPlace(bool value) async {
    await _prefs.setBool('sendPlace', value);
    notifyListeners();
  }

  Future<void> setSpeechLocale(String value) async {
    await _prefs.setString('speechLocale', value);
    notifyListeners();
  }

  Future<void> setLanguage(String value) async {
    await _prefs.setString('language', value);
    notifyListeners();
  }

  Future<void> setAgentRole(String value) async {
    await _prefs.setString('agentRole', value);
    notifyListeners();
  }

  Future<void> setMode(String value) async {
    await _prefs.setString('mode', value);
    notifyListeners();
  }
}
