import 'package:flutter_test/flutter_test.dart';
import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/authenticator_screen.dart';
import 'package:basalt_mobile/src/i18n.dart';

void main() {
  setUp(() => Texts.use('en'));

  test('only authenticator links are handed over, each once, and the progress is shown', () async {
    final sent = <String>[];
    final t = OfferTracker((link) async {
      sent.add(link);
      return {'parts': 2, 'have': List.generate(sent.length, (i) => i + 1), 'accounts': sent.length, 'complete': sent.length >= 2};
    });
    expect(t.line(), 'Point the camera at the Google Authenticator QR code.');
    expect(await t.offer('https://example.com'), isFalse);
    expect(await t.offer('otpauth-migration://offline?data=AAA'), isTrue);
    expect(await t.offer('otpauth-migration://offline?data=AAA'), isFalse);
    expect(t.line(), 'Part 1 of 2 read.');
    expect(t.complete, isFalse);
    expect(await t.offer('otpauth-migration://offline?data=BBB'), isTrue);
    expect(t.complete, isTrue);
    expect(t.line(), '2 account handed to the PC.');
    expect(sent.length, 2);
  });

  test('a refusal of the PC is shown and the link can be tried again', () async {
    var fail = true;
    final t = OfferTracker((link) async {
      if (fail) throw ApiError(400, 'this is not an authenticator link');
      return {'parts': 0, 'have': [], 'accounts': 1, 'complete': true};
    });
    expect(await t.offer('otpauth://totp/x?secret=ABC'), isTrue);
    expect(t.line(), 'this is not an authenticator link');
    fail = false;
    expect(await t.offer('otpauth://totp/x?secret=ABC'), isTrue);
    expect(t.complete, isTrue);
  });
}
