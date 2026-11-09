import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_engines.dart';
import 'secret_store.dart';

/// What the app knows about the server and about the person's preferences. Kept on the phone only.
class AppSettings extends ChangeNotifier {
  AppSettings._(this._prefs, this._secrets, this._log);
  final SharedPreferences _prefs;
  final SecretStore _secrets;
  final void Function(String) _log;

  static const _keyName = 'key';
  String _key = '';
  bool _keyIsSecure = true;

  /// Reads the settings. The phone's key lives in [secrets] (the Android Keystore); a key an older version left in the preferences is moved
  /// there once. When the secure store cannot be used the key stays in the preferences and [keyIsSecure] says so.
  static Future<AppSettings> load({SecretStore? secrets, void Function(String)? log}) async {
    final settings = AppSettings._(await SharedPreferences.getInstance(), secrets ?? KeystoreSecretStore(), log ?? debugPrint);
    await settings._loadKey();
    return settings;
  }

  Future<void> _loadKey() async {
    final old = _prefs.getString(_keyName) ?? '';
    try {
      if (old.isNotEmpty) {
        await _secrets.write(_keyName, old);
        if (await _secrets.read(_keyName) != old) throw StateError('the key did not read back');
        _key = old;
        await _prefs.remove(_keyName);
        _log('settings: the phone key moved from the preferences to the secure store');
      } else {
        _key = await _secrets.read(_keyName) ?? '';
      }
      _keyIsSecure = true;
    } catch (e) {
      _key = old;
      _keyIsSecure = false;
      _log('settings: secure store unavailable ($e); the phone key stays in the plain preferences');
    }
  }

  /// False when the key is in the plain preferences because the secure store failed; the Settings page tells the person.
  bool get keyIsSecure => _keyIsSecure;

  /// "100.64.1.2:7232" or a full address; the key made for this phone on the PC (Settings > Phones).
  String get address => _prefs.getString('address') ?? '';
  String get key => _key;
  String get deviceName => _prefs.getString('name') ?? '';
  bool get autoSpeak => _prefs.getBool('autoSpeak') ?? false;
  bool get sendPlace => _prefs.getBool('sendPlace') ?? false;
  String get speechLocale => _prefs.getString('speechLocale') ?? '';
  String get language => _prefs.getString('language') ?? '';
  String get mode => _prefs.getString('mode') ?? 'default';

  /// The Avatar tab: how the avatar is drawn ('video' from the PC, 'wav2lip' or 'visemes' on the phone), who answers ('direct' or 'harness')
  /// and the personality ('' until the person chooses one; the PC's own default then stands).
  String get avatarMode => _prefs.getString('avatarMode') ?? 'visemes';
  String get avatarBrain => _prefs.getString('avatarBrain') ?? '';
  String get avatarPersona => _prefs.getString('avatarPersona') ?? '';

  /// Avatar on (false: voice only), the picture size in pixels, its quality, where the sound goes ('speaker', 'bluetooth' or 'earpiece'),
  /// whether the speakerphone may be full duplex, and what the last audio test found (null: it was never run).
  bool get avatarEnabled => _prefs.getBool('avatarEnabled') ?? true;
  int get avatarResolution => avatarResolutions.contains(_prefs.getInt('avatarResolution')) ? _prefs.getInt('avatarResolution')! : defaultAvatarResolution;
  AvatarQuality get avatarQuality => AvatarQuality.fromName(_prefs.getString('avatarQuality') ?? '');
  String get audioRoute => const ['speaker', 'bluetooth', 'earpiece'].contains(_prefs.getString('audioRoute')) ? _prefs.getString('audioRoute')! : 'speaker';
  bool get fullDuplexSpeaker => _prefs.getBool('fullDuplexSpeaker') ?? false;
  bool? get audioEchoCancel => _prefs.getBool('audioEchoCancel');

  /// The folder of the avatar the PC last named, and the Wav2Lip self-test: the SHA-256 of the model that passed it on this phone ('' when
  /// none did) with its speed.
  String get avatarName => _prefs.getString('avatarName') ?? '';
  String get wav2lipPassed => _prefs.getString('wav2lipPassed') ?? '';
  double get wav2lipFps => _prefs.getDouble('wav2lipFps') ?? 0;
  double get wav2lipMs => _prefs.getDouble('wav2lipMs') ?? 0;

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
    await _storeKey(key.trim());
    if (name.trim().isNotEmpty) await _prefs.setString('name', name.trim());
    notifyListeners();
  }

  Future<void> _storeKey(String value) async {
    try {
      if (value.isEmpty) {
        await _secrets.delete(_keyName);
      } else {
        await _secrets.write(_keyName, value);
      }
      await _prefs.remove(_keyName);
      _keyIsSecure = true;
    } catch (e) {
      _keyIsSecure = false;
      _log('settings: secure store unavailable ($e); the phone key is kept in the plain preferences');
      await _prefs.setString(_keyName, value);
    }
    _key = value;
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

  Future<void> setAvatarMode(String value) async {
    await _prefs.setString('avatarMode', value);
    notifyListeners();
  }

  Future<void> setAvatarBrain(String value) async {
    await _prefs.setString('avatarBrain', value);
    notifyListeners();
  }

  Future<void> setAvatarPersona(String value) async {
    await _prefs.setString('avatarPersona', value);
    notifyListeners();
  }

  Future<void> setAvatarEnabled(bool value) async {
    await _prefs.setBool('avatarEnabled', value);
    notifyListeners();
  }

  Future<void> setAvatarResolution(int value) async {
    await _prefs.setInt('avatarResolution', value);
    notifyListeners();
  }

  Future<void> setAvatarQuality(AvatarQuality value) async {
    await _prefs.setString('avatarQuality', value.name);
    notifyListeners();
  }

  Future<void> setAudioRoute(String value) async {
    await _prefs.setString('audioRoute', value);
    notifyListeners();
  }

  Future<void> setFullDuplexSpeaker(bool value) async {
    await _prefs.setBool('fullDuplexSpeaker', value);
    notifyListeners();
  }

  Future<void> setAudioEchoCancel(bool value) async {
    await _prefs.setBool('audioEchoCancel', value);
    notifyListeners();
  }

  Future<void> setAvatarName(String value) async {
    await _prefs.setString('avatarName', value);
    notifyListeners();
  }

  /// Keeps the outcome of the Wav2Lip self-test: `sha` is the model that passed, '' when it failed.
  Future<void> setWav2lipTest({required String sha, required double fps, required double ms}) async {
    await _prefs.setString('wav2lipPassed', sha);
    await _prefs.setDouble('wav2lipFps', fps);
    await _prefs.setDouble('wav2lipMs', ms);
    notifyListeners();
  }
}
