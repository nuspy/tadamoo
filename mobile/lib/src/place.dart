import 'dart:async';

import 'package:geocoding/geocoding.dart';
import 'package:geolocator/geolocator.dart';

import 'settings.dart';

/// Where the phone is, for the messages: coordinates and, when the phone can work it out, the street, number and city.
/// Nothing is asked or sent unless the person switched it on in the settings; a missing fix or address never stops a message.
class PlaceFinder {
  PlaceFinder._(this.settings);
  static PlaceFinder? _shared;
  static PlaceFinder of(AppSettings settings) => _shared ??= PlaceFinder._(settings);

  final AppSettings settings;
  static const _fresh = Duration(seconds: 45);
  static const _fixWait = Duration(seconds: 3);
  static const _addressWait = Duration(seconds: 3);

  Map<String, dynamic>? _last;
  DateTime _at = DateTime.fromMillisecondsSinceEpoch(0);
  Position? _named;
  Map<String, String> _address = {};
  Future<Map<String, dynamic>?>? _running;

  /// Asks Android for the permission; true when the position may be read.
  static Future<bool> allow() async {
    if (!await Geolocator.isLocationServiceEnabled()) return false;
    var permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) permission = await Geolocator.requestPermission();
    return permission == LocationPermission.whileInUse || permission == LocationPermission.always;
  }

  /// The position to send with a message: a recent one when there is one, else a new reading. Null when the switch is off or there is none.
  Future<Map<String, dynamic>?> current() {
    if (!settings.sendPlace) return Future.value(null);
    if (_last != null && DateTime.now().difference(_at) < _fresh) return Future.value(_last);
    return _running ??= _read().whenComplete(() => _running = null);
  }

  Future<Map<String, dynamic>?> _read() async {
    try {
      final permission = await Geolocator.checkPermission();
      if (permission != LocationPermission.whileInUse && permission != LocationPermission.always) return null;
      Position? fix;
      try {
        fix = await Geolocator.getCurrentPosition(locationSettings: AndroidSettings(accuracy: LocationAccuracy.high, timeLimit: _fixWait));
      } catch (_) {
        fix = await Geolocator.getLastKnownPosition();
      }
      if (fix == null) return null;
      final address = await _addressOf(fix);
      _last = {
        'lat': fix.latitude,
        'lon': fix.longitude,
        'accuracy_m': fix.accuracy,
        'fix_age_s': DateTime.now().difference(fix.timestamp).inSeconds.clamp(0, 31 * 86400),
        if (address.isNotEmpty) 'address': address,
      };
      _at = DateTime.now();
      return _last;
    } catch (_) {
      return null;
    }
  }

  /// The street, number, postcode, city, region and country of a fix; reused while the phone stays within a few dozen metres.
  Future<Map<String, String>> _addressOf(Position fix) async {
    final near = _named;
    if (near != null && _address.isNotEmpty && Geolocator.distanceBetween(near.latitude, near.longitude, fix.latitude, fix.longitude) < 40) return _address;
    try {
      final marks = await Geocoding().placemarkFromCoordinates(fix.latitude, fix.longitude).timeout(_addressWait);
      if (marks.isEmpty) return {};
      final m = marks.first;
      String clean(String? v) => (v ?? '').trim();
      final found = {
        'street': clean(m.thoroughfare).isNotEmpty ? clean(m.thoroughfare) : clean(m.street),
        'number': clean(m.subThoroughfare),
        'postcode': clean(m.postalCode),
        'city': clean(m.locality),
        'region': clean(m.administrativeArea),
        'country': clean(m.country),
        'country_code': clean(m.isoCountryCode),
      }..removeWhere((_, v) => v.isEmpty);
      _named = fix;
      _address = found;
      return found;
    } catch (_) {
      return {};
    }
  }
}
