import 'package:basalt_mobile/src/secret_store.dart';

/// A secret store in memory, for tests: the platform Keystore needs a phone.
class FakeSecretStore implements SecretStore {
  final Map<String, String> data = {};
  bool broken = false;
  bool dropWrites = false;

  void _check() {
    if (broken) throw StateError('keystore unavailable');
  }

  @override
  Future<String?> read(String name) async {
    _check();
    return data[name];
  }

  @override
  Future<void> write(String name, String value) async {
    _check();
    if (!dropWrites) data[name] = value;
  }

  @override
  Future<void> delete(String name) async {
    _check();
    data.remove(name);
  }
}
